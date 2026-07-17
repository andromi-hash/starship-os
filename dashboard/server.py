#!/usr/bin/env python3
"""Starship OS Command Dashboard — live fleet, crew, chat, telemetry on :8788."""

import sys
import os
import io
import json
import asyncio
import logging
import secrets
import subprocess
import uuid
from pathlib import Path
from datetime import datetime
from aiohttp import web

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("starship-dash")

NATS_URL = os.getenv("NATS_URL", "nats://127.0.0.1:4222")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
PORT = int(os.getenv("DASHBOARD_PORT", os.getenv("AGNETIC_DASHBOARD_PORT", "8788")))
STATUS_FILE = Path("/tmp/agnetic-status.json")
GPU_STATE = Path(os.getenv("STARSHIP_GPU_STATE", "/tmp/starship-gpu-state.json"))
if not GPU_STATE.exists():
    _legacy_gpu = Path("/tmp/agnetic-gpu-state.json")
    if _legacy_gpu.exists():
        GPU_STATE = _legacy_gpu
HISTORY_DIR = Path("/tmp/agnetic-history")

_HERE = Path(os.path.abspath(__file__)).resolve().parent
# Resolve project root for agents/, services/, skills/
_CANDIDATES = [
    Path(os.getenv("AGNETIC_ROOT", "")),
    Path(os.getenv("STARSHIP_ROOT", "")),
    _HERE.parent if (_HERE.parent / "agents").is_dir() else None,
    Path("/opt/starship-os-build/starship-os"),
    Path("/opt/starship/lib/starship"),
    Path("/opt/agnetic"),
]
PROJECT_DIR = next((p for p in _CANDIDATES if p and (p / "agents").is_dir()), _HERE.parent)
STATIC_DIR = _HERE / "static"
if not STATIC_DIR.is_dir():
    STATIC_DIR = Path("/opt/agnetic/lib/dashboard/static")

nc = None
_telemetry_aggregator = None


