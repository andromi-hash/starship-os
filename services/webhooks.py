#!/usr/bin/env python3
"""
Starship OS — Webhook Receiver & Cron Scheduler

HTTP webhook receiver with GitHub HMAC verification, generic JSON routing,
built-in cron scheduler, and NATS integration for agent dispatch.

Usage:
    python3 webhooks.py serve                           # start webhook server
    python3 webhooks.py schedule "0 9 * * *" ergo daily-briefing  # add cron
    python3 webhooks.py schedules                       # list cron schedules
    python3 webhooks.py unschedule <id>                 # remove cron
    python3 webhooks.py test --source github --event push  # send test webhook
"""

import sys
import os
import json
import time
import signal
import asyncio
import hashlib
import hmac
import uuid
import logging
import logging.handlers
import re
import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Optional
from functools import partial

try:
    import yaml
except ImportError:
    yaml = None

try:
    from aiohttp import web
except ImportError:
    web = None

try:
    import nats as nats_mod
except ImportError:
    nats_mod = None

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONFIG_PATH = Path("/etc/agnetic/webhooks.yaml")

_db_dir = Path("/var/lib/agnetic")
if not os.access(_db_dir, os.W_OK):
    _db_dir = Path("/tmp/agnetic-data")
_db_dir.mkdir(parents=True, exist_ok=True)
DB_DIR = _db_dir
DB_PATH = DB_DIR / "webhooks.db"

_log_dir = Path("/var/log/agnetic")
if not os.access(_log_dir, os.W_OK):
    _log_dir = Path("/tmp/agnetic-data/logs")
_log_dir.mkdir(parents=True, exist_ok=True)
LOG_DIR = _log_dir
LOG_FILE = LOG_DIR / "webhooks.log"

_pid_dir = Path("/var/run/agnetic")
if not os.access(_pid_dir, os.W_OK):
    _pid_dir = Path("/tmp/agnetic-data")
_pid_dir.mkdir(parents=True, exist_ok=True)
PID_FILE = _pid_dir / "webhooks.pid"
NATS_URL = os.getenv("NATS_URL", "nats://127.0.0.1:4222")

DEFAULT_CONFIG = {
    "server": {"host": "0.0.0.0", "port": 8900},
    "github": {
        "secret": "",
        "events": {
            "push": {"agent": "romi", "action": "review-code"},
            "pull_request": {"agent": "ergo", "action": "create-pr-summary"},
            "issues": {"agent": "proxy", "action": "triage-issue"},
        },
    },
    "generic": [],
    "cron": [
        {
            "schedule": "0 9 * * *",
            "agent": "ergo",
            "action": "daily-briefing",
            "description": "Morning briefing at 9am",
        },
        {
            "schedule": "0 18 * * 5",
            "agent": "romi",
            "action": "weekly-report",
            "description": "Friday 6pm weekly report",
        },
    ],
}

VALID_AGENTS = {"proxy", "romi", "ergo", "staragent"}

# ---------------------------------------------------------------------------
# Structured Logger
# ---------------------------------------------------------------------------


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "service": getattr(record, "service", "webhooks"),
            "event": getattr(record, "event", record.getMessage()),
        }
        details = getattr(record, "details", None)
        if details:
            entry["details"] = details
        return json.dumps(entry, default=str)


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("webhooks")
    logger.setLevel(logging.INFO)
    fmt = JSONFormatter()
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            str(LOG_FILE), maxBytes=5 * 1024 * 1024, backupCount=3
        )
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except OSError:
        pass
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


log = setup_logging()


def _log(event: str, level: str = "info", details: dict | None = None):
    extra: dict[str, Any] = {"service": "webhooks", "event": event}
    if details:
        extra["details"] = details
    getattr(log, level, log.info)(event, extra=extra)


# ---------------------------------------------------------------------------
# Database — persistent cron schedules & webhook log
# ---------------------------------------------------------------------------


