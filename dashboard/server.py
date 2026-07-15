#!/usr/bin/env python3
"""Starship OS Web Dashboard — dynamic config, real-time status, Ollama management."""

import sys
import os
import json
import asyncio
import logging
import subprocess
import uuid
from pathlib import Path
from datetime import datetime
from aiohttp import web

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("agnetic-dash")

NATS_URL = os.getenv("NATS_URL", "nats://127.0.0.1:4222")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
PORT = int(os.getenv("DASHBOARD_PORT", os.getenv("AGNETIC_DASHBOARD_PORT", "8788")))
STATUS_FILE = Path("/tmp/agnetic-status.json")
GPU_STATE = Path("/tmp/agnetic-gpu-state.json")
HISTORY_DIR = Path("/tmp/agnetic-history")
PROJECT_DIR = Path(os.getenv("AGNETIC_ROOT", os.path.dirname(os.path.abspath(__file__)).replace("/dashboard", "")))

nc = None


def load_agent_configs():
    """Load agent configs from YAML files."""
    configs = {}
    agents_dir = PROJECT_DIR / "agents"
    for yaml_file in agents_dir.glob("*.yaml"):
        if yaml_file.name == "config.yaml":
            continue
        try:
            import yaml
            with open(yaml_file) as f:
                data = yaml.safe_load(f)
            if data and "agent" in data:
                name = data["agent"].get("name", yaml_file.stem)
                configs[name] = {
                    "name": name,
                    "model": data["agent"].get("model", "unknown"),
                    "description": data["agent"].get("description", ""),
                    "skills": data["agent"].get("skills", []),
                    "nats_subjects": data["agent"].get("nats", {}).get("subjects", {}),
                    "file": yaml_file.name,
                }
        except Exception as e:
            log.warning(f"Failed to load {yaml_file}: {e}")

    # Fallback: read main config.yaml
    if not configs:
        config_file = agents_dir / "config.yaml"
        if config_file.exists():
            try:
                import yaml
                with open(config_file) as f:
                    data = yaml.safe_load(f)
                for name, agent_data in data.get("agents", {}).items():
                    configs[name] = {
                        "name": name,
                        "model": agent_data.get("model", "unknown"),
                        "description": f"{name} agent",
                        "skills": agent_data.get("skills", []),
                        "file": "config.yaml",
                    }
            except Exception as e:
                log.warning(f"Failed to load config.yaml: {e}")

    return configs


def get_gpu_info():
    """Read GPU state from detect-gpu output."""
    try:
        if GPU_STATE.exists():
            return json.loads(GPU_STATE.read_text())
    except (json.JSONDecodeError, IOError):
        pass
    return {"vendor": "none"}


def get_system_telemetry():
    """Read system telemetry from status file."""
    try:
        if STATUS_FILE.exists():
            return json.loads(STATUS_FILE.read_text())
    except (json.JSONDecodeError, IOError):
        pass
    return {"agents": {}, "telemetry": {}, "messages": []}


async def get_nats():
    global nc
    if nc is None or not nc.is_connected:
        from nats import connect as nats_connect
        nc = await nats_connect(NATS_URL)
    return nc


async def get_ollama_models():
    """Fetch Ollama model list."""
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{OLLAMA_URL}/api/tags") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("models", [])
    except Exception:
        pass
    return []


async def get_agent_process_status():
    """Check which agent daemons are running."""
    agents = {}
    for name in ["proxy", "romi", "ergo"]:
        try:
            result = subprocess.run(
                ["pgrep", "-f", f"agent_daemon.py {name}"],
                capture_output=True, timeout=2
            )
            agents[name] = result.returncode == 0
        except Exception:
            agents[name] = False
    return agents


async def handle_index(request):
    index_html = PROJECT_DIR / "dashboard" / "index.html"
    if index_html.exists():
        return web.Response(text=index_html.read_text(), content_type="text/html")
    return web.Response(text="<h1>Dashboard loading...</h1>", content_type="text/html")


