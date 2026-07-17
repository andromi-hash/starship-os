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
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("agent-daemon")

def _resolve_nats_url() -> str:
    try:
        from nats_connect import build_nats_url, safe_url
        return build_nats_url()
    except ImportError:
        return os.getenv("NATS_URL", "nats://127.0.0.1:4222")


def _nats_log_url() -> str:
    try:
        from nats_connect import safe_url
        return safe_url()
    except ImportError:
        u = os.getenv("NATS_URL", "nats://127.0.0.1:4222")
        return u.split("@")[-1] if "@" in u else u


NATS_URL = _resolve_nats_url()
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
_SCRIPT_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
_PROJECT_ROOT = Path(os.getenv("STARSHIP_ROOT", os.getenv("AGNETIC_ROOT", str(_SCRIPT_DIR.parent))))
AGENTS_DIR = _PROJECT_ROOT / "agents"


def load_agent_config(name):
    """Load agent YAML config."""
    config_path = AGENTS_DIR / f"{name}.yaml"
    if not config_path.exists():
        log.error("Agent config not found: %s", config_path)
        sys.exit(1)
    with open(config_path) as f:
        return yaml.safe_load(f)


async def query_ollama(model, prompt, system=None, tools=None, nats=None, max_tool_rounds=10, callbacks=None):
    """Send a prompt to Ollama with optional tool calling loop.

    Borrowed patterns from Hermes Agent:
    - Tool call auto-repair for malformed JSON
    - Callback-driven streaming for real-time progress
    - Buffered status for retry noise

    If tools are provided, uses Ollama's chat API with tool definitions.
    Loops: send prompt → Ollama calls tool → execute → feed result back → repeat.
    Stops when Ollama returns text (no more tool calls) or max rounds hit.
    """
    import httpx
    from tools import get_tool_definitions, execute_tool, repair_tool_arguments

    callbacks = callbacks or {}
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    if not tools:
        # Simple generate (no tool calling)
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.3},
        }
        if system:
            payload["system"] = system
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(f"{OLLAMA_URL}/api/generate", json=payload)
            resp.raise_for_status()
            return resp.json().get("response", "")

    # Tool-calling loop via chat API (Hermes pattern)
    tool_defs = get_tool_definitions("full")
    status_buffer = []  # Hermes: buffered status for retry noise

    for round_num in range(max_tool_rounds):
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "tools": tool_defs,
            "options": {"temperature": 0.3},
        }

        # Emit step callback (Hermes pattern)
        if "step" in callbacks:
            callbacks["step"](round_num, max_tool_rounds)

        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(f"{OLLAMA_URL}/api/chat", json=payload)
                resp.raise_for_status()
                result = resp.json()
        except Exception as e:
            # Buffer error, retry once
            status_buffer.append(("error", str(e)))
            log.warning("Ollama request failed (round %d): %s", round_num, e)
            await asyncio.sleep(1)
            continue

        message = result.get("message", {})
        tool_calls = message.get("tool_calls", [])

        # No tool calls → return final text response
        if not tool_calls:
            content = message.get("content", "")
            # Flush status buffer on success (Hermes pattern)
            if status_buffer and "status" in callbacks:
                for kind, text in status_buffer:
                    callbacks["status"](kind, text)
            return content

        # Append assistant message
        messages.append(message)

        # Execute each tool call with auto-repair (Hermes pattern)
        for tc in tool_calls:
            func = tc.get("function", {})
            tool_name = func.get("name", "")
            tool_args_raw = func.get("arguments", {})

            # Auto-repair malformed arguments
            tool_args = repair_tool_arguments(tool_args_raw, tool_name)

            log.info("Tool call: %s(%s)", tool_name, json.dumps(tool_args)[:200])

            if "tool_progress" in callbacks:
                callbacks["tool_progress"](tool_name, tool_args, "starting")

            tool_result = await execute_tool(tool_name, tool_args, nats=nats, callbacks=callbacks)
            log.info("Tool result: %s", json.dumps(tool_result)[:300])

            if "tool_progress" in callbacks:
                callbacks["tool_progress"](tool_name, tool_result, "complete")

            messages.append({"role": "tool", "content": json.dumps(tool_result)})

    # Max rounds hit
    return f"[Tool loop completed after {max_tool_rounds} rounds]"


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


def load_memory_context(agent_name):
    """Load the MEMORY.md and USER.md files for an agent as a frozen snapshot."""
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


async def process_command(agent_name, config, subject, payload, telemetry=None, nats=None, use_tools=True):
    """Process a single command and return the result."""
    model = config.get("model", "qwen2.5:7b")
    role = config.get("role", "assistant")
    capabilities = config.get("capabilities", [])
    skills = config.get("skills", [])
    
    command = payload.get("command", "")
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
    
    soul = load_soul(agent_name)
    skill_context = load_skill_content(skills)
    skill_block = f"\n\n## Active Skills\n{skill_context}" if skill_context else ""

    memory_context = load_memory_context(agent_name)
    memory_block = f"\n\n## Persistent Memory (frozen at session start)\n{memory_context}" if memory_context else ""
    
    operational_context = (
        f"\n\n## Operational Context\n"
        f"You are connected via the Starship OS NATS agent bus.\n"
        f"{telemetry_context}"
        f"Current timestamp: {datetime.now().isoformat()}"
    )
    
    if soul:
        system_prompt = (
            f"{soul}\n"
            f"{skill_block}"
            f"{memory_block}"
            f"{operational_context}"
        )
    else:
        system_prompt = (
            f"You are {agent_name}, the {role} in the Starship OS agent mesh.\n"
            f"Your capabilities: {', '.join(capabilities) if capabilities else 'general assistance'}.\n"
            f"You operate via the NATS agent bus. Respond concisely and accurately.\n"
            f"{skill_block}"
            f"{memory_block}"
            f"{operational_context}"
        )
    
    user_prompt = f"Command: {command}\n"
    if args:
        user_prompt += f"Arguments: {json.dumps(args, indent=2)}\n"
    user_prompt += "\nProvide your response."
    
    log.info("Processing command '%s' for agent '%s' (tools=%s)", command, agent_name, use_tools)
    response = await query_ollama(model, user_prompt, system=system_prompt, tools=use_tools, nats=nats)
    log.info("Response received (%d chars) for '%s'", len(response), command)

    try:
        from services.archive import ArchiveService
        arch = ArchiveService()
        arch.write(agent=agent_name, command=command, response=response[:5000])
        arch.close()
    except Exception:
        pass

    return response