def _get_db() -> sqlite3.Connection:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=3000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schedules (
            id          TEXT PRIMARY KEY,
            cron_expr   TEXT NOT NULL,
            agent       TEXT NOT NULL,
            action      TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            enabled     INTEGER NOT NULL DEFAULT 1,
            created_at  TEXT NOT NULL,
            last_run    TEXT,
            next_run    TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS webhook_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            received_at TEXT NOT NULL,
            source      TEXT NOT NULL,
            event       TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'received',
            agent       TEXT NOT NULL DEFAULT '',
            action      TEXT NOT NULL DEFAULT '',
            details     TEXT NOT NULL DEFAULT '{}'
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_whlog_ts ON webhook_log(received_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_whlog_src ON webhook_log(source)")
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Cron math helpers
# ---------------------------------------------------------------------------

_CRON_FIELD = re.compile(
    r"^(\*|([0-9]|[1-5][0-9])(-([0-9]|[1-5][0-9]))?"
    r"(/([0-9]|[1-5][0-9]))?)"
    r"(,(\*|([0-9]|[1-5][0-9])(-([0-9]|[1-5][0-9]))?(/([0-9]|[1-5][0-9]))?))*$"
)


def _parse_cron_field(field: str, lo: int, hi: int) -> set[int]:
    values: set[int] = set()
    for part in field.split(","):
        if part == "*":
            values.update(range(lo, hi + 1))
            continue
        if "/" in part:
            base, step_s = part.split("/", 1)
            step = int(step_s)
            if step < 1:
                raise ValueError(f"bad step {step}")
            if base == "*":
                start = lo
            elif "-" in base:
                start = int(base.split("-")[0])
            else:
                start = int(base)
            values.update(range(start, hi + 1, step))
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            values.update(range(int(a), int(b) + 1))
        else:
            values.add(int(part))
    return {v for v in values if lo <= v <= hi}


def _validate_cron(expr: str) -> tuple[set, set, set, set, set]:
    parts = expr.strip().split()
    if len(parts) != 5:
        raise ValueError(f"cron expression must have 5 fields, got {len(parts)}")
    minute = _parse_cron_field(parts[0], 0, 59)
    hour = _parse_cron_field(parts[1], 0, 23)
    day = _parse_cron_field(parts[2], 1, 31)
    month = _parse_cron_field(parts[3], 1, 12)
    dow = _parse_cron_field(parts[4], 0, 6)
    return minute, hour, day, month, dow


def cron_next_run(expr: str, now: datetime | None = None) -> datetime:
    now = now or datetime.now()
    minute, hour, day, month, dow = _validate_cron(expr)
    candidate = now.replace(second=0, microsecond=0)
    for _ in range(366 * 24 * 60):
        candidate = candidate.replace(second=0, microsecond=0)
        if (
            candidate.minute in minute
            and candidate.hour in hour
            and candidate.day in day
            and candidate.month in month
            and candidate.weekday() in dow
        ):
            return candidate
        candidate = candidate.replace(second=0, microsecond=0)
        minute_s = min(minute)
        hour_s = min(hour)
        if candidate.minute < minute_s:
            candidate = candidate.replace(minute=minute_s)
        elif candidate.minute > minute_s or candidate.hour < hour_s:
            candidate = candidate.replace(minute=minute_s)
            candidate = candidate.replace(hour=hour_s)
            if candidate.hour > hour_s:
                candidate = candidate.replace(hour=hour_s)
                candidate = candidate.replace(minute=minute_s)
                candidate = candidate.replace(day=min(day), month=min(month))
                # advance day
                from datetime import timedelta
                candidate += timedelta(days=1)
                candidate = candidate.replace(hour=min(hour), minute=min(minute))
        else:
            from datetime import timedelta
            candidate += timedelta(minutes=1)
    raise ValueError("could not compute next cron run within 1 year")


def _compute_next(expr: str) -> str:
    nxt = cron_next_run(expr)
    return nxt.isoformat()


# ---------------------------------------------------------------------------
# Schedule CRUD
# ---------------------------------------------------------------------------


def schedule_add(cron_expr: str, agent: str, action: str, description: str = "") -> str:
    _validate_cron(cron_expr)
    if agent not in VALID_AGENTS:
        raise ValueError(f"unknown agent {agent!r}; valid: {sorted(VALID_AGENTS)}")
    sched_id = uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc).isoformat()
    next_run = _compute_next(cron_expr)
    conn = _get_db()
    conn.execute(
        "INSERT INTO schedules (id, cron_expr, agent, action, description, enabled, created_at, next_run) "
        "VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
        (sched_id, cron_expr, agent, action, description, now, next_run),
    )
    conn.commit()
    _log("schedule_added", details={"id": sched_id, "cron": cron_expr, "agent": agent, "action": action})
    return sched_id


def schedule_remove(sched_id: str) -> bool:
    conn = _get_db()
    cur = conn.execute("DELETE FROM schedules WHERE id = ?", (sched_id,))
    conn.commit()
    removed = cur.rowcount > 0
    if removed:
        _log("schedule_removed", details={"id": sched_id})
    else:
        _log("schedule_remove_not_found", level="warning", details={"id": sched_id})
    return removed


def schedule_list() -> list[dict]:
    conn = _get_db()
    rows = conn.execute("SELECT * FROM schedules ORDER BY next_run").fetchall()
    return [dict(r) for r in rows]


def schedule_get_due() -> list[dict]:
    now = datetime.now(timezone.utc).isoformat()
    conn = _get_db()
    rows = conn.execute(
        "SELECT * FROM schedules WHERE enabled = 1 AND next_run <= ?", (now,)
    ).fetchall()
    return [dict(r) for r in rows]


def schedule_update_after_run(sched_id: str):
    conn = _get_db()
    row = conn.execute("SELECT cron_expr FROM schedules WHERE id = ?", (sched_id,)).fetchone()
    if not row:
        return
    nxt = _compute_next(row["cron_expr"])
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE schedules SET last_run = ?, next_run = ? WHERE id = ?",
        (now, nxt, sched_id),
    )
    conn.commit()


