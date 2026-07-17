#!/usr/bin/env python3
"""
Starship OS Agent Daemon

Subscribes to NATS command subjects for a given agent role,
processes commands via Ollama API, and publishes responses back.

Usage:
  python3 agent_daemon.py <agent_name> [--model MODEL] [--nats NATS_URL]

Agent config files are loaded from ./agents/<agent_name>.yaml
"""

import sys
import os
import json
import yaml
import asyncio
import logging
import signal
from pathlib import Path
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("agent-daemon")

NATS_URL = os.getenv("NATS_URL", "nats://127.0.0.1:4222")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11435")
_SCRIPT_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
_PROJECT_ROOT = Path(os.getenv("AGNETIC_ROOT", str(_SCRIPT_DIR.parent)))
AGENTS_DIR = _PROJECT_ROOT / "agents"

try:
    from services.memory import MemoryManager, MemoryType, ProspectiveMemoryManager, get_memory_manager, get_prospective_memory, MEMORY_DESCRIPTIONS
except Exception:
    MemoryManager = None
    MemoryType = None
    ProspectiveMemoryManager = None
    get_memory_manager = None
    get_prospective_memory = None
    MEMORY_DESCRIPTIONS = {}
try:
    from services.governance import GovernanceManager
except Exception:
    GovernanceManager = None
try:
    from services.mcp import MCPManager
except Exception:
    MCPManager = None
try:
    from services.event_hooks import get_hook_manager
except Exception:
    get_hook_manager = None
try:
    from services.credential_pool import get_credential_manager
except Exception:
    get_credential_manager = None
try:
    from lib.plugin_manager import get_plugin_manager
except Exception:
    get_plugin_manager = None


def load_agent_config(name, role=None, model=None, ephemeral=False):
    """Load agent YAML config. Falls back to dynamic defaults for spawned subagents."""
    candidates = [
        AGENTS_DIR / f"{name}.yaml",
        AGENTS_DIR / f"{name}.json",
        Path("/tmp/agnetic-subagents") / f"{name}.yaml",
        Path("/tmp/agnetic-subagents") / f"{name}.json",
    ]
    for p in candidates:
        if p.exists():
            with open(p) as f:
                if p.suffix == ".json":
                    cfg = json.load(f)
                else:
                    cfg = yaml.safe_load(f) if yaml else json.load(f)
            break
    else:
        # Dynamic subagent support
        cfg = {
            "name": name,
            "role": role or "subagent",
            "model": model or "qwen2.5:3b",
            "provider": "ollama",
            "ephemeral": ephemeral,
            "capabilities": ["task_execution", "delegation"],
            "skills": [],
            "nats": {
                "subjects": {
                    "command": f"agnetic.agent.{name}.command.>",
                    "event": f"agnetic.agent.{name}.event.>",
                    "status": f"agnetic.agent.{name}.status"
                }
            }
        }
    if ephemeral:
        cfg["ephemeral"] = True
    return cfg


async def query_ollama(model, prompt, system=None, tools=None, nats=None, max_tool_rounds=10, callbacks=None, provider_name="ollama", provider_config=None):
    """Send a prompt to the LLM provider with optional tool calling loop.

    Supports Ollama and OpenAI-compatible providers (OpenRouter, custom).
    Borrowed patterns from Hermes Agent for tool call handling.
    """
    import httpx
    from tools import get_tool_definitions, execute_tool, repair_tool_arguments

    callbacks = callbacks or {}
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    if not tools:
        return await _simple_generate(model, prompt, system, provider_name, provider_config)

    # Tool-calling loop
    tool_defs = get_tool_definitions("full")
    status_buffer = []

    for round_num in range(max_tool_rounds):
        if "step" in callbacks:
            callbacks["step"](round_num, max_tool_rounds)

        try:
            result = await _provider_chat(model, messages, tool_defs, provider_name, provider_config)
        except Exception as e:
            status_buffer.append(("error", str(e)))
            log.warning("Provider request failed (round %d): %s", round_num, e)
            await asyncio.sleep(1)
            continue

        message = result.get("message", {})
        tool_calls = message.get("tool_calls", [])

        if not tool_calls:
            content = message.get("content", "")
            if status_buffer and "status" in callbacks:
                for kind, text in status_buffer:
                    callbacks["status"](kind, text)
            return content

        messages.append(message)

        for tc in tool_calls:
            func = tc.get("function", {})
            tool_name = func.get("name", "")
            tool_args_raw = func.get("arguments", {})
            tool_args = repair_tool_arguments(tool_args_raw, tool_name)

            log.info("Tool call: %s(%s)", tool_name, json.dumps(tool_args)[:200])
            if "tool_progress" in callbacks:
                callbacks["tool_progress"](tool_name, tool_args, "starting")

            tool_result = await execute_tool(tool_name, tool_args, nats=nats, callbacks=callbacks)
            log.info("Tool result: %s", json.dumps(tool_result)[:300])

            if "tool_progress" in callbacks:
                callbacks["tool_progress"](tool_name, tool_result, "complete")

            messages.append({"role": "tool", "content": json.dumps(tool_result)})

    return f"[Tool loop completed after {max_tool_rounds} rounds]"