async def handle_api_dashboard(request):
    """Main dashboard API — returns all data needed by the UI."""
    agent_configs = load_agent_configs()
    agent_status = await get_agent_process_status()
    gpu_info = get_gpu_info()
    telemetry = get_system_telemetry()
    ollama_models = await get_ollama_models()

    # Merge agent configs with runtime status
    agents = {}
    for name, config in agent_configs.items():
        agents[name] = {
            **config,
            "running": agent_status.get(name, False),
            "status": "online" if agent_status.get(name, False) else "offline",
        }

    # Add any agents from runtime status not in configs
    for name, running in agent_status.items():
        if name not in agents:
            agents[name] = {
                "name": name,
                "model": "unknown",
                "description": "",
                "skills": [],
                "running": running,
                "status": "online" if running else "offline",
            }

    return web.json_response({
        "agents": agents,
        "telemetry": telemetry.get("telemetry", {}),
        "messages": telemetry.get("messages", []),
        "gpu": gpu_info,
        "ollama": {
            "url": OLLAMA_URL,
            "models": [{"name": m.get("name", ""), "size": m.get("size", 0)} for m in ollama_models],
        },
        "nats": {"url": NATS_URL, "connected": nc.is_connected if nc else False},
        "timestamp": datetime.now().isoformat(),
    })


async def handle_api_agents(request):
    """Return agent configs and status."""
    agent_configs = load_agent_configs()
    agent_status = await get_agent_process_status()
    agents = {}
    for name, config in agent_configs.items():
        agents[name] = {
            **config,
            "running": agent_status.get(name, False),
        }
    return web.json_response({"agents": agents})


async def handle_api_gpu(request):
    return web.json_response(get_gpu_info())


async def handle_api_ollama_models(request):
    """List Ollama models."""
    models = await get_ollama_models()
    return web.json_response({"models": models})


async def handle_api_ollama_pull(request):
    """Pull an Ollama model."""
    try:
        body = await request.json()
        model = body.get("model", "")
        if not model:
            return web.json_response({"error": "model name required"}, status=400)

        # Pull in background
        async def pull_model():
            proc = await asyncio.create_subprocess_exec(
                "ollama", "pull", model,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            await proc.communicate()

        asyncio.create_task(pull_model())
        return web.json_response({"status": "pulling", "model": model})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def handle_api_ollama_delete(request):
    """Delete an Ollama model."""
    try:
        body = await request.json()
        model = body.get("model", "")
        if not model:
            return web.json_response({"error": "model name required"}, status=400)
        proc = await asyncio.create_subprocess_exec(
            "ollama", "rm", model,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await proc.communicate()
        return web.json_response({"status": "deleted", "model": model, "output": stdout.decode()})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def handle_send(request):
    try:
        body = await request.json()
        agent = body.get("agent", "proxy")
        command = body.get("command", "ping")
        args = body.get("args", {})

        nats = await get_nats()
        safe_command = command.replace(" ", ".")
        if not safe_command:
            safe_command = "ping"
        subject = f"agnetic.agent.{agent}.command.{safe_command}"
        reply = f"agnetic.reply.{datetime.now().timestamp()}"
        sub = await nats.subscribe(reply, max_msgs=1)

        await nats.publish(subject, json.dumps({
            "command": command,
            "args": args,
            "reply_to": reply,
        }).encode())

        try:
            msg = await sub.next_msg(timeout=120)
            result = json.loads(msg.data.decode())
            return web.json_response(result)
        except asyncio.TimeoutError:
            return web.json_response({"error": "timeout", "response": "Agent did not respond in 120s"})
    except Exception as e:
        return web.json_response({"error": str(e)})


async def handle_logs(request):
    agent = request.query.get("agent", "proxy")
    log_file = PROJECT_DIR / "logs" / f"agents-{agent}.log"
    if not log_file.exists():
        log_file = PROJECT_DIR / "logs" / f"{agent}.log"
    try:
        lines = log_file.read_text().splitlines()[-100:]
        return web.json_response({"agent": agent, "lines": lines})
    except (FileNotFoundError, IOError):
        return web.json_response({"agent": agent, "lines": ["No log file found"]})


async def handle_history(request):
    agent = request.query.get("agent", "")
    limit = int(request.query.get("limit", "50"))
    results = []
    for f in sorted(HISTORY_DIR.glob("*.jsonl"), reverse=True)[:3]:
        if not f.exists():
            continue
        with open(f) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if agent and agent not in entry.get("subject", ""):
                        continue
                    results.append(entry)
                    if len(results) >= limit:
                        break
                except json.JSONDecodeError:
                    continue
            if len(results) >= limit:
                break
    return web.json_response({"messages": results, "total": len(results)})


async def stream_ollama(model, messages, on_token=None):
    """Stream a response from Ollama, yielding tokens via callback."""
    import aiohttp as _aiohttp
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
    }
    full_response = ""
    async with _aiohttp.ClientSession() as session:
        async with session.post(f"{OLLAMA_URL}/api/chat", json=payload) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"Ollama error {resp.status}: {body}")
            async for line in resp.content:
                line = line.decode().strip()
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue
                token = chunk.get("message", {}).get("content", "")
                if token:
                    full_response += token
                    if on_token:
                        await on_token(token)
                if chunk.get("done"):
                    break
    return full_response


