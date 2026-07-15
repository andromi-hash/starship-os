"""Scheduled tasks engine for Starship OS.
Reads agent config and triggers workflows on cron schedule."""

import json
import asyncio
import logging
from datetime import datetime
from pathlib import Path
import yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("scheduler")

CONFIG_FILE = Path(__file__).parent / "config.yaml"


def parse_cron(expr: str):
    parts = expr.split()
    if len(parts) != 5:
        return None
    return {
        "minute": parts[0],
        "hour": parts[1],
        "day_month": parts[2],
        "month": parts[3],
        "day_week": parts[4],
    }


def matches_cron(cron: dict, now: datetime) -> bool:
    def match_field(pattern, value):
        if pattern == "*":
            return True
        if "/" in pattern:
            base, step = pattern.split("/")
            if base == "*":
                return value % int(step) == 0
            return value == int(base) and value % int(step) == 0
        if "," in pattern:
            return value in [int(p) for p in pattern.split(",")]
        if "-" in pattern:
            lo, hi = pattern.split("-")
            return int(lo) <= value <= int(hi)
        return str(value) == pattern

    return (
        match_field(cron["minute"], now.minute)
        and match_field(cron["hour"], now.hour)
        and match_field(cron["day_month"], now.day)
        and match_field(cron["month"], now.month)
        and match_field(cron["day_week"], now.weekday())
    )


async def trigger_workflow(nc, workflow: str):
    subject = f"agnetic.workflow.{workflow}"
    reply = f"agnetic.schedule.reply.{datetime.now().timestamp()}"
    log.info("Triggering scheduled workflow: %s", workflow)
    try:
        sub = await nc.subscribe(reply, max_msgs=1)
        await nc.publish(subject, json.dumps({
            "workflow": workflow,
            "scheduled": True,
            "reply_to": reply,
        }).encode())
        msg = await sub.next_msg(timeout=30)
        log.info("Workflow %s completed", workflow)
    except Exception as e:
        log.warning("Workflow %s failed: %s", workflow, e)


async def scheduler_loop(nc):
    log.info("Scheduler engine started")
    last_checks = {}

    while True:
        try:
            config = yaml.safe_load(CONFIG_FILE.read_text())
            now = datetime.now()

            for name, acfg in config.get("agents", {}).items():
                for sched in acfg.get("schedule", []):
                    cron = parse_cron(sched.get("cron", ""))
                    if cron and matches_cron(cron, now):
                        key = f"{name}.{sched['name']}"
                        last_run = last_checks.get(key)
                        if last_run is None or (now - last_run).total_seconds() > 60:
                            last_checks[key] = now
                            await trigger_workflow(nc, sched["workflow"])

        except Exception as e:
            log.error("Scheduler loop error: %s", e)

        await asyncio.sleep(30)


async def main():
    from nats import connect as nats_connect
    nc = await nats_connect("nats://127.0.0.1:4222")
    await scheduler_loop(nc)


if __name__ == "__main__":
    asyncio.run(main())