async def _simple_generate(model, prompt, system, provider_name="ollama", provider_config=None):
    from services.provider_router import get_provider, get_model_info

    if provider_name != "ollama":
        model_info = get_model_info("__generic__", model)
        if provider_config:
            model_info["config"] = provider_config
        return await _query_llm(model_info, [{"role": "user", "content": prompt}], system=system, tools=False)

    import httpx
    url = (provider_config or {}).get("url") or OLLAMA_URL
    payload = {"model": model, "prompt": prompt, "stream": False, "options": {"temperature": 0.3}}
    if system:
        payload["system"] = system
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(f"{url}/api/generate", json=payload)
        resp.raise_for_status()
        return resp.json().get("response", "")


async def _provider_chat(model, messages, tool_defs, provider_name="ollama", provider_config=None):
    from services.provider_router import get_provider, get_model_info

    if provider_name != "ollama":
        model_info = get_model_info("__generic__", model)
        if provider_config:
            model_info["config"] = provider_config
        return await _query_llm(model_info, messages, tools=True)

    url = (provider_config or {}).get("url") or OLLAMA_URL
    import httpx
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "tools": tool_defs,
        "options": {"temperature": 0.3},
    }
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(f"{url}/api/chat", json=payload)
        resp.raise_for_status()
        return resp.json()


async def _query_llm(model_info, messages, system=None, tools=False):
    """Route to provider_router for non-Ollama providers."""
    from services.provider_router import query_provider
    result = await query_provider(model_info, messages, system=system, tools=tools)
    if isinstance(result, dict):
        return result
    return {"message": {"content": result, "tool_calls": []}}


