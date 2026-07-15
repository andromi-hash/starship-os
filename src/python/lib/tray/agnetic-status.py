#!/usr/bin/env python3
"""Starship OS Status Bridge — feeds agent/telemetry data to all UI layers."""

import sys
import os
import json
import asyncio
import signal
import logging
from pathlib import Path
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("agnetic-status")

NATS_URL = os.getenv("NATS_URL", "nats://127.0.0.1:4222")
STATUS_FILE = Path("/tmp/agnetic-status.json")
AGENTS = ["proxy", "romi", "ergo", "orchestrator", "system_health", "knowledge_store", "codex-agent", "designer-agent"]


def write_status(data):
    STATUS_FILE.write_text(json.dumps(data, indent=2))
    link = "/tmp/agnetic-status-latest.json"
    try:
        os.unlink(link)
    except FileNotFoundError:
        pass
    os.symlink(str(STATUS_FILE), link)


async def main():
    from nats import connect as nats_connect

    nc = await nats_connect(NATS_URL)
    log.info("Connected to NATS: %s", NATS_URL)

    status = {
        "agents": {name: {"status": "unknown", "last_seen": None} for name in AGENTS},
        "telemetry": {},
        "messages": [],
        "updated": datetime.now().isoformat(),
    }
    write_status(status)

    async def on_agent_status(msg):
        try:
            data = json.loads(msg.data.decode())
            agent = data.get("agent", msg.subject.split(".")[2])
            status["agents"][agent] = {
                "status": data.get("status", "unknown"),
                "last_seen": datetime.now().isoformat(),
                "command": data.get("command", ""),
                "response": data.get("response", ""),
            }
            if data.get("response"):
                status["messages"].insert(0, {
                    "agent": agent,
                    "response": data["response"][:200],
                    "timestamp": datetime.now().isoformat(),
                })
                status["messages"] = status["messages"][:50]
            status["updated"] = datetime.now().isoformat()
            write_status(status)
        except Exception as e:
            log.warning("Status parse error: %s", e)

    async def on_telemetry(msg):
        try:
            data = json.loads(msg.data.decode())
            parts = msg.subject.split(".")
            if len(parts) >= 3:
                key = parts[-1]
            else:
                key = "full"
            status["telemetry"][key] = data
            status["updated"] = datetime.now().isoformat()
            write_status(status)
        except Exception as e:
            log.warning("Telemetry parse error: %s", e)

    for agent in AGENTS:
        await nc.subscribe(f"agnetic.agent.{agent}.status", cb=on_agent_status)

    await nc.subscribe("agnetic.telemetry", cb=on_telemetry)
    await nc.subscribe("agnetic.telemetry.cpu", cb=on_telemetry)
    await nc.subscribe("agnetic.telemetry.mem", cb=on_telemetry)

    log.info("Subscribed to agent status + telemetry. Writing to %s", STATUS_FILE)

    stop = asyncio.Future()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_running_loop().add_signal_handler(sig, lambda: stop.set_result(None))
        except NotImplementedError:
            pass

    try:
        await stop
    except asyncio.CancelledError:
        pass
    finally:
        await nc.close()
        log.info("Shutdown")


if __name__ == "__main__":
    asyncio.run(main())