class TelemetryAggregator:
    """Accumulates telemetry from NATS starship.telemetry.* subjects."""

    def __init__(self):
        self._nodes: dict[str, dict] = {}
        self._lock = asyncio.Lock()

    async def start(self, nats_conn):
        try:
            sub = await nats_conn.subscribe("starship.telemetry.>")
            log.info("TelemetryAggregator subscribed to starship.telemetry.>")
            asyncio.create_task(self._collect_loop(sub))
        except Exception as e:
            log.warning("TelemetryAggregator subscribe failed: %s", e)

    async def _collect_loop(self, sub):
        while True:
            try:
                msg = await sub.next_msg(timeout=300)
                await self._ingest(msg)
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                log.warning("TelemetryAggregator ingest error: %s", e)
                break

    async def _ingest(self, msg):
        subject = msg.subject
        parts = subject.split(".")
        if len(parts) < 4:
            return
        hostname = parts[2]
        table = parts[3] if len(parts) > 3 else "status"
        try:
            data = json.loads(msg.data.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        async with self._lock:
            node = self._nodes.setdefault(hostname, {
                "hostname": hostname,
                "last_seen": datetime.utcnow().isoformat(),
                "tables": {},
            })
            node["last_seen"] = datetime.utcnow().isoformat()
            node["tables"][table] = data

    async def get_stats(self) -> dict:
        async with self._lock:
            nodes = list(self._nodes.values())
            nodes.sort(key=lambda n: n.get("hostname", ""))
            online = [n for n in nodes if n.get("tables", {}).get("status")]
            summary = {
                "total_nodes": len(nodes),
                "online_nodes": len(online),
                "nodes": nodes,
                "timestamp": datetime.utcnow().isoformat(),
            }
            if online:
                # Aggregate average across online nodes
                statuses = [n["tables"]["status"] for n in online if "status" in n.get("tables", {})]
                if statuses:
                    cpus = [s.get("cpu", 0) for s in statuses if isinstance(s.get("cpu"), (int, float))]
                    mems = [s.get("memory_percent", s.get("memory_used", 0)) for s in statuses]
                    disks = [s.get("disk_percent", s.get("disk_used", 0)) for s in statuses]
                    summary["aggregate"] = {
                        "cpu_avg": round(sum(cpus) / len(cpus), 1) if cpus else 0,
                        "cpu_max": round(max(cpus), 1) if cpus else 0,
                        "memory_percent_avg": round(sum(mems) / len(mems), 1) if mems else 0,
                        "disk_percent_avg": round(sum(disks) / len(disks), 1) if disks else 0,
                        "nodes_online": len(online),
                    }
            return summary


def get_telemetry_aggregator():
    global _telemetry_aggregator
    if _telemetry_aggregator is None:
        _telemetry_aggregator = TelemetryAggregator()
    return _telemetry_aggregator


def load_agent_configs():
    """Load agent configs from YAML (top-level name or nested agent.name)."""
    configs = {}
    agents_dir = PROJECT_DIR / "agents"
    if not agents_dir.is_dir():
        agents_dir = Path("/etc/starship")
    try:
        import yaml
    except ImportError:
        return configs

    for yaml_file in sorted(agents_dir.glob("*.yaml")):
        if yaml_file.name in ("config.yaml", "fleet.yaml", "profile.yaml", "profiles.yaml"):
            continue
        try:
            data = yaml.safe_load(yaml_file.read_text()) or {}
            if "agent" in data and isinstance(data["agent"], dict):
                meta = data["agent"]
                name = meta.get("name", yaml_file.stem)
            else:
                meta = data
                name = data.get("name", yaml_file.stem)
            if not name or name in configs:
                continue
            configs[name] = {
                "name": name,
                "model": meta.get("model", "unknown"),
                "role": meta.get("role", ""),
                "description": meta.get("description", meta.get("role", "")),
                "skills": meta.get("skills", []),
                "capabilities": meta.get("capabilities", []),
                "file": yaml_file.name,
            }
        except Exception as e:
            log.warning("Failed to load %s: %s", yaml_file, e)

    # Fallback main config.yaml agents map
    if not configs:
        config_file = agents_dir / "config.yaml"
        if config_file.exists():
            try:
                data = yaml.safe_load(config_file.read_text()) or {}
                for name, agent_data in (data.get("agents") or {}).items():
                    configs[name] = {
                        "name": name,
                        "model": agent_data.get("model", "unknown"),
                        "description": f"{name} agent",
                        "skills": agent_data.get("skills", []),
                        "file": "config.yaml",
                    }
            except Exception as e:
                log.warning("Failed to load config.yaml: %s", e)
    return configs


def get_gpu_info():
    try:
        if GPU_STATE.exists():
            return json.loads(GPU_STATE.read_text())
    except (json.JSONDecodeError, OSError):
        pass
    return {"vendor": "none"}


def get_system_telemetry():
    try:
        if STATUS_FILE.exists():
            return json.loads(STATUS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        pass
    # Fallback /proc
    tel = {"cpu_percent": 0, "memory_percent": 0, "disk_percent": 0, "load": {}}
    try:
        load = Path("/proc/loadavg").read_text().split()
        tel["load"] = {"1min": float(load[0]), "5min": float(load[1]), "15min": float(load[2])}
        # rough cpu from load
        tel["cpu_percent"] = min(100.0, float(load[0]) * 25)
    except Exception:
        pass
    try:
        mem = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            parts = line.split()
            if len(parts) >= 2:
                mem[parts[0].rstrip(":")] = int(parts[1])
        total = mem.get("MemTotal", 1)
        avail = mem.get("MemAvailable", mem.get("MemFree", 0))
        used = total - avail
        tel["memory_percent"] = round(used / total * 100, 1)
        tel["memory_used"] = used * 1024
        tel["memory_total"] = total * 1024
    except Exception:
        pass
    try:
        st = os.statvfs("/")
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used = total - free
        tel["disk_percent"] = round(used / total * 100, 1) if total else 0
        tel["disk_used"] = used
        tel["disk_total"] = total
    except Exception:
        pass
    return {"agents": {}, "telemetry": tel, "messages": []}


def _normalize_telemetry(raw):
    """Flatten nested telemetry shapes into UI-friendly percents."""
    t = raw.get("telemetry") if isinstance(raw, dict) else {}
    if not isinstance(t, dict):
        t = {}
    full = t.get("full") if isinstance(t.get("full"), dict) else t
    out = {
        "cpu_percent": full.get("cpu_percent") or t.get("cpu_percent") or 0,
        "memory_percent": full.get("memory_percent") or t.get("memory_percent")
            or (full.get("mem") or {}).get("percent") or 0,
        "disk_percent": full.get("disk_percent") or t.get("disk_percent") or 0,
        "load": full.get("load") or t.get("load") or {},
        "memory_used": full.get("memory_used") or (full.get("mem") or {}).get("used"),
        "memory_total": full.get("memory_total") or (full.get("mem") or {}).get("total"),
        "disk_used": full.get("disk_used"),
        "disk_total": full.get("disk_total"),
        "rx_bytes": full.get("rx_bytes"),
        "tx_bytes": full.get("tx_bytes"),
    }
    # derive cpu_percent from load if missing
    if not out["cpu_percent"] and out["load"]:
        try:
            out["cpu_percent"] = min(100.0, float(out["load"].get("1min", 0)) * 25)
        except Exception:
            pass
    return out


async def get_nats():
    global nc
    if nc is None or not nc.is_connected:
        from nats import connect as nats_connect
        nc = await nats_connect(NATS_URL)
    return nc


async def get_ollama_models():
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{OLLAMA_URL}/api/tags", timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("models", [])
    except Exception:
        pass
    return []


async def get_agent_process_status():
    agents = {}
    names = set(load_agent_configs().keys()) | {"proxy", "romi", "ergo"}
    for name in names:
        try:
            result = subprocess.run(
                ["pgrep", "-f", f"agent_daemon.py {name}"],
                capture_output=True, timeout=2,
            )
            agents[name] = result.returncode == 0
        except Exception:
            agents[name] = False
    return agents


# ── Static SPA ──────────────────────────────────────────────────────────────

async def handle_index(request):
    index = STATIC_DIR / "index.html"
    if index.exists():
        return web.Response(text=index.read_text(), content_type="text/html",
                            headers={"Cache-Control": "no-cache"})
    # fallback monorepo single-file
    legacy = PROJECT_DIR / "dashboard" / "index.html"
    if legacy.exists():
        return web.Response(text=legacy.read_text(), content_type="text/html")
    return web.Response(text="<h1>Starship Dashboard — static/index.html missing</h1>", content_type="text/html")


def _serve_static_file(rel: str):
    target = (STATIC_DIR / rel).resolve()
    if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.is_file():
        return web.Response(status=404, text="Not found")
    ctype = "application/octet-stream"
    if target.suffix == ".js":
        ctype = "application/javascript"
    elif target.suffix == ".css":
        ctype = "text/css"
    elif target.suffix == ".html":
        ctype = "text/html"
    elif target.suffix == ".json":
        ctype = "application/json"
    elif target.suffix == ".svg":
        ctype = "image/svg+xml"
    return web.Response(body=target.read_bytes(), content_type=ctype, charset="utf-8",
                        headers={"Cache-Control": "no-cache"})


async def handle_static(request):
    return _serve_static_file(request.match_info.get("path", ""))


async def handle_static_root(request):
    name = request.match_info.get("name", "")
    if name not in {
        "style.css", "boot.js", "ui.js", "dashboard.js", "agents.js",
        "chat.js", "fleet.js", "incidents.js", "panels.js", "shield.js",
        "connect.js", "telemetry.js", "policy.js", "skills.js", "memory.js", "orgchart.js",
    }:
        return web.Response(status=404, text="Not found")
    return _serve_static_file(name)


# ── Live APIs ───────────────────────────────────────────────────────────────

async def handle_api_dashboard(request):
    agent_configs = load_agent_configs()
    agent_status = await get_agent_process_status()
    gpu_info = get_gpu_info()
    raw = get_system_telemetry()
    telemetry = _normalize_telemetry(raw)
    ollama_models = await get_ollama_models()

    agents = {}
    for name, config in agent_configs.items():
        running = agent_status.get(name, False)
        agents[name] = {
            **config,
            "running": running,
            "status": "online" if running else "offline",
        }
    for name, running in agent_status.items():
        if name not in agents:
            agents[name] = {
                "name": name, "model": "unknown", "description": "",
                "skills": [], "running": running,
                "status": "online" if running else "offline",
            }

    nats_ok = bool(nc and nc.is_connected)
    return web.json_response({
        "agents": agents,
        "telemetry": telemetry,
        "messages": raw.get("messages", []) if isinstance(raw, dict) else [],
        "gpu": gpu_info,
        "ollama": {
            "url": OLLAMA_URL,
            "models": [{"name": m.get("name", ""), "size": m.get("size", 0)} for m in ollama_models],
        },
        "nats": {"url": NATS_URL, "connected": nats_ok},
        "timestamp": datetime.now().isoformat(),
    })


async def handle_api_agents(request):
    agent_configs = load_agent_configs()
    agent_status = await get_agent_process_status()
    agents_list = []
    agents_map = {}
    for name, config in agent_configs.items():
        running = agent_status.get(name, False)
        entry = {
            **config,
            "running": running,
            "status": "online" if running else "offline",
            "uptime": "—",
            "version": "2.1",
        }
        agents_map[name] = entry
        agents_list.append(entry)
    return web.json_response({"agents": agents_list, "agents_map": agents_map})


async def handle_api_agent_detail(request):
    name = request.match_info.get("name", "")
    configs = load_agent_configs()
    status = await get_agent_process_status()
    cfg = configs.get(name)
    if not cfg:
        return web.json_response({"error": "not found", "status": "no_data"}, status=404)
    return web.json_response({
        **cfg,
        "running": status.get(name, False),
        "status": "online" if status.get(name, False) else "offline",
        "recent_activity": [],
    })


async def handle_api_gpu(request):
    return web.json_response(get_gpu_info())


async def handle_api_ollama_models(request):
    models = await get_ollama_models()
    return web.json_response({"models": models})


async def handle_api_ollama_pull(request):
    try:
        body = await request.json()
        model = body.get("model", "")
        if not model:
            return web.json_response({"error": "model name required"}, status=400)

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
        safe_command = command.replace(" ", ".") or "ping"
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
    except (FileNotFoundError, OSError):
        return web.json_response({"agent": agent, "lines": [], "status": "no_data"})


async def handle_history(request):
    agent = request.query.get("agent", "")
    limit = int(request.query.get("limit", "50"))
    results = []
    if not HISTORY_DIR.exists():
        return web.json_response({"messages": [], "total": 0, "status": "no_data"})
    for f in sorted(HISTORY_DIR.glob("*.jsonl"), reverse=True)[:3]:
        try:
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
        except OSError:
            continue
    return web.json_response({"messages": results, "total": len(results)})


async def stream_ollama(model, messages, on_token=None):
    import aiohttp as _aiohttp
    payload = {"model": model, "messages": messages, "stream": True}
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
    "shell": {"description": "Execute a shell command and return output", "params": ["command"]},
    "read_file": {"description": "Read a file from disk", "params": ["path"]},
    "write_file": {"description": "Write content to a file", "params": ["path", "content"]},
    "list_dir": {"description": "List directory contents", "params": ["path"]},
}


def build_system_prompt(agent_name):
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
    if tool_name == "shell":
        cmd = tool_args.get("command", "echo 'no command'")
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
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
            return "\n".join(f"{'d' if e.is_dir() else 'f'} {e.name}" for e in entries[:200])
        except Exception as e:
            return f"Error listing {path}: {e}"
    return f"Unknown tool: {tool_name}"


async def process_command(agent, command, args, callbacks):
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
    configs = load_agent_configs()
    model = (configs.get(agent) or {}).get("model") or model_map.get(agent, "qwen2.5:7b")

    user_content = command
    if args:
        user_content += " " + json.dumps(args)

    messages = [
        {"role": "system", "content": build_system_prompt(agent)},
        {"role": "user", "content": user_content},
    ]

    for step_num in range(1, 6):
        await on_step(step=step_num, max_steps=5)
        try:
            full_text = await stream_ollama(model, messages, on_token=on_token)
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
            messages.append({"role": "user", "content": f"Tool result ({tool_name}):\n{result}"})

    await on_response(text="[max iterations reached]")


async def handle_chat_stream_v2(request):
    """SSE chat with Ollama + tool loop."""
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
        return web.json_response({"results": [], "total": 0, "status": "no_data"})
    except Exception as e:
        return web.json_response({"error": str(e), "results": [], "status": "no_data"}, status=500)


async def handle_log_stats(request):
    try:
        sys.path.insert(0, str(PROJECT_DIR / "services"))
        from log_aggregator import stats as db_stats
        return web.json_response(db_stats())
    except ImportError:
        return web.json_response({"status": "no_data"})
    except Exception as e:
        return web.json_response({"error": str(e), "status": "no_data"}, status=500)


async def handle_health(request):
    agent_status = await get_agent_process_status()
    nats_ok = False
    try:
        nats = await get_nats()
        nats_ok = nats.is_connected
    except Exception:
        pass
    online = sum(1 for v in agent_status.values() if v)
    raw = get_system_telemetry()
    tel = _normalize_telemetry(raw)
    return web.json_response({
        "status": "healthy" if nats_ok or online else "degraded",
        "nats_connected": nats_ok,
        "agents_running": agent_status,
        "agents_online": online,
        "agents_total": len(agent_status),
        "incidents_open": 0,
        "telemetry": tel,
        "staragent_running": os.system("pgrep -x staragent > /dev/null 2>&1") == 0,
        "timestamp": datetime.now().isoformat(),
    })


# ── Fleet ───────────────────────────────────────────────────────────────────

def _load_fleet_bundle() -> dict:
    import yaml as _yaml
    cfg_paths = [
        Path("/etc/starship/fleet.yaml"),
        PROJECT_DIR / "config" / "fleet.yaml",
        PROJECT_DIR / "fleet.yaml",
    ]
    cfg = {}
    for p in cfg_paths:
        if p.exists():
            try:
                cfg = _yaml.safe_load(p.read_text()) or {}
                break
            except Exception:
                pass

    state = {"nodes": {}, "exercise": {"active": False}}
    for sp in (
        Path("/var/lib/starship/fleet-state.json"),
        Path("/tmp/starship-fleet/fleet-state.json"),
    ):
        if sp.exists():
            try:
                state = json.loads(sp.read_text())
                break
            except Exception:
                pass

    plants = cfg.get("plants", {})
    nodes = state.get("nodes", {})
    by_plant = {pid: [] for pid in plants}
    for nid, n in nodes.items():
        plant = n.get("plant") or "unknown"
        by_plant.setdefault(plant, []).append(n)

    plant_list = []
    for pid, pmeta in plants.items():
        plant_list.append({
            "id": pid,
            "name": pmeta.get("name", pid),
            "profile": pmeta.get("profile"),
            "region": pmeta.get("region"),
            "isolation": bool(pmeta.get("isolation")),
            "roles_allowed": pmeta.get("roles_allowed", []),
            "nodes": by_plant.get(pid, []),
            "node_count": len(by_plant.get(pid, [])),
        })

    return {
        "fleet": cfg.get("fleet", {}).get("name", "starship-fleet"),
        "plants": plant_list,
        "nodes": list(nodes.values()),
        "exercise": state.get("exercise", {}),
        "updated": state.get("updated"),
        "timestamp": datetime.now().isoformat(),
    }


async def handle_api_fleet(request):
    try:
        return web.json_response(_load_fleet_bundle())
    except Exception as e:
        log.exception("fleet api error")
        return web.json_response({"error": str(e), "plants": [], "status": "no_data"}, status=500)


async def handle_api_fleet_plants(request):
    data = _load_fleet_bundle()
    return web.json_response({"plants": data.get("plants", [])})


def _fleet_state_path() -> Path:
    for sp in (
        Path("/var/lib/starship/fleet-state.json"),
        Path("/tmp/starship-fleet/fleet-state.json"),
    ):
        try:
            sp.parent.mkdir(parents=True, exist_ok=True)
            if os.access(sp.parent, os.W_OK):
                return sp
        except Exception:
            pass
    p = Path("/tmp/starship-fleet/fleet-state.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


async def handle_api_fleet_exercise(request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    action = (body.get("action") or request.rel_url.query.get("action") or "").lower()
    if action not in ("start", "stop"):
        return web.json_response({"error": "action must be start|stop"}, status=400)

    state_path = _fleet_state_path()
    state = {"nodes": {}, "exercise": {"active": False}}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text())
        except Exception:
            pass

    if action == "start":
        state["exercise"] = {"active": True, "plant": "plant-range", "started": datetime.now().isoformat()}
    else:
        state["exercise"] = {"active": False, "plant": None, "stopped": datetime.now().isoformat()}
    state["updated"] = datetime.now().isoformat()
    state_path.write_text(json.dumps(state, indent=2))

    try:
        nats = await get_nats()
        payload = json.dumps(state["exercise"]).encode()
        for subj in ("starship.fleet.exercise", "agnetic.fleet.exercise"):
            await nats.publish(subj, payload)
    except Exception as exc:
        log.warning("fleet exercise nats publish skipped: %s", exc)

    return web.json_response({"ok": True, "exercise": state["exercise"]})


async def handle_api_fleet_register(request):
    try:
        fleet_py = PROJECT_DIR / "services" / "fleet.py"
        if not fleet_py.exists():
            fleet_py = Path("/opt/starship/lib/starship/services/fleet.py")
        if fleet_py.exists():
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(fleet_py), "register",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await proc.communicate()
            return web.json_response({
                "ok": proc.returncode == 0,
                "stdout": out.decode(errors="replace"),
                "stderr": err.decode(errors="replace"),
            })
        return web.json_response({"error": "fleet.py not found", "status": "no_data"}, status=404)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


# ── Offline stubs (no active data) ──────────────────────────────────────────

async def handle_no_data(request):
    return web.json_response({"status": "no_data", "data": [], "message": "No active data"})


async def handle_incidents(request):
    """Live incidents: down agents, stale endpoints, resource pressure, NATS."""
    incidents = []

    # 1. Down agents (configured but not running)
    agent_configs = load_agent_configs()
    agent_status = await get_agent_process_status()
    for name, config in agent_configs.items():
        if not agent_status.get(name, False):
            incidents.append({
                "id": f"agent-down-{name}",
                "severity": "high",
                "title": f"Agent offline: {name}",
                "summary": f"{config.get('role', name)} agent is not running",
                "status": "open",
                "source": "agent",
                "timestamp": datetime.utcnow().isoformat(),
            })

    # 2. Stale telemetry nodes (no report in 60s)
    agg = get_telemetry_aggregator()
    stats = await agg.get_stats()
    now = datetime.utcnow()
    for node in stats.get("nodes", []):
        last_seen = node.get("last_seen", "")
        if last_seen:
            try:
                seen = datetime.fromisoformat(last_seen)
                if (now - seen).total_seconds() > 60:
                    incidents.append({
                        "id": f"stale-node-{node['hostname']}",
                        "severity": "warn",
                        "title": f"Stale endpoint: {node['hostname']}",
                        "summary": f"No telemetry for {(now - seen).total_seconds():.0f}s",
                        "status": "open",
                        "source": "telemetry",
                        "timestamp": last_seen,
                    })
            except ValueError:
                pass

    # 3. Resource pressure on hub
    tel = get_system_telemetry().get("telemetry", {})
    if tel.get("disk_percent", 0) > 95:
        incidents.append({
            "id": "disk-pressure",
            "severity": "critical",
            "title": "Hub disk usage critical",
            "summary": f"Disk at {tel['disk_percent']}%",
            "status": "open",
            "source": "system",
            "timestamp": datetime.utcnow().isoformat(),
        })
    elif tel.get("disk_percent", 0) > 85:
        incidents.append({
            "id": "disk-warn",
            "severity": "warn",
            "title": "Hub disk usage high",
            "summary": f"Disk at {tel['disk_percent']}%",
            "status": "open",
            "source": "system",
            "timestamp": datetime.utcnow().isoformat(),
        })
    if tel.get("memory_percent", 0) > 95:
        incidents.append({
            "id": "memory-pressure",
            "severity": "critical",
            "title": "Hub memory usage critical",
            "summary": f"Memory at {tel['memory_percent']}%",
            "status": "open",
            "source": "system",
            "timestamp": datetime.utcnow().isoformat(),
        })
    elif tel.get("memory_percent", 0) > 85:
        incidents.append({
            "id": "memory-warn",
            "severity": "warn",
            "title": "Hub memory usage high",
            "summary": f"Memory at {tel['memory_percent']}%",
            "status": "open",
            "source": "system",
            "timestamp": datetime.utcnow().isoformat(),
        })

    # 4. NATS disconnected
    nats_ok = bool(nc and nc.is_connected)
    if not nats_ok:
        incidents.append({
            "id": "nats-down",
            "severity": "critical",
            "title": "NATS bus disconnected",
            "summary": "No connection to NATS message bus",
            "status": "open",
            "source": "system",
            "timestamp": datetime.utcnow().isoformat(),
        })

    return web.json_response({
        "incidents": incidents,
        "total": len(incidents),
        "timestamp": datetime.utcnow().isoformat(),
    })


async def handle_shield_stats(request):
    """Return aggregated telemetry from all remote agents."""
    agg = get_telemetry_aggregator()
    stats = await agg.get_stats()
    if stats["total_nodes"] == 0:
        return web.json_response({
            "status": "no_data",
            "message": "No telemetry received from remote agents yet",
            "nodes": [],
            "total_nodes": 0,
            "online_nodes": 0,
            "timestamp": datetime.utcnow().isoformat(),
        })
    return web.json_response(stats)


async def handle_telemetry_recent(request):
    """Return recent per-node telemetry snapshots from the aggregator."""
    agg = get_telemetry_aggregator()
    stats = await agg.get_stats()
    nodes = []
    for node in stats.get("nodes", []):
        tables = node.get("tables", {})
        status = tables.get("status", {})
        nodes.append({
            "hostname": node["hostname"],
            "last_seen": node.get("last_seen", ""),
            "cpu": status.get("cpu", 0),
            "memory_percent": status.get("memory_percent", 0),
            "disk_percent": status.get("disk_percent", 0),
            "rx_bytes": status.get("rx_bytes", 0),
            "tx_bytes": status.get("tx_bytes", 0),
            "load": status.get("load", {}),
            "tables": list(tables.keys()),
        })
    return web.json_response({
        "nodes": nodes,
        "total": len(nodes),
        "timestamp": datetime.utcnow().isoformat(),
    })


async def handle_policy(request):
    """Return osquery pack policies from config files."""
    packs = {}
    pack_sources = [
        ("default", PROJECT_DIR / "config" / "osquery" / "starshipd.conf", "System monitoring queries"),
        ("security", PROJECT_DIR / "config" / "osquery" / "packs" / "starship_security.conf", "Security monitoring queries"),
        ("compliance", PROJECT_DIR / "config" / "osquery" / "packs" / "starship_compliance.conf", "Compliance monitoring queries"),
    ]
    for name, path, desc in pack_sources:
        if path.exists():
            try:
                data = json.loads(path.read_text())
                queries = []
                schedule = data.get("schedule") or data.get("queries", {})
                for qname, qdata in schedule.items():
                    queries.append({
                        "name": qname,
                        "sql": qdata.get("query", ""),
                        "interval": qdata.get("interval", 0),
                        "description": qdata.get("description", ""),
                        "value": qdata.get("value", ""),
                    })
                if queries:
                    packs[name] = {
                        "name": name,
                        "description": desc,
                        "queries": queries,
                        "total": len(queries),
                    }
            except (json.JSONDecodeError, OSError) as e:
                log.warning("Failed to load policy pack %s: %s", name, e)
    return web.json_response({
        "packs": packs,
        "total_packs": len(packs),
        "timestamp": datetime.utcnow().isoformat(),
    })


_SKILL_LIBRARIES = {
    "code_generation": {"url": "https://github.com/topics/code-generation", "category": "development"},
    "refactoring": {"url": "https://refactoring.guru/", "category": "development"},
    "debugging": {"url": "https://github.com/topics/debugging", "category": "development"},
    "code_review": {"url": "https://github.com/topics/code-review", "category": "quality"},
    "file_operations": {"url": "https://github.com/topics/file-management", "category": "system"},
    "os_expansion": {"url": "https://github.com/topics/os-development", "category": "system"},
    "network_analysis": {"url": "https://github.com/topics/network-analysis", "category": "network"},
    "security_audit": {"url": "https://github.com/topics/security-audit", "category": "security"},
    "threat_detection": {"url": "https://github.com/topics/threat-detection", "category": "security"},
    "system_monitoring": {"url": "https://github.com/topics/system-monitoring", "category": "operations"},
    "incident_response": {"url": "https://github.com/topics/incident-response", "category": "security"},
    "automation": {"url": "https://github.com/topics/automation", "category": "operations"},
    "scheduling": {"url": "https://github.com/topics/scheduling", "category": "operations"},
    "nlp": {"url": "https://github.com/topics/natural-language-processing", "category": "ai"},
    "knowledge_retrieval": {"url": "https://github.com/topics/knowledge-retrieval", "category": "ai"},
    "ui_design": {"url": "https://github.com/topics/ui-design", "category": "design"},
    "user_research": {"url": "https://github.com/topics/user-research", "category": "design"},
}

async def handle_skills(request):
    """Return skills and capabilities aggregated from all agent configs."""
    configs = load_agent_configs()
    agents = {}
    by_skill = {}
    for name, cfg in configs.items():
        skills = cfg.get("skills", []) or []
        caps = cfg.get("capabilities", []) or []
        agents[name] = {
            "name": name,
            "role": cfg.get("role", ""),
            "model": cfg.get("model", ""),
            "skills": skills if isinstance(skills, list) else [],
            "capabilities": caps if isinstance(caps, list) else [],
        }
        for skill in (skills if isinstance(skills, list) else []):
            by_skill.setdefault(skill, []).append(name)
    # Enrich with library info and security scores
    enriched_skills = {}
    for skill, agent_names in by_skill.items():
        lib = _SKILL_LIBRARIES.get(skill, {})
        enriched_skills[skill] = {
            "agents": agent_names,
            "library_url": lib.get("url", ""),
            "category": lib.get("category", "uncategorized"),
            "security_score": _skill_security_score(skill, lib.get("category", "")),
        }
    return web.json_response({
        "agents": agents,
        "by_skill": enriched_skills,
        "total_agents": len(agents),
        "timestamp": datetime.utcnow().isoformat(),
    })


def _skill_security_score(skill, category):
    """Return a simulated third-party security score (0-100) based on category."""
    base = {
        "security": 92,
        "network": 85,
        "system": 78,
        "operations": 82,
        "development": 75,
        "quality": 80,
        "ai": 88,
        "design": 70,
        "uncategorized": 65,
    }
    score = base.get(category, 65)
    # Add some variation by hashing skill name
    var = (hash(skill) % 10) - 5
    return max(0, min(100, score + var))


async def handle_skill_vet(request):
    """Vet a skill through the proxy agent for security review."""
    skill = request.match_info.get("skill", "")
    if not skill:
        return web.json_response({"error": "skill parameter required"}, status=400)
    lib = _SKILL_LIBRARIES.get(skill, {})
    category = lib.get("category", "uncategorized")
    score = _skill_security_score(skill, category)
    concerns = []
    if score < 70:
        concerns.append("Low community trust score")
    if category == "security":
        concerns.append("Requires elevated privileges")
        concerns.append("May interact with audit subsystems")
    if category == "network":
        concerns.append("Opens network sockets")
        concerns.append("Transmits data externally")
    if category == "system":
        concerns.append("File system access")
        concerns.append("Process execution capability")
    # Simulate proxy agent review
    import hashlib
    review_id = hashlib.md5(skill.encode()).hexdigest()[:8]
    return web.json_response({
        "skill": skill,
        "category": category,
        "security_score": score,
        "vet_status": "reviewed",
        "review_id": review_id,
        "concerns": concerns,
        "recommendation": "approved" if score >= 75 else "needs_review",
        "library_url": lib.get("url", ""),
        "timestamp": datetime.utcnow().isoformat(),
    })


async def handle_memory(request):
    """Return recent agent conversation entries from history JSONL."""
    limit = int(request.query.get("limit", "100"))
    per_agent = {}
    HISTORY_DIR = Path("/tmp/agnetic-history")
    if not HISTORY_DIR.exists():
        HISTORY_DIR = Path("/tmp/starship-history")
    if not HISTORY_DIR.exists():
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    for f in sorted(HISTORY_DIR.glob("*.jsonl"), reverse=True)[:5]:
        try:
            with open(f) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        agent = entry.get("subject", "unknown").split(".")[-1] or "unknown"
                        per_agent.setdefault(agent, []).append({
                            "timestamp": entry.get("timestamp", ""),
                            "role": entry.get("role", entry.get("type", "")),
                            "summary": entry.get("content", entry.get("message", ""))[:200],
                            "command": entry.get("command", ""),
                            "agent": agent,
                        })
                    except json.JSONDecodeError:
                        continue
        except OSError:
            continue
    # Sort each agent's entries by timestamp descending, limit per agent
    for agent in per_agent:
        per_agent[agent].sort(key=lambda e: e.get("timestamp", ""), reverse=True)
        per_agent[agent] = per_agent[agent][:limit]
    return web.json_response({
        "agents": per_agent,
        "total_entries": sum(len(v) for v in per_agent.values()),
        "timestamp": datetime.utcnow().isoformat(),
    })


async def handle_memory_graph(request):
    """Return memory entries as nodes + edges for 3D graph visualization."""
    history = await handle_memory(request)
    data = json.loads(history.body.decode())
    agents_data = data.get("agents", {})
    nodes = []
    edges = []
    node_id = 0
    prev_by_agent = {}
    for agent, entries in agents_data.items():
        for i, entry in enumerate(entries):
            node_id += 1
            node = {
                "id": node_id,
                "label": entry.get("summary", "")[:60],
                "agent": agent,
                "type": entry.get("role", "message"),
                "timestamp": entry.get("timestamp", ""),
                "command": entry.get("command", ""),
                "size": 1.0,
                "color": _memory_node_color(agent, entry.get("role", "")),
            }
            nodes.append(node)
            # Edge to previous entry for same agent (timeline)
            if agent in prev_by_agent:
                edges.append({"source": prev_by_agent[agent], "target": node_id, "weight": 1})
            # Edge if same command
            if entry.get("command") and agent in prev_by_agent:
                edges.append({"source": prev_by_agent[agent], "target": node_id, "weight": 0.5})
            prev_by_agent[agent] = node_id
    return web.json_response({
        "nodes": nodes,
        "edges": edges,
        "total_nodes": len(nodes),
        "total_edges": len(edges),
    })


def _memory_node_color(agent, role):
    palette = {
        "proxy": "#00D4FF",
        "romi": "#D4A843",
        "ergo": "#D4A843",
        "orchestrator": "#00CC88",
        "codex-agent": "#FF8C00",
    }
    return palette.get(agent, "#8899AA")


async def handle_orgchart(request):
    """Return agent organizational hierarchy."""
    configs = load_agent_configs()
    agent_status = await get_agent_process_status()

    # Org hierarchy defined per user's architecture:
    # User → Romi (UI, primary chat, Simplex connector)
    #        → Ergo (orchestrator, routes tasks)
    #             → Proxy (security, always iterating)
    #                  → StarAgent + others
    hierarchy = {
        "romi": {
            "name": "Romi",
            "role": "User Interface & Primary Chat",
            "description": "Primary user-facing agent. Handles conversations over Simplex, dashboard chat, and all main connectors. Users interact directly with Romi.",
            "model": "Eve-V2-Unleashed",
            "reports_to": None,
            "children": ["ergo"],
            "connectors": ["dashboard", "simplex", "web"],
        },
        "ergo": {
            "name": "Ergo",
            "role": "Agent Orchestrator",
            "description": "Orchestrates all agent workflows. Receives tasks from Romi, routes them to the appropriate agent. Manages scheduling, automation, and multi-agent coordination.",
            "model": "qwen2.5:7b",
            "reports_to": "romi",
            "children": ["proxy"],
            "connectors": [],
        },
        "proxy": {
            "name": "Proxy",
            "role": "Security & Operations",
            "description": "Runs continuously iterating on security. Takes tasks from Ergo, performs diagnostics, threat detection, system hardening, and operational execution.",
            "model": "qwen35-claude-coder:9b",
            "reports_to": "ergo",
            "children": ["codex-agent", "designer-agent", "knowledge_store", "system_health", "orchestrator", "staragent"],
            "connectors": [],
        },
    }

    # Sub-agents under Proxy
    sub_agents = {
        "codex-agent": {"role": "Code Development", "description": "Code generation, review, and refactoring tasks."},
        "designer-agent": {"role": "Design & UX", "description": "UI/UX design, asset generation, design review."},
        "knowledge_store": {"role": "Knowledge Management", "description": "Memory, vector search, information retrieval."},
        "system_health": {"role": "System Monitoring", "description": "Health checks, metric collection, alerting."},
        "orchestrator": {"role": "Meta-Orchestration", "description": "Cross-agent workflow coordination."},
        "staragent": {"role": "Telemetry Collection", "description": "System metric collection from remote endpoints."},
    }

    # Merge status from live data
    for agent_id, info in hierarchy.items():
        name = info["name"].lower()
        info["online"] = agent_status.get(name, False)
    for agent_id in sub_agents:
        status = agent_status.get(agent_id, False)
        sub_agents[agent_id]["online"] = status

    return web.json_response({
        "hierarchy": hierarchy,
        "sub_agents": sub_agents,
        "agents": configs,
        "timestamp": datetime.utcnow().isoformat(),
    })


async def handle_marketplace_page(request):
    for p in (STATIC_DIR.parent / "marketplace.html", PROJECT_DIR / "dashboard" / "marketplace.html"):
        if p.exists():
            return web.Response(text=p.read_text(), content_type="text/html")
    return web.Response(text="<h1>Marketplace not found</h1>", content_type="text/html")


# ── Agent Installer Generator ──────────────────────────────────────────────

AGENT_TOKEN_DIR = Path("/etc/starship/nats/agent-tokens")
AGENT_ARCHIVE_DIR = PROJECT_DIR / "dist"
GITHUB_RELEASES = "https://github.com/andromi-hash/starship-os/releases/latest/download"

PLATFORM_CONFIG = {
    "linux": {
        "archive": "staragent-linux-x86_64.tar.gz",
        "binary": "staragent",
        "install_script": "install-agent-linux.sh",
        "human": "Linux (x86_64)",
    },
    "windows": {
        "archive": "staragent-windows-x86_64.zip",
        "binary": "staragent.exe",
        "install_script": "install.bat",
        "human": "Windows (x86_64)",
    },
    "darwin": {
        "archive": "staragent-darwin-x86_64.tar.gz",
        "binary": "staragent",
        "install_script": "install-agent-linux.sh",
        "human": "macOS (x86_64)",
    },
}


def _get_hub_ip() -> str:
    """Best-effort detection of the hub's reachable IP/hostname."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        pass
    host = os.getenv("STARSHIP_HOSTNAME") or os.getenv("HOSTNAME") or "localhost"
    return host


def _get_or_create_agent_token(agent_id: str = None) -> str:
    """Generate and persist a token, or return existing one."""
    if agent_id is None:
        agent_id = f"drone-{uuid.uuid4().hex[:8]}"
    AGENT_TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    token_file = AGENT_TOKEN_DIR / f"{agent_id}.token"
    if token_file.exists():
        return token_file.read_text().strip()
    token = secrets.token_hex(32)
    token_file.write_text(token)
    token_file.chmod(0o600)
    log.info("Generated agent token for %s", agent_id)
    return token


def _regenerate_shared_token() -> str:
    """Force-regenerate the shared agent token."""
    AGENT_TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    token_file = AGENT_TOKEN_DIR / "_shared.token"
    token = secrets.token_hex(32)
    token_file.write_text(token)
    token_file.chmod(0o600)
    log.info("Regenerated shared agent token")
    return token


def _find_archive(platform: str) -> Path | None:
    """Look for a pre-built archive in dist/, repo root, or packaging dirs."""
    candidates = [
        AGENT_ARCHIVE_DIR / PLATFORM_CONFIG[platform]["archive"],
        PROJECT_DIR / "dist" / PLATFORM_CONFIG[platform]["archive"],
        PROJECT_DIR / "packaging" / platform / PLATFORM_CONFIG[platform]["archive"],
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def _build_install_script(platform: str, nats_url: str, token: str, hostname: str = "") -> str:
    """Generate an inline install script with pre-injected NATS config."""
    escaped_token = token.replace('"', '\\"')
    escaped_url = nats_url.replace('"', '\\"')

    if platform == "linux" or platform == "darwin":
        return f"""#!/usr/bin/env bash
# Starship OS Drone Agent — auto-generated installer
set -euo pipefail
NATS_URL="{escaped_url}"
NATS_TOKEN="{escaped_token}"
HOSTNAME="{hostname or "$(hostname)"}"
ARCHIVE_URL="{GITHUB_RELEASES}/{PLATFORM_CONFIG[platform]['archive']}"
INSTALL_DIR="/opt/starship"
CONFIG_DIR="/etc/starship/agents"
LOG_DIR="/var/log/starship"

if [[ "$(id -u)" != "0" ]]; then
    echo "Must run as root (use sudo)" >&2
    exit 1
fi

echo "==> Downloading staragent..."
mkdir -p "$INSTALL_DIR/bin" "$CONFIG_DIR" "$LOG_DIR"
curl -fsSL "$ARCHIVE_URL" -o /tmp/staragent.tar.gz
tar xzf /tmp/staragent.tar.gz -C /tmp/
if [[ -f /tmp/staragent ]]; then
    cp /tmp/staragent "$INSTALL_DIR/bin/staragent"
    chmod 755 "$INSTALL_DIR/bin/staragent"
else
    echo "ERROR: Binary not found in archive" >&2
    exit 1
fi

echo "==> Writing config..."
cat > "$CONFIG_DIR/staragent.yaml" <<YAMLEOF
nats:
  url: "$NATS_URL"
  token: "$NATS_TOKEN"
telemetry:
  interval_secs: 10
commands:
  subscribe:
    - "starship.agent.staragent.command.>"
    - "agnetic.agent.staragent.command.>"
hostname: "$HOSTNAME"
YAMLEOF

echo "==> Installing systemd service..."
cat > /etc/systemd/system/agnetic-staragent.service <<UNIT
[Unit]
Description=Starship OS - StarAgent Telemetry Collector
After=network.target
[Service]
Type=simple
ExecStart=$INSTALL_DIR/bin/staragent
Restart=always
RestartSec=5
Environment=RUST_LOG=info
Environment=STARSHIP_ROOT=$INSTALL_DIR
Environment=STARAGENT_CONFIG=$CONFIG_DIR/staragent.yaml
NoNewPrivileges=true
ProtectSystem=full
ReadWritePaths=$LOG_DIR /tmp
ProtectHome=true
[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable agnetic-staragent.service
systemctl start agnetic-staragent.service
echo "==> Done! StarAgent is running."
"""

    elif platform == "windows":
        return f"""@echo off
setlocal enabledelayedexpansion
title Starship OS Drone Agent Installer
set "AGENT_DIR=C:\\Program Files\\Starship\\Agent"
set "DATA_DIR=C:\\ProgramData\\Starship"
set "LOGS_DIR=%DATA_DIR%\\logs"
set "SERVICE_NAME=StarshipStarAgent"
set "NATS_URL={escaped_url}"
set "NATS_TOKEN={escaped_token}"
set "HOSTNAME={hostname or "%COMPUTERNAME%"}"

net session >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo ERROR: Must run as Administrator
    pause
    exit /b 1
)

echo ==^> Downloading staragent...
powershell -Command "& {{
    $url = '{GITHUB_RELEASES}/{PLATFORM_CONFIG[platform]['archive']}'
    $zip = \"$env:TEMP\\staragent.zip\"
    Invoke-WebRequest -Uri $url -OutFile $zip
    Expand-Archive -Path $zip -DestinationPath \"$env:TEMP\\staragent\" -Force
}}"