TOOL_DEFINITIONS = {
    "shell": {
        "description": "Execute a shell command and return output",
        "params": ["command"],
    },
    "read_file": {
        "description": "Read a file from disk",
        "params": ["path"],
    },
    "write_file": {
        "description": "Write content to a file",
        "params": ["path", "content"],
    },
    "list_dir": {
        "description": "List directory contents",
        "params": ["path"],
    },
}


def build_system_prompt(agent_name):
    """Build a system prompt for the given agent."""
    tool_desc = "\n".join(
        f"- {name}: {info['description']} (params: {', '.join(info['params'])})"
        for name, info in TOOL_DEFINITIONS.items()
    )
    return (
        f"You are {agent_name}, an AI agent in the Starship OS system.\n"
        f"You have access to these tools:\n{tool_desc}\n\n"
        "To use a tool, output a JSON block on its own line:\n"
        '{"tool": "shell", "args": {"command": "ls -la"}}\n\n'
        "After receiving tool results, provide your final response.\n"
        "Keep responses concise and helpful."
    )


def extract_tool_calls(text):
    """Extract JSON tool calls from agent text output."""
    calls = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                obj = json.loads(line)
                if "tool" in obj:
                    calls.append(obj)
            except json.JSONDecodeError:
                continue
    return calls


async def execute_tool(tool_name, tool_args):
    """Execute a tool locally and return the result."""
    if tool_name == "shell":
        cmd = tool_args.get("command", "echo 'no command'")
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
            return stdout.decode(errors="replace")[:4000]
        except asyncio.TimeoutError:
            return "Error: command timed out after 30s"
        except Exception as e:
            return f"Error: {e}"
    elif tool_name == "read_file":
        path = tool_args.get("path", "")
        try:
            return Path(path).read_text(errors="replace")[:4000]
        except Exception as e:
            return f"Error reading {path}: {e}"
    elif tool_name == "write_file":
        path = tool_args.get("path", "")
        content = tool_args.get("content", "")
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(content)
            return f"Written {len(content)} bytes to {path}"
        except Exception as e:
            return f"Error writing {path}: {e}"
    elif tool_name == "list_dir":
        path = tool_args.get("path", ".")
        try:
            entries = sorted(Path(path).iterdir())
            return "\n".join(
                f"{'d' if e.is_dir() else 'f'} {e.name}" for e in entries[:200]
            )
        except Exception as e:
            return f"Error listing {path}: {e}"
    else:
        return f"Unknown tool: {tool_name}"