async def run_agent(agent_name, model_override=None):
    """Main agent daemon loop."""
    from nats_subjects import dual, dual_publish, agent_command, agent_status, agent_event, telemetry

    config = load_agent_config(agent_name)
    model = model_override or config.get("model", "qwen2.5:7b")
    nats_config = config.get("nats", {})
    cmd_subject = nats_config.get("subjects", {}).get("command", agent_command(agent_name))
    status_subject = nats_config.get("subjects", {}).get("status", agent_status(agent_name))
    event_subject = nats_config.get("subjects", {}).get("event", agent_event(agent_name))
    
    nats_url = _resolve_nats_url()
    log.info("Starting agent '%s' (model=%s, nats=%s)", agent_name, model, _nats_log_url())
    log.info("  Command subjects: %s", dual(cmd_subject))
    
    # Ensure model is available before connecting to NATS
    await ensure_model(model)
    
    try:
        try:
            from nats_connect import connect as nats_connect
        except ImportError:
            from nats import connect as nats_connect
        from nats.errors import TimeoutError
        
        nc = await nats_connect(nats_url)
        log.info("Connected to NATS: %s", _nats_log_url())
        
        await dual_publish(nc, status_subject, json.dumps({
            "agent": agent_name,
            "status": "online",
            "model": model,
            "timestamp": datetime.now().isoformat(),
        }).encode())
        
        # Subscribe to telemetry for live system context (both prefixes)
        telemetry_cache = {}
        telemetry_subjects = dual(telemetry()) + dual("starship.telemetry")
        # de-dupe
        telemetry_subjects = list(dict.fromkeys(telemetry_subjects))
        telemetry_tasks = []
        
        async def update_telemetry(msg):
            try:
                data = json.loads(msg.data.decode())
                parts = msg.subject.split(".")
                if len(parts) >= 3:
                    key = parts[-1]  # e.g., "cpu" from "*.telemetry.cpu"
                else:
                    key = "full"
                telemetry_cache[key] = data
                telemetry_cache["_timestamp"] = datetime.now().isoformat()
            except (json.JSONDecodeError, IndexError):
                pass
        
        for ts in telemetry_subjects:
            sub = await nc.subscribe(ts)
            log.info("Subscribed to telemetry: %s", ts)
            task = asyncio.create_task(_consume_msgs(sub, update_telemetry))
            telemetry_tasks.append(task)
        
        # Subscribe to commands on both starship.* and agnetic.*
        cmd_subs = []
        for cs in dual(cmd_subject):
            sub = await nc.subscribe(cs)
            log.info("Subscribed to: %s", cs)
            cmd_subs.append(sub)
        
        async def handle_msg(msg):
            subject = msg.subject
            try:
                data = json.loads(msg.data.decode())
                log.info("Received command on %s", subject)
                
                await dual_publish(nc, status_subject, json.dumps({
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
                await dual_publish(nc, status_subject, status_payload)
                reply_to = data.get("reply_to", "")
                if reply_to:
                    await dual_publish(nc, reply_to, status_payload)
                
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
        
        # Process messages from all dual-prefix command subscriptions
        await nc.flush()
        cmd_tasks = [
            asyncio.create_task(_consume_msgs(sub, handle_msg))
            for sub in cmd_subs
        ]
        try:
            await asyncio.gather(*cmd_tasks)
        except asyncio.CancelledError:
            pass
            
    except ImportError:
        log.error("nats-py not installed. Run: pip install nats-py")
        sys.exit(1)
    except KeyboardInterrupt:
        log.info("Shutting down...")
        if 'nc' in locals():
            from nats_subjects import dual_publish as _dp
            await _dp(nc, status_subject, json.dumps({
                "agent": agent_name,
                "status": "offline",
                "timestamp": datetime.now().isoformat(),
            }).encode())
            await nc.close()
        if 'telemetry_tasks' in locals():
            for t in telemetry_tasks:
                t.cancel()
        if 'cmd_tasks' in locals():
            for t in cmd_tasks:
                t.cancel()
    except Exception as e:
        log.error("Fatal error: %s", e)
        sys.exit(1)


def main():
    if len(sys.argv) < 2:
        print("Usage: agent_daemon.py <agent_name> [--model MODEL]")
        print("Examples: agent_daemon.py proxy")
        print("          agent_daemon.py romi --model qwen2.5:7b")
        print("          agent_daemon.py ergo")
        sys.exit(1)
    
    agent_name = sys.argv[1]
    model_override = None
    if "--model" in sys.argv:
        idx = sys.argv.index("--model")
        if idx + 1 < len(sys.argv):
            model_override = sys.argv[idx + 1]
    
    asyncio.run(run_agent(agent_name, model_override))


if __name__ == "__main__":
    main()