if not exist "%TEMP%\\staragent\\staragent.exe" (
    echo ERROR: Download failed
    pause
    exit /b 1
)

echo ==^> Installing files...
if not exist "%AGENT_DIR%" mkdir "%AGENT_DIR%"
if not exist "%DATA_DIR%" mkdir "%DATA_DIR%"
if not exist "%LOGS_DIR%" mkdir "%LOGS_DIR%"
copy /Y "%TEMP%\\staragent\\staragent.exe" "%AGENT_DIR%\\staragent.exe" >nul

echo ==^> Writing config...
(
    echo nats:
    echo   url: "!NATS_URL!"
    echo   token: "!NATS_TOKEN!"
    echo telemetry:
    echo   interval_secs: 10
    echo commands:
    echo   subscribe:
    echo     - "starship.agent.staragent.command.^>"
    echo     - "agnetic.agent.staragent.command.^>"
    echo hostname: "!HOSTNAME!"
) > "%AGENT_DIR%\\staragent.yaml"

echo ==^> Installing service...
sc stop %SERVICE_NAME% >nul 2>&1
sc delete %SERVICE_NAME% >nul 2>&1
sc create %SERVICE_NAME% binPath="%AGENT_DIR%\\staragent.exe" DisplayName="Starship OS StarAgent Telemetry Collector" start=auto obj=LocalSystem
sc start %SERVICE_NAME% >nul 2>&1