async def process_command(agent, command, args, callbacks):
    """
    Run the agent loop: send prompt to Ollama, parse tool calls, execute them,
    feed results back, repeat until the agent produces a final text response.

    callbacks dict keys: on_tool_start, on_tool_complete, on_step, on_token, on_response, on_error
    """
    on_tool_start = callbacks.get("on_tool_start", lambda **kw: asyncio.sleep(0))
    on_tool_complete = callbacks.get("on_tool_complete", lambda **kw: asyncio.sleep(0))
    on_step = callbacks.get("on_step", lambda **kw: asyncio.sleep(0))
    on_token = callbacks.get("on_token", lambda **kw: asyncio.sleep(0))
    on_response = callbacks.get("on_response", lambda **kw: asyncio.sleep(0))
    on_error = callbacks.get("on_error", lambda **kw: asyncio.sleep(0))

    model_map = {
        "proxy": "qwen2.5:7b",
        "romi": "qwen2.5:7b",
        "ergo": "jeffgreen311/eve-v2-unleashed-qwen3.5-8b-liberated-4b-4b-merged",
    }
    model = model_map.get(agent, "qwen2.5:7b")

    user_content = command
    if args:
        user_content += " " + json.dumps(args)

    messages = [
        {"role": "system", "content": build_system_prompt(agent)},
        {"role": "user", "content": user_content},
    ]

    max_iterations = 5
    for step_num in range(1, max_iterations + 1):
        await on_step(step=step_num, max_steps=max_iterations)

        try:
            full_text = await stream_ollama(
                model, messages, on_token=on_token
            )
        except Exception as e:
            await on_error(error=str(e))
            return

        tool_calls = extract_tool_calls(full_text)
        if not tool_calls:
            await on_response(text=full_text)
            return

        messages.append({"role": "assistant", "content": full_text})

        for call in tool_calls:
            tool_name = call["tool"]
            tool_args = call.get("args", {})
            await on_tool_start(tool=tool_name, args=tool_args)

            result = await execute_tool(tool_name, tool_args)
            summary = result[:200] + ("..." if len(result) > 200 else "")
            await on_tool_complete(tool=tool_name, summary=f"Output: {summary}")

            messages.append({
                "role": "user",
                "content": f"Tool result ({tool_name}):\n{result}",
            })

    await on_response(text="[max iterations reached]")


