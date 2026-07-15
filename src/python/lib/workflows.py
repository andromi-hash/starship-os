"""Agent-to-agent workflow orchestrator for Starship OS.
Listens for workflow requests and coordinates multi-agent responses."""

import json
import asyncio
import logging
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
    await nc.publish("agnetic.agent.proxy.command.run-audit", json.dumps({
        "command": "run-audit",
        "reply_to": "agnetic.workflow.security-audit.response"
    }).encode())
    await nc.publish("agnetic.agent.ergo.command.schedule-weekly-scan", json.dumps({
        "command": "schedule-weekly-scan",
        "reply_to": "agnetic.workflow.security-audit.schedule"
    }).encode())
    return {"status": "triggered", "workflow": "security-audit", "timestamp": datetime.now().isoformat()}


@register("deploy")
async def deploy(nc, payload):
    log.info("Workflow: deploy")
    branch = payload.get("branch", "main")
    await nc.publish("agnetic.agent.romi.command.review-code", json.dumps({
        "command": "review-code",
        "args": {"branch": branch},
        "reply_to": "agnetic.workflow.deploy.review"
    }).encode())
    await nc.publish("agnetic.agent.proxy.command.run-tests", json.dumps({
        "command": "run-tests",
        "args": {"branch": branch},
        "reply_to": "agnetic.workflow.deploy.tests"
    }).encode())
    return {"status": "triggered", "workflow": "deploy", "branch": branch, "timestamp": datetime.now().isoformat()}


@register("system-health")
async def system_health(nc, payload):
    log.info("Workflow: system-health")
    await nc.publish("agnetic.agent.proxy.command.check-health", json.dumps({
        "command": "check-health",
        "reply_to": "agnetic.workflow.health.response"
    }).encode())
    return {"status": "triggered", "workflow": "system-health", "timestamp": datetime.now().isoformat()}


@register("captains-briefing")
async def captains_briefing(nc, payload):
    """Romi's post-boot Captain's Briefing — checks agent status via NATS subjects (fast path)."""
    log.info("Workflow: captains-briefing")
    results = {}

    for agent_name in ["romi", "proxy", "ergo"]:
        try:
            sub = await nc.subscribe(f"agnetic.workflow.briefing.{agent_name}", max_msgs=1)
            await nc.publish(f"agnetic.agent.{agent_name}.command.status", json.dumps({
                "command": "status",
                "reply_to": f"agnetic.workflow.briefing.{agent_name}"
            }).encode())
            msg = await sub.next_msg(timeout=10)
            results[agent_name] = json.loads(msg.data.decode())
        except Exception as e:
            results[agent_name] = {"status": "unreachable", "error": str(e)}

    summary = {
        "status": "complete",
        "workflow": "captains-briefing",
        "results": results,
        "timestamp": datetime.now().isoformat(),
    }
    log.info("Captain's Briefing complete: %s", json.dumps(summary)[:300])
    await nc.publish("agnetic.briefing.summary", json.dumps(summary).encode())
    await nc.publish("starship.briefing.result", json.dumps(summary).encode())
    return summary


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
    await nc.subscribe("agnetic.workflow.>", cb=handle_workflow_request)
    log.info("Workflow engine listening on agnetic.workflow.>")
    log.info("Registered workflows: %s", ", ".join(WORKFLOWS.keys()))
