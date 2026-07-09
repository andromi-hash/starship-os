#!/usr/bin/env python3
"""Starship OS Web Dashboard — serves UI + bridges NATS to HTTP."""

import sys
import os
import json
import asyncio
import logging
from pathlib import Path
from datetime import datetime
from aiohttp import web

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("starship-dash")

NATS_URL = os.getenv("NATS_URL", "nats://127.0.0.1:4222")
PORT = int(os.getenv("DASHBOARD_PORT", "8899"))
STATUS_FILE = Path("/tmp/starship-status.json")
HISTORY_DIR = Path("/tmp/starship-history")
PROJECT_DIR = Path(os.getenv("STARSHIP_ROOT", os.path.dirname(os.path.abspath(__file__)).replace("/dashboard", "")))

DASHBOARD_HTML = (PROJECT_DIR / "dashboard" / "index.html").read_text() if (PROJECT_DIR / "dashboard" / "index.html").exists() else "<h1>Dashboard loading...</h1>"

nc = None


async def get_nats():
    global nc
    if nc is None or not nc.is_connected:
        from nats import connect as nats_connect
        nc = await nats_connect(NATS_URL)
    return nc


async def handle_status(request):
    try:
        data = json.loads(STATUS_FILE.read_text())
        return web.json_response(data)
    except (FileNotFoundError, json.JSONDecodeError):
        return web.json_response({"agents": {}, "telemetry": {}, "messages": []})


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
        subject = f"starship.agent.{agent}.command.{safe_command}"
        reply = f"starship.reply.{datetime.now().timestamp()}"
        sub = await nats.subscribe(reply, max_msgs=1)

        await nats.publish(subject, json.dumps({
            "command": command,
            "args": args,
            "reply_to": reply,
        }).encode())

        try:
            msg = await sub.next_msg(timeout=30)
            result = json.loads(msg.data.decode())
            return web.json_response(result)
        except asyncio.TimeoutError:
            return web.json_response({"error": "timeout", "response": "Agent did not respond in 30s"})
    except Exception as e:
        return web.json_response({"error": str(e)})


async def handle_index(request):
    return web.Response(text=DASHBOARD_HTML, content_type="text/html")


async def handle_logs(request):
    agent = request.query.get("agent", "proxy")
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


async def handle_workflow(request):
    try:
        body = await request.json()
        workflow = body.get("workflow", "")
        payload = body.get("payload", {})
        nats = await get_nats()
        subject = f"starship.workflow.{workflow}"
        reply = f"starship.workflow.reply.{datetime.now().timestamp()}"
        sub = await nats.subscribe(reply, max_msgs=1)
        await nats.publish(subject, json.dumps({
            "workflow": workflow,
            "payload": payload,
            "reply_to": reply,
        }).encode())
        try:
            msg = await sub.next_msg(timeout=45)
            result = json.loads(msg.data.decode())
            return web.json_response(result)
        except asyncio.TimeoutError:
            return web.json_response({"error": "timeout", "workflow": workflow})
    except Exception as e:
        return web.json_response({"error": str(e)})


async def handle_health(request):
    return web.json_response({
        "status": "ok",
        "agents_running": os.system("pgrep -f agent_daemon.py > /dev/null 2>&1") == 0,
        "nats_running": os.system("pgrep -x nats-server > /dev/null 2>&1") == 0,
        "staragent_running": os.system("pgrep -x staragent > /dev/null 2>&1") == 0,
        "timestamp": datetime.now().isoformat(),
    })


app = web.Application()
app.router.add_get("/", handle_index)
app.router.add_get("/api/status", handle_status)
app.router.add_get("/api/health", handle_health)
app.router.add_get("/api/logs", handle_logs)
app.router.add_get("/api/history", handle_history)
app.router.add_post("/api/send", handle_send)
app.router.add_post("/api/workflow", handle_workflow)
app.router.add_static("/static", path=str(PROJECT_DIR / "dashboard"), name="static")


async def cleanup(app):
    global nc
    if nc:
        await nc.close()


app.on_shutdown.append(cleanup)

if __name__ == "__main__":
    log.info("Starship Dashboard starting on http://0.0.0.0:%d", PORT)
    web.run_app(app, host="0.0.0.0", port=PORT)