async def handle_chat_stream(request):
    """SSE endpoint: POST /api/chat/stream with {agent, command, args}."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)

    agent = body.get("agent", "proxy")
    command = body.get("command", "ping")
    args = body.get("args", {})

    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
    await response.prepare(request)

    async def send_sse(event, data):
        payload = f"event: {event}\ndata: {json.dumps(data)}\n\n"
        try:
            await response.write(payload.encode())
        except (ConnectionResetError, RuntimeError):
            pass

    async def on_tool_start(tool, args):
        await send_sse("tool_start", {"tool": tool, "args": args})

    async def on_tool_complete(tool, summary):
        await send_sse("tool_complete", {"tool": tool, "summary": summary})

    async def on_step(step, max_steps):
        await send_sse("step", {"step": step, "max_steps": max_steps})

    async def on_token(token):
        await send_sse("token", {"text": token})

    async def on_response(text):
        await send_sse("response", {"text": text})

    async def on_error(error):
        await send_sse("error", {"error": error})

    await process_command(
        agent, command, args,
        callbacks={
            "on_tool_start": on_tool_start,
            "on_tool_complete": on_tool_complete,
            "on_step": on_step,
            "on_token": on_token,
            "on_response": on_response,
            "on_error": on_error,
        },
    )

    await send_sse("done", {"id": str(uuid.uuid4())})
    await response.write_eof()
    return response


async def handle_log_search(request):
    """GET /api/logs/search?q=error&source=proxy&level=ERROR&since=1h&limit=50"""
    query = request.query.get("q", "")
    source = request.query.get("source", "")
    level = request.query.get("level", "")
    since = request.query.get("since", "")
    limit = min(int(request.query.get("limit", "100")), 1000)
    try:
        sys.path.insert(0, str(PROJECT_DIR / "services"))
        from log_aggregator import search as db_search
        results = db_search(query=query, level=level, source=source, since=since, limit=limit)
        return web.json_response({"results": results, "total": len(results)})
    except ImportError:
        return web.json_response({"error": "log_aggregator not available", "results": [], "total": 0}, status=503)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def handle_log_stats(request):
    """GET /api/logs/stats"""
    try:
        sys.path.insert(0, str(PROJECT_DIR / "services"))
        from log_aggregator import stats as db_stats
        return web.json_response(db_stats())
    except ImportError:
        return web.json_response({"error": "log_aggregator not available"}, status=503)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


MARKETPLACE_DIR = PROJECT_DIR / "skills"
MARKETPLACE_STATE = Path("/tmp/agnetic-marketplace.json")
MARKETPLACE_HISTORY = Path("/tmp/agnetic-marketplace-history.jsonl")

SKILL_SOURCES = {
    "hermes": "Hermes Registry",
    "skillssh": "skills.sh",
    "github": "GitHub",
}

CATEGORIES = [
    "devops", "security", "data", "productivity", "monitoring",
    "automation", "communication", "analytics", "infrastructure", "ai",
]

MOCK_SKILLS = [
    {"id": "k8s-deploy", "name": "k8s-deploy", "description": "Kubernetes deployment automation", "source": "hermes", "version": "1.3.0", "category": "devops", "author": "hermes-core", "downloads": 1240, "security": "safe", "installed": False},
    {"id": "vault-secrets", "name": "vault-secrets", "description": "HashiCorp Vault secrets manager", "source": "hermes", "version": "2.1.0", "category": "security", "author": "hermes-core", "downloads": 890, "security": "safe", "installed": False},
    {"id": "log-analyzer", "name": "log-analyzer", "description": "Intelligent log analysis and alerting", "source": "skillssh", "version": "0.9.4", "category": "monitoring", "author": "sysadmin-pro", "downloads": 2100, "security": "safe", "installed": False},
    {"id": "cron-master", "name": "cron-master", "description": "Advanced cron job management", "source": "skillssh", "version": "1.0.2", "category": "automation", "author": "devops-hub", "downloads": 560, "security": "safe", "installed": False},
    {"id": "net-scanner", "name": "net-scanner", "description": "Network discovery and port scanning", "source": "github", "version": "3.0.1", "category": "security", "author": "net-tools", "downloads": 3200, "security": "warning", "installed": False},
    {"id": "backup-pro", "name": "backup-pro", "description": "Automated backup and restore", "source": "hermes", "version": "1.1.0", "category": "infrastructure", "author": "hermes-core", "downloads": 780, "security": "safe", "installed": False},
    {"id": "slack-bridge", "name": "slack-bridge", "description": "Slack integration for agent notifications", "source": "skillssh", "version": "2.0.0", "category": "communication", "author": "chat-ops", "downloads": 1500, "security": "safe", "installed": False},
    {"id": "db-migrate", "name": "db-migrate", "description": "Database schema migration tool", "source": "github", "version": "0.8.3", "category": "data", "author": "data-forge", "downloads": 420, "security": "warning", "installed": False},
    {"id": "gpu-monitor", "name": "gpu-monitor", "description": "GPU utilization tracking and alerts", "source": "hermes", "version": "1.0.0", "category": "monitoring", "author": "hermes-core", "downloads": 950, "security": "safe", "installed": False},
    {"id": "ml-pipeline", "name": "ml-pipeline", "description": "ML training pipeline orchestrator", "source": "github", "version": "0.5.2", "category": "ai", "author": "ml-ops", "downloads": 670, "security": "dangerous", "installed": False},
    {"id": "dns-manager", "name": "dns-manager", "description": "DNS zone and record management", "source": "skillssh", "version": "1.2.0", "category": "infrastructure", "author": "net-tools", "downloads": 340, "security": "safe", "installed": False},
    {"id": "report-gen", "name": "report-gen", "description": "Automated report generation from data", "source": "hermes", "version": "1.4.0", "category": "productivity", "author": "hermes-core", "downloads": 1800, "security": "safe", "installed": False},
]


def _load_marketplace_state():
    if MARKETPLACE_STATE.exists():
        try:
            return json.loads(MARKETPLACE_STATE.read_text())
        except (json.JSONDecodeError, IOError):
            pass
    installed = {}
    for f in MARKETPLACE_DIR.glob("*/SKILL.md"):
        skill_id = f.parent.name
        installed[skill_id] = {
            "id": skill_id,
            "name": skill_id,
            "version": "1.0.0",
            "source": "hermes",
            "installed_at": datetime.now().isoformat(),
            "status": "active",
            "security": "safe",
        }
    _save_marketplace_state({"installed": installed})
    return {"installed": installed}


def _save_marketplace_state(state):
    MARKETPLACE_STATE.write_text(json.dumps(state, indent=2))


def _append_history(entry):
    entry["timestamp"] = datetime.now().isoformat()
    MARKETPLACE_HISTORY.parent.mkdir(parents=True, exist_ok=True)
    with open(MARKETPLACE_HISTORY, "a") as f:
        f.write(json.dumps(entry) + "\n")


def _scan_skill(skill_id):
    import random
    score = random.random()
    if score < 0.65:
        return {"level": "safe", "score": round(score * 100, 1), "checks": ["no_network_abuse", "no_filesystem_write", "no_privilege_escalation"], "message": "No security issues detected"}
    elif score < 0.9:
        return {"level": "warning", "score": round(score * 100, 1), "checks": ["no_network_abuse", "filesystem_write_detected", "no_privilege_escalation"], "message": "Uses filesystem writes — review recommended"}
    else:
        return {"level": "dangerous", "score": round(score * 100, 1), "checks": ["network_abuse_risk", "filesystem_write_detected", "privilege_escalation_risk"], "message": "Potentially dangerous — requires manual review"}


async def handle_marketplace_search(request):
    q = request.query.get("q", "").lower()
    source = request.query.get("source", "all")
    category = request.query.get("category", "all")
    results = MOCK_SKILLS[:]
    state = _load_marketplace_state()
    installed_ids = set(state.get("installed", {}).keys())
    if q:
        results = [s for s in results if q in s["name"].lower() or q in s["description"].lower() or q in s.get("category", "").lower() or q in s.get("author", "").lower()]
    if source != "all":
        results = [s for s in results if s["source"] == source]
    if category != "all":
        results = [s for s in results if s["category"] == category]
    for s in results:
        s["installed"] = s["id"] in installed_ids
    results.sort(key=lambda s: s["downloads"], reverse=True)
    return web.json_response({"skills": results, "total": len(results)})


async def handle_marketplace_installed(request):
    state = _load_marketplace_state()
    installed = state.get("installed", {})
    result = []
    for skill_id, info in installed.items():
        entry = next((s for s in MOCK_SKILLS if s["id"] == skill_id), None)
        item = {**info}
        if entry:
            item["description"] = entry["description"]
            item["category"] = entry["category"]
            item["author"] = entry["author"]
            item["downloads"] = entry["downloads"]
            item["latest_version"] = entry["version"]
            item["has_update"] = entry["version"] != info.get("version", "1.0.0")
        result.append(item)
    return web.json_response({"installed": result, "total": len(result)})


async def handle_marketplace_install(request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    skill_id = body.get("skill_id", "")
    if not skill_id:
        return web.json_response({"error": "skill_id required"}, status=400)
    skill_info = next((s for s in MOCK_SKILLS if s["id"] == skill_id), None)
    if not skill_info:
        return web.json_response({"error": f"skill '{skill_id}' not found"}, status=404)
    scan_result = _scan_skill(skill_id)
    state = _load_marketplace_state()
    installed = state.get("installed", {})
    if skill_id in installed:
        return web.json_response({"error": f"skill '{skill_id}' already installed"}, status=409)
    skill_dir = MARKETPLACE_DIR / skill_id
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(f"# {skill_info['name']}\n\n{skill_info['description']}\n\n## Security\nScan level: {scan_result['level']}\n")
    installed[skill_id] = {
        "id": skill_id,
        "name": skill_info["name"],
        "version": skill_info["version"],
        "source": skill_info["source"],
        "installed_at": datetime.now().isoformat(),
        "status": "active",
        "security": scan_result["level"],
        "security_detail": scan_result,
    }
    state["installed"] = installed
    _save_marketplace_state(state)
    _append_history({"action": "install", "skill_id": skill_id, "name": skill_info["name"], "source": skill_info["source"], "version": skill_info["version"], "security": scan_result["level"]})
    return web.json_response({"status": "installed", "skill": installed[skill_id]})


async def handle_marketplace_remove(request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    skill_id = body.get("skill_id", "")
    if not skill_id:
        return web.json_response({"error": "skill_id required"}, status=400)
    state = _load_marketplace_state()
    installed = state.get("installed", {})
    if skill_id not in installed:
        return web.json_response({"error": f"skill '{skill_id}' not installed"}, status=404)
    skill_info = installed.pop(skill_id)
    state["installed"] = installed
    _save_marketplace_state(state)
    skill_dir = MARKETPLACE_DIR / skill_id
    if skill_dir.exists():
        import shutil
        shutil.rmtree(skill_dir, ignore_errors=True)
    _append_history({"action": "remove", "skill_id": skill_id, "name": skill_info.get("name", skill_id), "source": skill_info.get("source", "unknown"), "version": skill_info.get("version", "?")})
    return web.json_response({"status": "removed", "skill_id": skill_id})


async def handle_marketplace_scan(request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    skill_id = body.get("skill_id", "")
    if not skill_id:
        return web.json_response({"error": "skill_id required"}, status=400)
    scan_result = _scan_skill(skill_id)
    _append_history({"action": "scan", "skill_id": skill_id, "security": scan_result["level"]})
    return web.json_response({"skill_id": skill_id, "scan": scan_result})


async def handle_marketplace_history(request):
    limit = min(int(request.query.get("limit", "50")), 500)
    entries = []
    if MARKETPLACE_HISTORY.exists():
        try:
            lines = MARKETPLACE_HISTORY.read_text().splitlines()
            for line in reversed(lines[-limit:]):
                if line.strip():
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except IOError:
            pass
    return web.json_response({"history": entries, "total": len(entries)})


async def handle_marketplace_page(request):
    mp_html = PROJECT_DIR / "dashboard" / "marketplace.html"
    if mp_html.exists():
        return web.Response(text=mp_html.read_text(), content_type="text/html")
    return web.Response(text="<h1>Marketplace not found</h1>", content_type="text/html")


async def handle_health(request):
    agent_status = await get_agent_process_status()
    nats_ok = False
    try:
        nats = await get_nats()
        nats_ok = nats.is_connected
    except Exception:
        pass

    return web.json_response({
        "status": "ok",
        "nats_connected": nats_ok,
        "agents_running": agent_status,
        "staragent_running": os.system("pgrep -x staragent > /dev/null 2>&1") == 0,
        "timestamp": datetime.now().isoformat(),
    })


app = web.Application()
app.router.add_get("/", handle_index)
app.router.add_get("/api/dashboard", handle_api_dashboard)
app.router.add_get("/api/agents", handle_api_agents)
app.router.add_get("/api/gpu", handle_api_gpu)
app.router.add_get("/api/ollama/models", handle_api_ollama_models)
app.router.add_post("/api/ollama/pull", handle_api_ollama_pull)
app.router.add_post("/api/ollama/delete", handle_api_ollama_delete)
app.router.add_get("/api/logs/search", handle_log_search)
app.router.add_get("/api/logs/stats", handle_log_stats)
app.router.add_get("/api/logs", handle_logs)
app.router.add_get("/api/history", handle_history)
app.router.add_post("/api/send", handle_send)
app.router.add_post("/api/chat/stream", handle_chat_stream)
app.router.add_get("/api/health", handle_health)

app.router.add_get("/marketplace", handle_marketplace_page)
app.router.add_get("/api/marketplace/search", handle_marketplace_search)
app.router.add_get("/api/marketplace/installed", handle_marketplace_installed)
app.router.add_post("/api/marketplace/install", handle_marketplace_install)
app.router.add_post("/api/marketplace/remove", handle_marketplace_remove)
app.router.add_post("/api/marketplace/scan", handle_marketplace_scan)
app.router.add_get("/api/marketplace/history", handle_marketplace_history)


async def cleanup(app):
    global nc
    if nc:
        await nc.close()

app.on_shutdown.append(cleanup)

if __name__ == "__main__":
    log.info("Agnetic Dashboard starting on http://0.0.0.0:%d", PORT)
    web.run_app(app, host="0.0.0.0", port=PORT)
