"""Agent-to-agent workflow orchestrator for Starship OS.
Listens for workflow requests and coordinates multi-agent responses."""

import json
import asyncio
import logging
import subprocess
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("workflows")

WORKFLOWS = {}


def register(name):
    def wrapper(fn):
        WORKFLOWS[name] = fn
        return fn
    return wrapper


@register("security-audit")
async def security_audit(nc, payload):
    log.info("Workflow: security-audit")
    await nc.publish("starship.agent.proxy.command.run-audit", json.dumps({
        "command": "run-audit",
        "reply_to": "starship.workflow.security-audit.response"
    }).encode())
    await nc.publish("starship.agent.ergo.command.schedule-weekly-scan", json.dumps({
        "command": "schedule-weekly-scan",
        "reply_to": "starship.workflow.security-audit.schedule"
    }).encode())
    return {"status": "triggered", "workflow": "security-audit", "timestamp": datetime.now().isoformat()}


@register("deploy")
async def deploy(nc, payload):
    log.info("Workflow: deploy")
    branch = payload.get("branch", "main")
    await nc.publish("starship.agent.romi.command.review-code", json.dumps({
        "command": "review-code",
        "args": {"branch": branch},
        "reply_to": "starship.workflow.deploy.review"
    }).encode())
    await nc.publish("starship.agent.proxy.command.run-tests", json.dumps({
        "command": "run-tests",
        "args": {"branch": branch},
        "reply_to": "starship.workflow.deploy.tests"
    }).encode())
    return {"status": "triggered", "workflow": "deploy", "branch": branch, "timestamp": datetime.now().isoformat()}


@register("system-health")
async def system_health(nc, payload):
    log.info("Workflow: system-health")
    await nc.publish("starship.agent.proxy.command.check-health", json.dumps({
        "command": "check-health",
        "reply_to": "starship.workflow.health.response"
    }).encode())
    return {"status": "triggered", "workflow": "system-health", "timestamp": datetime.now().isoformat()}


async def handle_workflow_request(msg):
    try:
        data = json.loads(msg.data.decode())
        workflow = data.get("workflow", "")
        if workflow in WORKFLOWS:
            result = await WORKFLOWS[workflow](msg._client, data)
            if msg.reply:
                await msg._client.publish(msg.reply, json.dumps(result).encode())
        else:
            log.warning("Unknown workflow: %s", workflow)
    except Exception as e:
        log.error("Workflow error: %s", e)


async def start_workflow_engine(nc):
    await nc.subscribe("starship.workflow.>", cb=handle_workflow_request)
    log.info("Workflow engine listening on starship.workflow.>")
    log.info("Registered workflows: %s", ", ".join(WORKFLOWS.keys()))