def log_webhook(source: str, event: str, status: str = "received", agent: str = "", action: str = "", details: dict | None = None):
    conn = _get_db()
    conn.execute(
        "INSERT INTO webhook_log (received_at, source, event, status, agent, action, details) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            datetime.now(timezone.utc).isoformat(),
            source, event, status, agent, action,
            json.dumps(details or {}, default=str),
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------


def load_config() -> dict:
    merged = json.loads(json.dumps(DEFAULT_CONFIG))
    if CONFIG_PATH.exists() and yaml is not None:
        try:
            with open(CONFIG_PATH) as f:
                cfg = yaml.safe_load(f) or {}
            if "server" in cfg:
                merged["server"].update(cfg["server"])
            if "github" in cfg:
                gh = cfg["github"]
                secret = gh.get("secret", "")
                if secret.startswith("${") and secret.endswith("}"):
                    env_key = secret[2:-1]
                    secret = os.getenv(env_key, "")
                merged["github"]["secret"] = secret
                if "events" in gh:
                    merged["github"]["events"].update(gh["events"])
            if "generic" in cfg:
                merged["generic"] = cfg["generic"]
            if "cron" in cfg:
                merged["cron"] = cfg["cron"]
            _log("config_loaded", details={"path": str(CONFIG_PATH)})
        except Exception as exc:
            _log("config_load_failed", level="warning", details={"error": str(exc)})
    return merged


# ---------------------------------------------------------------------------
# NATS publisher
# ---------------------------------------------------------------------------


_nats_client = None


async def _get_nats():
    global _nats_client
    if nats_mod is None:
        return None
    if _nats_client is None or _nats_client.is_closed:
        try:
            _nats_client = await nats_mod.connect(NATS_URL)
        except Exception as exc:
            _log("nats_connect_failed", level="warning", details={"error": str(exc)})
            return None
    return _nats_client


async def publish_to_nats(source: str, event: str, payload: dict):
    nc = await _get_nats()
    if nc is None:
        _log("nats_publish_skip", details={"reason": "not connected"})
        return
    subject = f"agnetic.webhooks.{source}.{event}"
    msg = {
        "source": source,
        "event": event,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "payload": payload,
    }
    try:
        await nc.publish(subject, json.dumps(msg, default=str).encode())
        _log("nats_published", details={"subject": subject})
    except Exception as exc:
        _log("nats_publish_failed", level="error", details={"subject": subject, "error": str(exc)})


async def dispatch_to_agent(agent: str, action: str, payload: dict):
    if agent not in VALID_AGENTS:
        _log("invalid_agent", level="warning", details={"agent": agent})
        return
    nc = await _get_nats()
    if nc is None:
        _log("dispatch_skip", details={"agent": agent, "action": action})
        return
    subject = f"agnetic.agent.{agent}.command.webhook"
    msg = {
        "command": action,
        "source": "webhook",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "payload": payload,
    }
    try:
        await nc.publish(subject, json.dumps(msg, default=str).encode())
        _log("agent_dispatched", details={"agent": agent, "action": action})
    except Exception as exc:
        _log("agent_dispatch_failed", level="error", details={"agent": agent, "error": str(exc)})


# ---------------------------------------------------------------------------
# GitHub HMAC verification
# ---------------------------------------------------------------------------


def verify_github_signature(secret: str, payload_body: bytes, signature_header: str) -> bool:
    if not secret:
        _log("github_no_secret", level="warning")
        return True
    if not signature_header:
        return False
    if not signature_header.startswith("sha256="):
        return False
    sig = signature_header[7:]
    expected = hmac.new(secret.encode(), payload_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)


# ---------------------------------------------------------------------------
# HTTP Handlers
# ---------------------------------------------------------------------------


def _json_response(data: dict, status: int = 200) -> web.Response:
    return web.json_response(data, status=status)


async def handle_github(request: web.Request) -> web.Response:
    cfg = request.app["config"]
    body = await request.read()
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not verify_github_signature(cfg["github"]["secret"], body, signature):
        _log("github_signature_invalid", level="warning")
        return _json_response({"error": "invalid signature"}, 403)

    event = request.headers.get("X-GitHub-Event", "unknown")
    delivery_id = request.headers.get("X-GitHub-Delivery", str(uuid.uuid4()))
    _log("github_received", details={"event": event, "delivery": delivery_id})

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return _json_response({"error": "invalid JSON"}, 400)

    event_cfg = cfg["github"]["events"].get(event)
    if event_cfg:
        agent = event_cfg.get("agent", "")
        action = event_cfg.get("action", "")
        log_webhook("github", event, "dispatched", agent, action, {"delivery": delivery_id})
        await publish_to_nats("github", event, payload)
        if agent and action:
            await dispatch_to_agent(agent, action, {"event": event, "delivery": delivery_id, **payload})
    else:
        log_webhook("github", event, "unmapped", details={"delivery": delivery_id})
        await publish_to_nats("github", event, payload)

    return _json_response({"status": "ok", "event": event, "delivery": delivery_id})


async def handle_generic(request: web.Request) -> web.Response:
    cfg = request.app["config"]
    body = await request.read()
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return _json_response({"error": "invalid JSON"}, 400)

    # Route via custom header or payload path field
    route = request.headers.get("X-Webhook-Route", payload.get("route", ""))
    source = payload.get("source", "generic")
    event = payload.get("event", route or "generic")

    _log("generic_received", details={"route": route, "source": source, "event": event})

    # Match against configured generic routes
    dispatched = False
    for route_cfg in cfg.get("generic", []):
        if route_cfg.get("path", "") == route or route_cfg.get("event", "") == event:
            agent = route_cfg.get("agent", "")
            action = route_cfg.get("action", "")
            log_webhook("generic", event, "dispatched", agent, action)
            await publish_to_nats("generic", event, payload)
            if agent and action:
                await dispatch_to_agent(agent, action, payload)
            dispatched = True
            break

    if not dispatched:
        log_webhook("generic", event, "no_match")
        await publish_to_nats("generic", event, payload)

    return _json_response({"status": "ok", "event": event, "routed": dispatched})


async def handle_cron(request: web.Request) -> web.Response:
    body = await request.read()
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        payload = {}

    sched_id = payload.get("schedule_id", "")
    _log("cron_triggered", details={"schedule_id": sched_id})

    conn = _get_db()
    if sched_id:
        row = conn.execute("SELECT * FROM schedules WHERE id = ?", (sched_id,)).fetchone()
        if not row:
            return _json_response({"error": f"schedule {sched_id} not found"}, 404)
        sched = dict(row)
    else:
        due = schedule_get_due()
        if not due:
            return _json_response({"status": "ok", "message": "no schedules due"})
        sched = due[0]
        sched_id = sched["id"]

    agent = sched["agent"]
    action = sched["action"]
    log_webhook("cron", action, "triggered", agent, action, {"schedule_id": sched_id})
    await publish_to_nats("cron", action, {"schedule_id": sched_id, "cron_expr": sched["cron_expr"]})
    await dispatch_to_agent(agent, action, {"schedule_id": sched_id, "cron_expr": sched["cron_expr"]})
    schedule_update_after_run(sched_id)

    return _json_response({"status": "ok", "schedule_id": sched_id, "agent": agent, "action": action})


async def handle_test(request: web.Request) -> web.Response:
    body = await request.read()
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        payload = {}

    source = payload.get("source", "test")
    event = payload.get("event", "ping")
    _log("test_received", details={"source": source, "event": event})
    log_webhook(source, event, "test")
    await publish_to_nats(source, event, {"test": True, "timestamp": datetime.now(timezone.utc).isoformat()})

    return _json_response({
        "status": "ok",
        "source": source,
        "event": event,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


async def handle_health(request: web.Request) -> web.Response:
    return _json_response({
        "status": "healthy",
        "service": "webhooks",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "uptime": time.time() - request.app.get("start_time", time.time()),
    })


# ---------------------------------------------------------------------------
# Cron scheduler loop
# ---------------------------------------------------------------------------


async def cron_loop(app: web.Application):
    _log("cron_loop_started")
    while True:
        try:
            due = schedule_get_due()
            for sched in due:
                _log("cron_executing", details={"id": sched["id"], "action": sched["action"]})
                try:
                    agent = sched["agent"]
                    action = sched["action"]
                    log_webhook("cron", action, "auto-triggered", agent, action, {"schedule_id": sched["id"]})
                    await publish_to_nats("cron", action, {"schedule_id": sched["id"]})
                    await dispatch_to_agent(agent, action, {"schedule_id": sched["id"], "cron_expr": sched["cron_expr"]})
                    schedule_update_after_run(sched["id"])
                except Exception as exc:
                    _log("cron_exec_failed", level="error", details={"id": sched["id"], "error": str(exc)})
        except Exception as exc:
            _log("cron_loop_error", level="error", details={"error": str(exc)})
        await asyncio.sleep(30)


# ---------------------------------------------------------------------------
# Seed default cron schedules from config (if DB empty)
# ---------------------------------------------------------------------------


def seed_cron_from_config(cfg: dict):
    conn = _get_db()
    existing = conn.execute("SELECT COUNT(*) as cnt FROM schedules").fetchone()["cnt"]
    if existing > 0:
        return
    for entry in cfg.get("cron", []):
        try:
            schedule_add(
                entry["schedule"],
                entry["agent"],
                entry["action"],
                entry.get("description", ""),
            )
            _log("cron_seeded", details={"action": entry["action"]})
        except Exception as exc:
            _log("cron_seed_failed", level="warning", details={"action": entry.get("action", ""), "error": str(exc)})


# ---------------------------------------------------------------------------
# Server startup
# ---------------------------------------------------------------------------


def build_app(cfg: dict) -> web.Application:
    app = web.Application()
    app["config"] = cfg
    app["start_time"] = time.time()
    app.router.add_post("/webhook/github", handle_github)
    app.router.add_post("/webhook/generic", handle_generic)
    app.router.add_post("/webhook/cron", handle_cron)
    app.router.add_post("/webhook/test", handle_test)
    app.router.add_get("/health", handle_health)
    return app


async def start_background_tasks(app: web.Application):
    app["cron_task"] = asyncio.create_task(cron_loop(app))


async def cleanup_background_tasks(app: web.Application):
    task = app.get("cron_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    if _nats_client and not _nats_client.is_closed:
        await _nats_client.close()


def cmd_serve(args):
    if web is None:
        print("aiohttp is required: pip install aiohttp", file=sys.stderr)
        sys.exit(1)

    cfg = load_config()
    seed_cron_from_config(cfg)
    host = cfg["server"]["host"]
    port = cfg["server"]["port"]

    _log("server_starting", details={"host": host, "port": port})

    app = build_app(cfg)
    app.on_startup.append(start_background_tasks)
    app.on_cleanup.append(cleanup_background_tasks)

    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))

    try:
        web.run_app(app, host=host, port=port, print=None)
    finally:
        try:
            PID_FILE.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------


def cmd_schedule(args):
    cron_expr = args[0] if args else ""
    agent = args[1] if len(args) > 1 else ""
    action = args[2] if len(args) > 2 else ""
    description = args[3] if len(args) > 3 else ""
    if not all([cron_expr, agent, action]):
        print("usage: webhooks.py schedule \"cron_expr\" agent action [description]")
        sys.exit(1)
    try:
        sched_id = schedule_add(cron_expr, agent, action, description)
        print(f"added schedule {sched_id}: {cron_expr} → {agent}/{action}")
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


def cmd_schedules(args):
    rows = schedule_list()
    if not rows:
        print("no schedules")
        return
    print(f"{'ID':<14} {'CRON':<16} {'AGENT':<12} {'ACTION':<20} {'NEXT RUN':<22} {'ENABLED'}")
    print("-" * 100)
    for r in rows:
        print(
            f"{r['id']:<14} {r['cron_expr']:<16} {r['agent']:<12} "
            f"{r['action']:<20} {r['next_run']:<22} {'yes' if r['enabled'] else 'no'}"
        )
    if rows:
        print(f"\n{len(rows)} schedule(s)")


def cmd_unschedule(args):
    if not args:
        print("usage: webhooks.py unschedule <id>")
        sys.exit(1)
    removed = schedule_remove(args[0])
    if removed:
        print(f"removed schedule {args[0]}")
    else:
        print(f"schedule {args[0]} not found", file=sys.stderr)
        sys.exit(1)


def cmd_test(args):
    import argparse as _ap
    p = _ap.ArgumentParser()
    p.add_argument("--source", default="github")
    p.add_argument("--event", default="push")
    p.add_argument("--url", default="http://127.0.0.1:8900/webhook/test")
    opts = p.parse_args(args)

    import urllib.request
    payload = json.dumps({"source": opts.source, "event": opts.event}).encode()
    req = urllib.request.Request(
        opts.url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        print(json.loads(resp.read().decode()))
    except Exception as exc:
        print(f"test failed: {exc}", file=sys.stderr)
        sys.exit(1)


def cmd_health(args):
    import urllib.request
    url = "http://127.0.0.1:8900/health"
    try:
        resp = urllib.request.urlopen(url, timeout=5)
        print(json.loads(resp.read().decode()))
    except Exception as exc:
        print(f"health check failed: {exc}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    if len(sys.argv) < 2:
        print("usage: webhooks.py <command> [args]")
        print("commands: serve, schedule, schedules, unschedule, test, health")
        sys.exit(1)

    cmd = sys.argv[1]
    rest = sys.argv[2:]

    cmds = {
        "serve": cmd_serve,
        "schedule": cmd_schedule,
        "schedules": cmd_schedules,
        "unschedule": cmd_unschedule,
        "test": cmd_test,
        "health": cmd_health,
    }

    fn = cmds.get(cmd)
    if fn is None:
        print(f"unknown command: {cmd}", file=sys.stderr)
        print("commands: serve, schedule, schedules, unschedule, test, health")
        sys.exit(1)

    fn(rest)


if __name__ == "__main__":
    main()