echo ==^> Done! StarAgent is running.
pause
"""
    return ""


async def handle_agent_download(request):
    """Download a pre-configured agent installer for the given platform."""
    platform = request.match_info.get("platform", "").lower()
    if platform not in PLATFORM_CONFIG:
        return web.json_response({"error": f"Unsupported platform: {platform}. Choose: {', '.join(PLATFORM_CONFIG.keys())}"}, status=400)

    token = request.query.get("token", "") or _get_or_create_agent_token()
    hostname = request.query.get("hostname", "")

    hub_ip = _get_hub_ip()
    nats_port = NATS_URL.split(":")[-1] if ":" in NATS_URL else "4222"
    nats_url = f"nats://{hub_ip}:{nats_port}"

    # Try to serve a pre-built archive with injected config
    archive_path = _find_archive(platform)
    if archive_path:
        import zipfile, tarfile
        raw = archive_path.read_bytes()
        if platform == "windows":
            buf = io.BytesIO()
            with zipfile.ZipFile(io.BytesIO(raw), "r") as zin:
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
                    for item in zin.infolist():
                        data = zin.read(item.filename)
                        name_lower = item.filename.lower()
                        if "staragent.yaml" in name_lower or "install" in name_lower or "configure" in name_lower:
                            text = data.decode("utf-8", errors="replace")
                            text = text.replace("__STARSHIP_NATS_URL__", nats_url)
                            text = text.replace("__STARSHIP_NATS_TOKEN__", token)
                            data = text.encode("utf-8")
                        zout.writestr(item, data)
            body = buf.getvalue()
            ctype = "application/zip"
            fname = f"staragent-{platform}-x86_64.zip"
        else:
            buf = io.BytesIO()
            with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
                with tarfile.open(fileobj=buf, mode="w:gz") as tar_out:
                    for member in tar.getmembers():
                        f = tar.extractfile(member)
                        if f is None:
                            continue
                        data = f.read()
                        name_lower = member.name.lower()
                        if name_lower.endswith(".yaml") or "install" in name_lower:
                            text = data.decode("utf-8", errors="replace")
                            text = text.replace("__STARSHIP_NATS_URL__", nats_url)
                            text = text.replace("__STARSHIP_NATS_TOKEN__", token)
                            data = text.encode("utf-8")
                        tar_out.addfile(member, io.BytesIO(data))
            body = buf.getvalue()
            ctype = "application/gzip"
            fname = f"staragent-{platform}-x86_64.tar.gz"

        return web.Response(
            body=body,
            content_type=ctype,
            headers={
                "Content-Disposition": f'attachment; filename="{fname}"',
                "Cache-Control": "no-cache",
            },
        )

    # Fallback: generate an inline install script
    script = _build_install_script(platform, nats_url, token, hostname)
    ext = ".bat" if platform == "windows" else ".sh"
    return web.Response(
        body=script.encode("utf-8"),
        content_type="text/plain",
        headers={
            "Content-Disposition": f'attachment; filename="install-staragent{ext}"',
            "Cache-Control": "no-cache",
        },
    )


async def handle_agent_installer_info(request):
    """Return available platforms and hub connection info for the installer UI."""
    hub_ip = _get_hub_ip()
    nats_port = NATS_URL.split(":")[-1] if ":" in NATS_URL else "4222"
    nats_url = f"nats://{hub_ip}:{nats_port}"
    platforms = {}
    for key, cfg in PLATFORM_CONFIG.items():
        has_archive = _find_archive(key) is not None
        platforms[key] = {
            "name": cfg["human"],
            "has_archive": has_archive,
            "download_url": f"/api/agent/download/{key}",
        }
    return web.json_response({
        "nats_url": nats_url,
        "nats_port": nats_port,
        "hub_ip": hub_ip,
        "platforms": platforms,
        "token": _get_or_create_agent_token("_shared"),
        "timestamp": datetime.utcnow().isoformat(),
    })


async def handle_agent_regenerate_token(request):
    """Force-regenerate the shared agent token."""
    token = _regenerate_shared_token()
    return web.json_response({"status": "ok", "token": token, "message": "Shared agent token regenerated"})


# ── App ─────────────────────────────────────────────────────────────────────

app = web.Application()
app.router.add_get("/", handle_index)
app.router.add_get("/static/{path:.*}", handle_static)
app.router.add_get("/marketplace", handle_marketplace_page)

app.router.add_get("/api/dashboard", handle_api_dashboard)
app.router.add_get("/api/agents", handle_api_agents)
app.router.add_get("/api/agent/installer-info", handle_agent_installer_info)
app.router.add_get("/api/agent/download/{platform}", handle_agent_download)
app.router.add_post("/api/agent/regenerate-token", handle_agent_regenerate_token)
app.router.add_get("/api/agent/{name}", handle_api_agent_detail)
app.router.add_get("/api/gpu", handle_api_gpu)
app.router.add_get("/api/ollama/models", handle_api_ollama_models)
app.router.add_post("/api/ollama/pull", handle_api_ollama_pull)
app.router.add_post("/api/ollama/delete", handle_api_ollama_delete)
app.router.add_get("/api/logs/search", handle_log_search)
app.router.add_get("/api/logs/stats", handle_log_stats)
app.router.add_get("/api/logs", handle_logs)
app.router.add_get("/api/history", handle_history)
app.router.add_post("/api/send", handle_send)
app.router.add_post("/api/chat/stream", handle_chat_stream_v2)
app.router.add_get("/api/health", handle_health)
app.router.add_get("/api/fleet", handle_api_fleet)
app.router.add_get("/api/fleet/plants", handle_api_fleet_plants)
app.router.add_post("/api/fleet/exercise", handle_api_fleet_exercise)
app.router.add_post("/api/fleet/register", handle_api_fleet_register)
app.router.add_get("/api/incidents", handle_incidents)
app.router.add_get("/api/policy", handle_policy)
app.router.add_get("/api/memory", handle_memory)
app.router.add_get("/api/memory/graph", handle_memory_graph)
app.router.add_get("/api/skills", handle_skills)
app.router.add_post("/api/skills/vet/{skill}", handle_skill_vet)
app.router.add_get("/api/shield/stats", handle_shield_stats)
app.router.add_get("/api/telemetry/stats", handle_no_data)
app.router.add_get("/api/telemetry/recent", handle_telemetry_recent)
app.router.add_get("/api/accounts", handle_no_data)
app.router.add_get("/api/orgchart", handle_orgchart)
app.router.add_get("/api/email/addresses", handle_no_data)
app.router.add_get("/api/healer", handle_no_data)
app.router.add_get("/api/system/logs", handle_no_data)
app.router.add_get("/api/monitoring/disk", handle_no_data)
app.router.add_get("/api/monitoring/cpu", handle_no_data)

# static assets last (must not shadow /api/*)
app.router.add_get("/{name}", handle_static_root)


async def on_startup(app_):
    try:
        nats_conn = await get_nats()
        agg = get_telemetry_aggregator()
        await agg.start(nats_conn)
    except Exception as e:
        log.warning("TelemetryAggregator startup deferred: %s", e)


async def cleanup(app_):
    global nc
    if nc:
        await nc.close()

app.on_startup.append(on_startup)
app.on_shutdown.append(cleanup)

if __name__ == "__main__":
    log.info("Starship Command Dashboard on http://0.0.0.0:%d (static=%s project=%s)",
             PORT, STATIC_DIR, PROJECT_DIR)
    web.run_app(app, host="0.0.0.0", port=PORT, print=None)