async def ensure_model(model):
    """Check if Ollama model exists, pull if not."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{OLLAMA_URL}/api/tags")
            if resp.status_code == 200:
                models = [m["name"] for m in resp.json().get("models", [])]
                # Check both exact match and name:tag match
                if model in models or any(m.startswith(model + ":") for m in models):
                    log.info("Model '%s' already available", model)
                    return True
    except Exception as e:
        log.warning("Failed to check Ollama models: %s", e)

    # Model not found — pull it
    log.info("Model '%s' not found — pulling from Ollama...", model)
    try:
        proc = await asyncio.create_subprocess_exec(
            "ollama", "pull", model,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0:
            log.info("Model '%s' pulled successfully", model)
            return True
        else:
            log.error("Failed to pull model '%s': %s", model, stdout.decode()[-200:])
            return False
    except Exception as e:
        log.error("Failed to pull model '%s': %s", model, e)
        return False


async def _consume_msgs(sub, handler):
    """Consume messages from a subscription and pass to handler."""
    try:
        async for msg in sub.messages:
            await handler(msg)
    except asyncio.CancelledError:
        pass


SKILLS_DIR = _PROJECT_ROOT / "skills"
SOULS_DIR = _PROJECT_ROOT / "souls"
MEMORY_DIR = _PROJECT_ROOT / "memory"


def load_skill_content(skill_names):
    """Load skill markdown files and return their content."""
    parts = []
    for name in (skill_names or []):
        skill_path = SKILLS_DIR / name / "SKILL.md"
        if skill_path.exists():
            try:
                content = skill_path.read_text()
                parts.append(f"=== Skill: {name} ===\n{content.strip()}\n")
            except Exception as e:
                log.warning("Failed to load skill '%s': %s", name, e)
    return "\n\n".join(parts)


def load_soul(agent_name):
    """Load the SOUL.md personality file for an agent."""
    soul_path = SOULS_DIR / agent_name / "SOUL.md"
    if soul_path.exists():
        try:
            content = soul_path.read_text().strip()
            log.info("Loaded soul for '%s' (%d chars)", agent_name, len(content))
            return content
        except Exception as e:
            log.warning("Failed to load soul for '%s': %s", agent_name, e)
    log.info("No soul file found for '%s', using generic personality", agent_name)
    return None


def load_memory_files(agent_name):
    """Load the MEMORY.md and USER.md files as a frozen snapshot."""
    memory_path = MEMORY_DIR / agent_name / "MEMORY.md"
    user_path = MEMORY_DIR / agent_name / "USER.md"
    parts = []
    if memory_path.exists():
        try:
            content = memory_path.read_text().strip()
            if content:
                parts.append(f"=== Agent Notes ===\n{content}")
        except Exception as e:
            log.warning("Failed to load MEMORY.md for '%s': %s", agent_name, e)
    if user_path.exists():
        try:
            content = user_path.read_text().strip()
            if content:
                parts.append(f"=== User Profile (frozen at session start) ===\n{content}")
        except Exception as e:
            log.warning("Failed to load USER.md for '%s': %s", agent_name, e)
    if parts:
        return "\n\n".join(parts)
    return None


def choose_model(config: dict, command: str) -> tuple:
    """Select best model + provider based on config and command content.
    Returns (model_name, provider_name).
    """
    models_cfg = config.get("models", {})
    default_model = models_cfg.get("default") or config.get("model", "qwen2.5:7b")
    default_provider = config.get("provider", "ollama")
    available = models_cfg.get("available", [default_model])
    provider_models = models_cfg.get("providers", {})
    cmd_l = (command or "").lower()

    heavy = any(k in cmd_l for k in ["code", "debug", "fix", "tech", "diagnose", "script", "implement", "analyze", "research", "brief"])
    light = any(k in cmd_l for k in ["quick", "status", "list", "fast", "ping", "help", "hello"])

    if heavy:
        for m in available:
            if "coder" in m.lower() or ":7b" in m:
                return m, default_provider
        if provider_models:
            for pname, models in provider_models.items():
                if models:
                    return models[0], pname
    if light:
        for m in available:
            if any(x in m.lower() for x in [":3b", ":2b", "gemma", "llama3.2"]):
                return m, default_provider

    return default_model, default_provider


async def process_command(agent_name, config, subject, payload, telemetry=None, nats=None, use_tools=True):
    """Process a single command and return the result."""
    command = payload.get("command", "")
    model, provider = choose_model(config, command)
    # Support runtime model/provider override via payload
    if payload.get("model"):
        model = payload["model"]
    if payload.get("provider"):
        provider = payload["provider"]
    # Support runtime model switch via UI /tmp override
    try:
        ov = f"/tmp/agnetic-model-{agent_name}"
        if os.path.exists(ov):
            with open(ov) as f:
                ov_model = f.read().strip()
                if ov_model:
                    model = ov_model
    except Exception:
        pass
    if "/" in model and provider == "ollama":
        provider = "openrouter"
    if provider == "ollama" and model != config.get("model", model):
        asyncio.create_task(ensure_model(model))
    if GovernanceManager:
        try:
            gov = GovernanceManager()
            risky = any(k in command.lower() for k in ["shell", "deploy", "write", "flamingo", "sudo", "rm", "kill"])
            if risky:
                decision = await gov.check_action(agent_name, command, {"args": args})
                if not decision.get("approved", True):
                    return f"[GOVERNANCE BLOCKED] {decision.get('reason', 'denied')}"
        except Exception:
            pass
    role = config.get("role", "assistant")
    capabilities = config.get("capabilities", [])
    skills = config.get("skills", [])
    
    args = payload.get("args", {})
    
    telemetry_context = ""
    if telemetry:
        parts = []
        # Handle flat StarAgent telemetry (single "agnetic.telemetry" message)
        if "full" in telemetry:
            f = telemetry["full"]
            cpu = f.get("cpu", "N/A")
            mu = f.get("memory_used", 0) // (1024*1024)
            mt = f.get("memory_total", 0) // (1024*1024)
            du = f.get("disk_used", 0) // (1024*1024*1024)
            dt = f.get("disk_total", 0) // (1024*1024*1024)
            rx = f.get("rx_bytes", 0) // 1024
            tx = f.get("tx_bytes", 0) // 1024
            parts.append(f"CPU: {cpu}% | Memory: {mu}MB/{mt}MB | Disk: {du}GB/{dt}GB | Net RX: {rx}KB TX: {tx}KB")
        # Handle individual subject telemetry (future use)
        if "cpu" in telemetry:
            c = telemetry["cpu"]
            parts.append(f"CPU Usage: {c.get('percent', 'N/A')}%")
        if "mem" in telemetry:
            m = telemetry["mem"]
            mu = m.get("used", 0) // (1024*1024)
            mt = m.get("total", 0) // (1024*1024)
            parts.append(f"Memory: {mu}MB / {mt}MB")
        if "disk" in telemetry:
            d = telemetry["disk"]
            du = d.get("used", 0) // (1024*1024*1024)
            dt = d.get("total", 0) // (1024*1024*1024)
            parts.append(f"Disk: {du}GB / {dt}GB")
        if "net" in telemetry:
            n = telemetry["net"]
            parts.append(f"Net RX: {n.get('rx_bytes', 0)//1024}KB TX: {n.get('tx_bytes', 0)//1024}KB")
        if parts:
            telemetry_context = "Live System Telemetry:\n" + "\n".join(f"  {p}" for p in parts) + "\n"
    
    memory_context = ""
    if MemoryManager:
        try:
            mgr = get_memory_manager()
            mem_parts = []
            try:
                semantic = await mgr.semantic_search(command, limit=3)
                if semantic:
                    items = "\n".join(f"  • {m.summary or m.content[:120]}" for m in semantic)
                    mem_parts.append(f"### Semantic Facts\n{items}")
            except Exception: pass
            try:
                episodic = await mgr.episodic_search(command, limit=3)
                if episodic:
                    items = "\n".join(f"  • [{m.importance:.2f}] {m.summary or m.content[:120]}" for m in episodic)
                    mem_parts.append(f"### Past Episodes\n{items}")
            except Exception: pass
            try:
                prospective = await mgr.prospective_search(status="pending", limit=5)
                if prospective:
                    items = "\n".join(f"  • {m.summary[:120]}" for m in prospective)
                    mem_parts.append(f"### Pending Intentions\n{items}")
            except Exception: pass
            try:
                procedural = await mgr.search("", mem_type=MemoryType.PROCEDURAL, limit=3, agent=agent_name)
                if procedural:
                    items = "\n".join(f"  • {m.summary[:120]}" for m in procedural)
                    mem_parts.append(f"### Procedural Rules\n{items}")
            except Exception: pass
            try:
                working = await mgr.search("", mem_type=MemoryType.WORKING, limit=3, agent=agent_name)
                if working:
                    items = "\n".join(f"  • {m.content[:120]}" for m in working)
                    mem_parts.append(f"### Working Context\n{items}")
            except Exception: pass
            combined = "\n\n".join(mem_parts)
            if combined:
                memory_context = "\n\n## Memory Systems\n" + combined
            try:
                retrieval_ctx = await mgr.get_context(command, agent_name, max_tokens=400) or ""
                if retrieval_ctx:
                    memory_context += "\n\n## Vector Retrieval\n" + retrieval_ctx
            except Exception: pass
        except Exception:
            pass
    
    soul = load_soul(agent_name)
    skill_context = load_skill_content(skills)
    skill_block = f"\n\n## Active Skills\n{skill_context}" if skill_context else ""
    
    memory_files_ctx = load_memory_files(agent_name)
    memory_files_block = f"\n\n## Persistent Memory (frozen at session start)\n{memory_files_ctx}" if memory_files_ctx else ""

    context_ctx = ""
    try:
        from services.context_loader import load_context
        context_ctx = load_context(agent_name)
    except Exception:
        pass
    context_block = f"\n\n## Discovered Context Files\n{context_ctx}" if context_ctx else ""

    operational_context = (
        f"\n\n## Operational Context\n"
        f"You are connected via the Starship OS NATS agent bus.\n"
        f"{telemetry_context}{memory_context}"
        f"{memory_files_block}"
        f"{context_block}"
        f"Current timestamp: {datetime.now().isoformat()}"
    )
    
    extra_capabilities = []
    try:
        from services.mcp import _mcp_manager
        if _mcp_manager._loaded and _mcp_manager._tools_cache:
            extra_capabilities.append(f"MCP tools available: {len(_mcp_manager._tools_cache)} tools from {len(_mcp_manager.servers)} servers")
    except Exception:
        pass
    try:
        from services.checkpoint import CHECKPOINT_DIR
        if CHECKPOINT_DIR.exists():
            extra_capabilities.append("Checkpoint/rollback system with filesystem snapshots")
    except Exception:
        pass
    try:
        from services.browser import _browser
        if _browser.is_available:
            extra_capabilities.append("Browser automation for web interaction")
    except Exception:
        pass
    extra_block = f"\n\n## Extensions\n" + "\n".join(f"- {c}" for c in extra_capabilities) if extra_capabilities else ""

    if soul:
        system_prompt = (
            f"{soul}\n"
            f"{skill_block}"
            f"{operational_context}"
            f"{extra_block}"
        )
    else:
        system_prompt = (
            f"You are {agent_name}, the {role} in the Starship OS agent mesh.\n"
            f"Your capabilities: {', '.join(capabilities) if capabilities else 'general assistance'}.\n"
            f"You operate via the NATS agent bus. Respond concisely and accurately.\n"
            f"{skill_block}"
            f"{operational_context}"
            f"{extra_block}"
        )
    
    user_prompt = f"Command: {command}\n"
    if args:
        user_prompt += f"Arguments: {json.dumps(args, indent=2)}\n"
    user_prompt += "\nProvide your response."
    
    log.info("Processing command '%s' for agent '%s' (model=%s, provider=%s, tools=%s)", command, agent_name, model, provider, use_tools)
    response = await query_ollama(model, user_prompt, system=system_prompt, tools=use_tools, nats=nats, provider_name=provider)
    log.info("Response received (%d chars) for '%s'", len(response), command)
    if MemoryManager and MemoryType:
        try:
            mgr = get_memory_manager()
            await mgr.store(
                agent_name,
                MemoryType.EPISODIC,
                f"Command: {command}\nResponse: {response[:300]}",
                summary=f"{command[:60]} | {response[:60]}".replace("\n", " "),
                importance=0.35,
            )
        except Exception:
            pass

    try:
        from services.archive import ArchiveService
        arch = ArchiveService()
        arch.write(agent=agent_name, command=command, response=response[:5000])
        arch.close()
    except Exception:
        pass

    stall_phrases = ["stuck", "can't", "failing", "hard problem", "debug loop", "no idea", "repeat"]
    if any(p in (command + " " + response).lower() for p in stall_phrases):
        try:
            codex_prompt = f"codex:rescue {command} previous_response: {response[:800]}"
            codex_res = await execute_tool("opencode", {"prompt": codex_prompt, "context": command})
            if isinstance(codex_res, dict):
                codex_out = codex_res.get("stdout", codex_res.get("output", str(codex_res)))
            else:
                codex_out = str(codex_res)
            response = response + "\n\n[AUTONOMOUS CODEX SUBAGENT INVOKED]\n" + str(codex_out)[:1500]
        except Exception:
            pass

    # Backlog update hooks inside process_command: auto "in_progress" when delegated
    try:
        if MemoryManager and MemoryType:
            mgr = get_memory_manager()
            if any(k in (command or "").lower() for k in ["delegate", "spawn_subagent", "backlog"]):
                res = await mgr.search("backlog", limit=5)
                for r in res:
                    try:
                        d = json.loads(r.content)
                        if (d.get("assignee") == agent_name or "backlog" in (command or "").lower()) and d.get("status") in ("todo", "pending"):
                            d["status"] = "in_progress"
                            d["updated"] = datetime.now(timezone.utc).isoformat()
                            await mgr.store("backlog", MemoryType.PROCEDURAL, json.dumps(d), metadata={"backlog_id": d.get("id"), "assignee": agent_name, "status": "in_progress"}, importance=0.8)
                    except: pass
    except Exception:
        pass
    return response


async def run_agent(agent_name, model_override=None, role=None, ephemeral=False):
    """Main agent daemon loop."""
    config = load_agent_config(agent_name, role=role, model=model_override, ephemeral=ephemeral)
    model = model_override or config.get("model", "qwen2.5:7b")
    nats_config = config.get("nats", {})
    cmd_subject = nats_config.get("subjects", {}).get("command", f"agnetic.agent.{agent_name}.command.>")
    status_subject = nats_config.get("subjects", {}).get("status", f"agnetic.agent.{agent_name}.status")
    event_subject = nats_config.get("subjects", {}).get("event", f"agnetic.agent.{agent_name}.event.>")
    
    log.info("Starting agent '%s' (model=%s, nats=%s)", agent_name, model, NATS_URL)
    log.info("  Command subject: %s", cmd_subject)

    # Initialize new services: MCP, hooks, credentials, plugins
    try:
        from services.mcp import _mcp_manager
        await _mcp_manager.initialize()
        log.info("MCP initialized (%d servers)", len(_mcp_manager.servers))
    except Exception as e:
        log.debug("MCP init skipped: %s", e)

    if get_hook_manager:
        hm = get_hook_manager()
        hm.load_hooks_from_directory()
        log.info("Event hooks initialized (%d events)", len(hm._hooks))

    if get_credential_manager:
        cm = get_credential_manager()
        cm.load()
        log.info("Credential pools loaded (%d pools)", len(cm.pools))

    if get_plugin_manager:
        pm = get_plugin_manager()
        pm.discover()
        log.info("Plugins discovered (%d)", len(pm.plugins))

    # Initialize self-healing heartbeat
    _healer_ready = False
    try:
        from services.healer import check_and_report
        _healer_ready = True
    except Exception:
        _healer_ready = False
    if _healer_ready:
        log.info("Self-healing system initialized for '%s'", agent_name)

    # Ensure model is available before connecting to NATS
    await ensure_model(model)
    await ensure_model("nomic-embed-text")

    last_activity = datetime.now(timezone.utc)
    IDLE_TIMEOUT = 60  # seconds for ephemeral agents

    async def retire_if_idle():
        nonlocal last_activity
        while True:
            await asyncio.sleep(10)
            if config.get("ephemeral") and (datetime.now(timezone.utc) - last_activity).total_seconds() > IDLE_TIMEOUT:
                log.info("Ephemeral agent %s retiring due to idle timeout", agent_name)
                await nc.publish(status_subject, json.dumps({
                    "agent": agent_name,
                    "status": "retired",
                    "reason": "idle_timeout",
                    "timestamp": datetime.now().isoformat(),
                }).encode())
                await nc.close()
                sys.exit(0)
    
    try:
        from nats import connect as nats_connect
        from nats.errors import TimeoutError
        
        nc = await nats_connect(NATS_URL)
        log.info("Connected to NATS: %s", NATS_URL)
        
        await nc.publish(status_subject, json.dumps({
            "agent": agent_name,
            "status": "online",
            "model": model,
            "timestamp": datetime.now().isoformat(),
        }).encode())

        if config.get("ephemeral"):
            asyncio.create_task(retire_if_idle())

        # Self-healing heartbeat
        if _healer_ready:
            async def heartbeat():
                while True:
                    await asyncio.sleep(30)
                    try:
                        import psutil
                        mem = psutil.Process().memory_info().rss / (1024 * 1024)
                    except Exception:
                        mem = 0
                    check_and_report(
                        agent=agent_name,
                        status="alive",
                        response_time_ms=0,
                        memory_usage_mb=mem,
                    )
            asyncio.create_task(heartbeat())
            log.info("Self-healing heartbeat started for '%s' (30s interval)", agent_name)

        # Subscribe to telemetry for live system context
        telemetry_cache = {}
        telemetry_subjects = ["agnetic.telemetry.>", "agnetic.telemetry"]
        telemetry_tasks = []
        
        async def update_telemetry(msg):
            try:
                data = json.loads(msg.data.decode())
                parts = msg.subject.split(".")
                if len(parts) >= 3:
                    key = parts[-1]  # e.g., "cpu" from "agnetic.telemetry.cpu"
                else:
                    key = "full"  # flat "agnetic.telemetry" -> store as "full"
                telemetry_cache[key] = data
                telemetry_cache["_timestamp"] = datetime.now().isoformat()
            except (json.JSONDecodeError, IndexError):
                pass
        
        for ts in telemetry_subjects:
            sub = await nc.subscribe(ts)
            log.info("Subscribed to telemetry: %s", ts)
            task = asyncio.create_task(_consume_msgs(sub, update_telemetry))
            telemetry_tasks.append(task)
        
        # Subscribe to commands
        sub = await nc.subscribe(cmd_subject)
        log.info("Subscribed to: %s", cmd_subject)
        
        async def handle_msg(msg):
            nonlocal last_activity
            last_activity = datetime.now(timezone.utc)
            subject = msg.subject
            try:
                data = json.loads(msg.data.decode())
                log.info("Received command on %s", subject)
                
                reply_subject = f"agnetic.agent.{agent_name}.status"
                await nc.publish(reply_subject, json.dumps({
                    "agent": agent_name,
                    "status": "processing",
                    "command": data.get("command", ""),
                    "timestamp": datetime.now().isoformat(),
                }).encode())
                
                response = await process_command(agent_name, config, subject, data, telemetry_cache, nats=nc, use_tools=True)
                
                # Publish response to status (for simple replies) or a reply subject
                status_payload = json.dumps({
                    "agent": agent_name,
                    "status": "complete",
                    "command": data.get("command", ""),
                    "response": response,
                    "timestamp": datetime.now().isoformat(),
                }).encode()
                await nc.publish(status_subject, status_payload)
                reply_to = data.get("reply_to", "")
                if reply_to:
                    await nc.publish(reply_to, status_payload)
                
                # If the message had a reply subject (NATS request-reply), respond directly
                if msg.reply:
                    await nc.publish(msg.reply, json.dumps({
                        "agent": agent_name,
                        "response": response,
                    }).encode())
                    
            except json.JSONDecodeError:
                log.warning("Invalid JSON on %s: %s", subject, msg.data[:200])
                if msg.reply:
                    await nc.publish(msg.reply, json.dumps({"error": "invalid JSON"}).encode())
            except Exception as e:
                log.error("Error processing message: %s", e)
                if msg.reply:
                    await nc.publish(msg.reply, json.dumps({"error": str(e)}).encode())
        
        # Process messages
        await nc.flush()
        
        try:
            async for msg in sub.messages:
                await handle_msg(msg)
        except asyncio.CancelledError:
            pass
            
    except ImportError:
        log.error("nats-py not installed. Run: pip install nats-py")
        sys.exit(1)
    except KeyboardInterrupt:
        log.info("Shutting down...")
        if 'nc' in locals():
            await nc.publish(status_subject, json.dumps({
                "agent": agent_name,
                "status": "offline",
                "timestamp": datetime.now().isoformat(),
            }).encode())
            await nc.close()
        if 'telemetry_tasks' in locals():
            for t in telemetry_tasks:
                t.cancel()
    except Exception as e:
        log.error("Fatal error: %s", e)
        sys.exit(1)


def main():
    if len(sys.argv) < 2:
        print("Usage: agent_daemon.py <agent_name> [--model MODEL] [--role ROLE] [--ephemeral]")
        print("Examples: agent_daemon.py proxy")
        print("          agent_daemon.py sub-debug-123 --model qwen2.5:3b --role debugger --ephemeral")
        sys.exit(1)
    
    agent_name = sys.argv[1]
    model_override = None
    role = None
    ephemeral = False
    
    if "--model" in sys.argv:
        idx = sys.argv.index("--model")
        if idx + 1 < len(sys.argv):
            model_override = sys.argv[idx + 1]
    if "--role" in sys.argv:
        idx = sys.argv.index("--role")
        if idx + 1 < len(sys.argv):
            role = sys.argv[idx + 1]
    if "--ephemeral" in sys.argv:
        ephemeral = True
    
    asyncio.run(run_agent(agent_name, model_override, role=role, ephemeral=ephemeral))


if __name__ == "__main__":
    main()
