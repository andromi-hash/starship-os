#!/usr/bin/env python3
"""
Starship OS — Kill Switch & Cost Control Service

Prevents runaway agents from consuming unlimited resources.
Hard-coded limits that cannot be overridden by agents.

Usage:
    python3 kill_switch.py status              # show all limits and usage
    python3 kill_switch.py activate            # emergency stop
    python3 kill_switch.py deactivate          # resume
    python3 kill_switch.py usage proxy         # show proxy agent usage
    python3 kill_switch.py limits              # show configured limits
    python3 kill_switch.py set-limit max_tokens 200000  # update a limit
    python3 kill_switch.py daemon              # run as HTTP API daemon
"""

import sys
import os
import json
import time
import signal
import asyncio
import logging
import logging.handlers
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Any

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

CONFIG_PATH = Path("/etc/agnetic/kill-switch.yaml")

_log_dir = Path("/var/log/agnetic")
if not os.access(_log_dir, os.W_OK):
    _log_dir = Path("/tmp/agnetic-data/logs")
_log_dir.mkdir(parents=True, exist_ok=True)
LOG_DIR = _log_dir
LOG_FILE = LOG_DIR / "kill-switch.log"

_data_dir = Path("/var/lib/agnetic")
if not os.access(_data_dir, os.W_OK):
    _data_dir = Path("/tmp/agnetic-data")
_data_dir.mkdir(parents=True, exist_ok=True)
DATA_DIR = _data_dir
USAGE_DB = DATA_DIR / "kill_switch_usage.db"

NATS_URL = os.getenv("NATS_URL", "nats://127.0.0.1:4222")
NATS_KILL_ACTIVATE = "agnetic.kill.activate"
NATS_KILL_DEACTIVATE = "agnetic.kill.deactivate"
NATS_RESOURCE_USAGE = "agnetic.resource.usage"

DEFAULT_LIMITS = {
    "max_tokens_per_session": 100000,
    "max_tool_calls_per_session": 50,
    "max_api_calls_per_minute": 20,
    "max_session_duration_seconds": 3600,
    "max_cost_per_session_usd": 5.00,
    "max_file_writes_per_session": 20,
    "max_shell_commands_per_session": 30,
    "max_delegation_depth": 3,
}

CIRCUIT_BREAKER_DEFAULTS = {
    "failure_threshold": 5,
    "cooldown_seconds": 60,
}

LIMIT_FIELD_MAP = {
    "max_tokens": "max_tokens_per_session",
    "max_tokens_per_session": "max_tokens_per_session",
    "max_tool_calls": "max_tool_calls_per_session",
    "max_tool_calls_per_session": "max_tool_calls_per_session",
    "max_api_calls": "max_api_calls_per_minute",
    "max_api_calls_per_minute": "max_api_calls_per_minute",
    "max_session_duration": "max_session_duration_seconds",
    "max_session_duration_seconds": "max_session_duration_seconds",
    "max_cost": "max_cost_per_session_usd",
    "max_cost_per_session_usd": "max_cost_per_session_usd",
    "max_file_writes": "max_file_writes_per_session",
    "max_file_writes_per_session": "max_file_writes_per_session",
    "max_shell_commands": "max_shell_commands_per_session",
    "max_shell_commands_per_session": "max_shell_commands_per_session",
    "max_delegation_depth": "max_delegation_depth",
}

# ---------------------------------------------------------------------------
# Structured Logger
# ---------------------------------------------------------------------------


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "service": getattr(record, "service", "kill-switch"),
            "event": getattr(record, "event", record.getMessage()),
        }
        details = getattr(record, "details", None)
        if details:
            entry["details"] = details
        return json.dumps(entry, default=str)


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("kill-switch")
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
    extra: dict[str, Any] = {"service": "kill-switch", "event": event}
    if details:
        extra["details"] = details
    getattr(log, level, log.info)(event, extra=extra)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def load_config() -> dict:
    merged = {
        "limits": dict(DEFAULT_LIMITS),
        "circuit_breaker": dict(CIRCUIT_BREAKER_DEFAULTS),
        "per_agent": {},
    }
    if CONFIG_PATH.exists() and yaml is not None:
        try:
            with open(CONFIG_PATH) as f:
                cfg = yaml.safe_load(f) or {}
            if "limits" in cfg:
                merged["limits"].update(cfg["limits"])
            if "circuit_breaker" in cfg:
                merged["circuit_breaker"].update(cfg["circuit_breaker"])
            if "per_agent" in cfg:
                merged["per_agent"] = cfg["per_agent"]
            _log("config_loaded", details={"path": str(CONFIG_PATH)})
        except Exception as exc:
            _log("config_load_failed", level="warning", details={"error": str(exc)})
    return merged


def get_limits_for_agent(config: dict, agent_name: str) -> dict[str, Any]:
    """Return merged limits: global defaults overridden by per-agent config."""
    limits = dict(config["limits"])
    agent_overrides = config.get("per_agent", {}).get(agent_name, {})
    if agent_overrides:
        limits.update(agent_overrides)
    return limits


# ---------------------------------------------------------------------------
# Resource Limits
# ---------------------------------------------------------------------------


class ResourceLimits:
    """Immutable snapshot of resource limits for a session."""

    __slots__ = (
        "max_tokens_per_session",
        "max_tool_calls_per_session",
        "max_api_calls_per_minute",
        "max_session_duration_seconds",
        "max_cost_per_session_usd",
        "max_file_writes_per_session",
        "max_shell_commands_per_session",
        "max_delegation_depth",
    )

    def __init__(self, **kwargs):
        for slot in self.__slots__:
            setattr(self, slot, kwargs.get(slot, DEFAULT_LIMITS.get(slot, 0)))

    def to_dict(self) -> dict[str, Any]:
        return {slot: getattr(self, slot) for slot in self.__slots__}


# ---------------------------------------------------------------------------
# Circuit Breaker
# ---------------------------------------------------------------------------


class CircuitBreaker:
    """Trips when limits are exceeded, blocking further action.

    States:
        CLOSED   — normal operation, actions allowed
        OPEN     — block all actions until cooldown expires
        HALF_OPEN — allow exactly one test action through
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(self, failure_threshold: int = 5, cooldown_seconds: float = 60):
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._state = self.CLOSED
        self._failure_count = 0
        self._last_failure_time: float = 0.0
        self._trip_reason: str = ""

    @property
    def state(self) -> str:
        if self._state == self.OPEN:
            elapsed = time.time() - self._last_failure_time
            if elapsed >= self._cooldown_seconds:
                self._state = self.HALF_OPEN
        return self._state

    @property
    def trip_reason(self) -> str:
        return self._trip_reason

    def check(self, action_type: str) -> bool:
        """Returns True if action is allowed."""
        current = self.state
        if current == self.CLOSED:
            return True
        if current == self.HALF_OPEN:
            return True
        # OPEN — block everything
        _log("circuit_breaker_blocked", level="warning", details={
            "action": action_type,
            "state": self._state,
            "failures": self._failure_count,
            "reason": self._trip_reason,
        })
        return False

    def record_success(self):
        """Record a successful action."""
        if self._state == self.HALF_OPEN:
            _log("circuit_breaker_reset", details={"previous_failures": self._failure_count})
            self._state = self.CLOSED
            self._failure_count = 0
            self._trip_reason = ""

    def record_failure(self):
        """Record a failed action."""
        self._failure_count += 1
        self._last_failure_time = time.time()
        if self._failure_count >= self._failure_threshold:
            self.trip(f"exceeded failure threshold ({self._failure_count}/{self._failure_threshold})")

    def trip(self, reason: str):
        """Open the circuit, blocking all actions."""
        self._state = self.OPEN
        self._trip_reason = reason
        _log("circuit_breaker_tripped", level="error", details={
            "reason": reason,
            "failures": self._failure_count,
        })

    def reset(self):
        """Reset to closed state (manual override)."""
        self._state = self.CLOSED
        self._failure_count = 0
        self._last_failure_time = 0.0
        self._trip_reason = ""
        _log("circuit_breaker_manually_reset")

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "failure_count": self._failure_count,
            "failure_threshold": self._failure_threshold,
            "cooldown_seconds": self._cooldown_seconds,
            "trip_reason": self._trip_reason,
            "last_failure_time": datetime.fromtimestamp(
                self._last_failure_time, tz=timezone.utc
            ).isoformat() if self._last_failure_time else None,
        }


# ---------------------------------------------------------------------------
# Session Tracker
# ---------------------------------------------------------------------------


class SessionTracker:
    """Track resource usage for a single agent session."""

    def __init__(self, agent_name: str, session_id: str, limits: ResourceLimits | None = None):
        self.agent_name = agent_name
        self.session_id = session_id
        self.limits = limits or ResourceLimits()
        self.tokens_used = 0
        self.tool_calls = 0
        self.api_calls = 0
        self.file_writes = 0
        self.shell_commands = 0
        self.delegation_depth = 0
        self.start_time = datetime.now(timezone.utc)
        self.cost_usd = 0.0
        self.api_call_timestamps: list[float] = []

    def record_tool_call(self, tool_name: str, tokens: int = 0):
        """Record a tool call and update counters."""
        self.tool_calls += 1
        self.tokens_used += tokens
        if tool_name in ("write_file", "edit_file"):
            self.file_writes += 1
        elif tool_name in ("shell", "bash", "exec"):
            self.shell_commands += 1
        _log("tool_call_recorded", details={
            "agent": self.agent_name,
            "session": self.session_id,
            "tool": tool_name,
            "tokens": tokens,
        })

    def record_api_call(self, cost: float = 0):
        """Record an API call, update cost, and enforce rate limits."""
        now = time.time()
        self.api_call_timestamps.append(now)
        self.cost_usd += cost
        self.api_calls += 1
        # Prune timestamps older than 1 minute
        cutoff = now - 60
        self.api_call_timestamps = [t for t in self.api_call_timestamps if t > cutoff]

    def get_rate_calls_per_minute(self) -> float:
        """Current rate of API calls in the last minute."""
        now = time.time()
        cutoff = now - 60
        recent = [t for t in self.api_call_timestamps if t > cutoff]
        if not recent:
            return 0.0
        window = now - min(recent)
        if window <= 0:
            return float(len(recent))
        return len(recent) / (window / 60)

    def is_over_limit(self) -> tuple[bool, str]:
        """Check if any limit is exceeded. Returns (exceeded, reason)."""
        elapsed = (datetime.now(timezone.utc) - self.start_time).total_seconds()

        if self.tokens_used >= self.limits.max_tokens_per_session:
            return True, f"tokens: {self.tokens_used}/{self.limits.max_tokens_per_session}"

        if self.tool_calls >= self.limits.max_tool_calls_per_session:
            return True, f"tool_calls: {self.tool_calls}/{self.limits.max_tool_calls_per_session}"

        if self.file_writes >= self.limits.max_file_writes_per_session:
            return True, f"file_writes: {self.file_writes}/{self.limits.max_file_writes_per_session}"

        if self.shell_commands >= self.limits.max_shell_commands_per_session:
            return True, f"shell_commands: {self.shell_commands}/{self.limits.max_shell_commands_per_session}"

        if self.cost_usd >= self.limits.max_cost_per_session_usd:
            return True, f"cost: ${self.cost_usd:.2f}/${self.limits.max_cost_per_session_usd:.2f}"

        if elapsed >= self.limits.max_session_duration_seconds:
            return True, f"duration: {elapsed:.0f}s/{self.limits.max_session_duration_seconds}s"

        if self.api_calls >= self.limits.max_api_calls_per_minute:
            rate = self.get_rate_calls_per_minute()
            if rate > self.limits.max_api_calls_per_minute:
                return True, f"rate: {rate:.1f}/{self.limits.max_api_calls_per_minute} calls/min"

        return False, ""

    def get_usage_report(self) -> dict[str, Any]:
        """Return current usage stats."""
        elapsed = (datetime.now(timezone.utc) - self.start_time).total_seconds()
        return {
            "agent": self.agent_name,
            "session_id": self.session_id,
            "tokens_used": self.tokens_used,
            "tool_calls": self.tool_calls,
            "api_calls": self.api_calls,
            "file_writes": self.file_writes,
            "shell_commands": self.shell_commands,
            "cost_usd": round(self.cost_usd, 4),
            "duration_seconds": round(elapsed, 1),
            "rate_per_minute": round(self.get_rate_calls_per_minute(), 1),
            "limits": self.limits.to_dict(),
            "started_at": self.start_time.isoformat(),
        }


# ---------------------------------------------------------------------------
# Kill Switch
# ---------------------------------------------------------------------------


class KillSwitch:
    """Emergency stop for all agents."""

    KILL_FILE = Path("/tmp/agnetic-kill-switch")

    @classmethod
    def activate(cls):
        """Activate kill switch — stops ALL agents immediately."""
        cls.KILL_FILE.parent.mkdir(parents=True, exist_ok=True)
        cls.KILL_FILE.write_text(json.dumps({
            "activated_at": datetime.now(timezone.utc).isoformat(),
            "pid": os.getpid(),
        }))
        _log("kill_switch_activated", level="error")
        # Publish to NATS (fire-and-forget)
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(_publish_kill_signal(NATS_KILL_ACTIVATE))
            else:
                loop.run_until_complete(_publish_kill_signal(NATS_KILL_ACTIVATE))
        except RuntimeError:
            pass

    @classmethod
    def is_active(cls) -> bool:
        """Check if kill switch is active."""
        return cls.KILL_FILE.exists()

    @classmethod
    def deactivate(cls):
        """Deactivate kill switch."""
        cls.KILL_FILE.unlink(missing_ok=True)
        _log("kill_switch_deactivated", level="info")
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(_publish_kill_signal(NATS_KILL_DEACTIVATE))
            else:
                loop.run_until_complete(_publish_kill_signal(NATS_KILL_DEACTIVATE))
        except RuntimeError:
            pass

    @classmethod
    def get_status(cls) -> dict[str, Any]:
        """Return detailed kill switch status."""
        active = cls.is_active()
        status: dict[str, Any] = {
            "active": active,
            "kill_file": str(cls.KILL_FILE),
        }
        if active:
            try:
                content = json.loads(cls.KILL_FILE.read_text())
                status.update(content)
            except (OSError, json.JSONDecodeError):
                pass
        return status


# ---------------------------------------------------------------------------
# NATS helpers
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


async def _publish_kill_signal(subject: str):
    nc = await _get_nats()
    if nc is None:
        return
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": "kill-switch",
    }
    try:
        await nc.publish(subject, json.dumps(payload).encode())
        _log("nats_signal_published", details={"subject": subject})
    except Exception as exc:
        _log("nats_publish_failed", level="warning", details={"subject": subject, "error": str(exc)})


async def publish_usage_report(agent_name: str, report: dict):
    nc = await _get_nats()
    if nc is None:
        return
    subject = f"{NATS_RESOURCE_USAGE}.{agent_name}"
    try:
        await nc.publish(subject, json.dumps(report, default=str).encode())
    except Exception as exc:
        _log("nats_usage_publish_failed", level="warning", details={"error": str(exc)})


# ---------------------------------------------------------------------------
# In-memory session & circuit breaker registries
# ---------------------------------------------------------------------------

# Active sessions keyed by session_id
_sessions: dict[str, SessionTracker] = {}

# One circuit breaker per agent
_circuit_breakers: dict[str, CircuitBreaker] = {}

# Global config
_config: dict = {}


def get_circuit_breaker(agent_name: str) -> CircuitBreaker:
    """Get or create the circuit breaker for an agent."""
    if agent_name not in _circuit_breakers:
        cb_cfg = _config.get("circuit_breaker", CIRCUIT_BREAKER_DEFAULTS)
        _circuit_breakers[agent_name] = CircuitBreaker(
            failure_threshold=cb_cfg.get("failure_threshold", 5),
            cooldown_seconds=cb_cfg.get("cooldown_seconds", 60),
        )
    return _circuit_breakers[agent_name]


def get_or_create_session(agent_name: str, session_id: str) -> SessionTracker:
    """Get an existing session or create a new one."""
    if session_id not in _sessions:
        limits_cfg = get_limits_for_agent(_config, agent_name)
        limits = ResourceLimits(**limits_cfg)
        _sessions[session_id] = SessionTracker(agent_name, session_id, limits)
    return _sessions[session_id]


# ---------------------------------------------------------------------------
# Integration point
# ---------------------------------------------------------------------------


async def execute_with_limits(
    name: str,
    arguments: dict,
    session_tracker: SessionTracker,
    agent_name: str = "",
    tokens: int = 0,
) -> dict:
    """Gate a tool/action through kill switch, circuit breaker, and session limits.

    Call this before every tool execution. Returns the result dict or an error dict.
    """
    # 1. Kill switch
    if KillSwitch.is_active():
        return {"error": True, "message": "Kill switch active — all actions blocked"}

    # 2. Circuit breaker
    cb = get_circuit_breaker(agent_name)
    if not cb.check(name):
        return {"error": True, "message": "Circuit breaker open — too many failures"}

    # 3. Session limits
    exceeded, reason = session_tracker.is_over_limit()
    if exceeded:
        return {"error": True, "message": f"Resource limit exceeded: {reason}"}

    # 4. Record and execute
    session_tracker.record_tool_call(name, tokens=tokens)

    # In real usage, this would call the actual tool runner.
    # Here we provide the hook so callers can wire it in.
    result = {"error": False, "tool": name, "arguments": arguments}

    # 5. Check for errors that should trip circuit breaker
    if result.get("error"):
        cb.record_failure()
    else:
        cb.record_success()

    return result


# ---------------------------------------------------------------------------
# Usage persistence (SQLite)
# ---------------------------------------------------------------------------

import sqlite3


def _get_usage_db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(USAGE_DB), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=3000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            agent       TEXT NOT NULL,
            session_id  TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            tokens      INTEGER NOT NULL DEFAULT 0,
            tool_calls  INTEGER NOT NULL DEFAULT 0,
            api_calls   INTEGER NOT NULL DEFAULT 0,
            cost_usd    REAL NOT NULL DEFAULT 0.0,
            duration_s  REAL NOT NULL DEFAULT 0.0,
            details     TEXT NOT NULL DEFAULT '{}'
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_agent ON usage_log(agent)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_session ON usage_log(session_id)")
    conn.commit()
    return conn


def persist_usage_report(report: dict):
    """Store a usage report snapshot in the database."""
    conn = _get_usage_db()
    try:
        conn.execute(
            "INSERT INTO usage_log (agent, session_id, recorded_at, tokens, tool_calls, "
            "api_calls, cost_usd, duration_s, details) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                report.get("agent", ""),
                report.get("session_id", ""),
                datetime.now(timezone.utc).isoformat(),
                report.get("tokens_used", 0),
                report.get("tool_calls", 0),
                report.get("api_calls", 0),
                report.get("cost_usd", 0.0),
                report.get("duration_seconds", 0.0),
                json.dumps(report, default=str),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def query_usage(agent: str = "", since_hours: int = 24, limit: int = 100) -> list[dict]:
    """Query persisted usage logs."""
    conn = _get_usage_db()
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).isoformat()
        clauses = ["recorded_at >= ?"]
        params: list = [cutoff]
        if agent:
            clauses.append("agent = ?")
            params.append(agent)
        where = " WHERE " + " AND ".join(clauses)
        params.append(limit)
        rows = conn.execute(
            f"SELECT * FROM usage_log{where} ORDER BY recorded_at DESC LIMIT ?",
            params,
        ).fetchall()
        results = []
        for r in rows:
            entry = dict(r)
            try:
                entry["details"] = json.loads(entry["details"])
            except (json.JSONDecodeError, TypeError):
                pass
            results.append(entry)
        return results
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# HTTP API handlers (aiohttp)
# ---------------------------------------------------------------------------


def _json_response(data: dict, status: int = 200) -> web.Response:
    return web.json_response(data, status=status)


async def api_kill_status(request: web.Request) -> web.Response:
    """GET /api/kill-switch/status"""
    return _json_response(KillSwitch.get_status())


async def api_kill_activate(request: web.Request) -> web.Response:
    """POST /api/kill-switch/activate"""
    KillSwitch.activate()
    return _json_response({"status": "activated", **KillSwitch.get_status()})


async def api_kill_deactivate(request: web.Request) -> web.Response:
    """POST /api/kill-switch/deactivate"""
    KillSwitch.deactivate()
    return _json_response({"status": "deactivated"})


async def api_resource_usage_agent(request: web.Request) -> web.Response:
    """GET /api/resource-usage/{agent}"""
    agent = request.match_info["agent"]
    active_reports = []
    for tracker in _sessions.values():
        if tracker.agent_name == agent:
            active_reports.append(tracker.get_usage_report())
    cb = get_circuit_breaker(agent)
    return _json_response({
        "agent": agent,
        "active_sessions": active_reports,
        "circuit_breaker": cb.to_dict(),
    })


async def api_resource_usage_all(request: web.Request) -> web.Response:
    """GET /api/resource-usage/all"""
    by_agent: dict[str, list] = {}
    for tracker in _sessions.values():
        by_agent.setdefault(tracker.agent_name, []).append(tracker.get_usage_report())
    cbs = {name: cb.to_dict() for name, cb in _circuit_breakers.items()}
    return _json_response({
        "agents": by_agent,
        "circuit_breakers": cbs,
        "kill_switch": KillSwitch.get_status(),
    })


async def api_health(request: web.Request) -> web.Response:
    """GET /health"""
    return _json_response({
        "status": "healthy",
        "service": "kill-switch",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


def build_api_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/api/kill-switch/status", api_kill_status)
    app.router.add_post("/api/kill-switch/activate", api_kill_activate)
    app.router.add_post("/api/kill-switch/deactivate", api_kill_deactivate)
    app.router.add_get("/api/resource-usage/all", api_resource_usage_all)
    app.router.add_get("/api/resource-usage/{agent}", api_resource_usage_agent)
    app.router.add_get("/health", api_health)
    return app


# ---------------------------------------------------------------------------
# NATS subscription daemon
# ---------------------------------------------------------------------------


async def run_nats_listener():
    """Subscribe to kill signal subjects and react immediately."""
    nc = await _get_nats()
    if nc is None:
        _log("nats_listener_skip", level="warning", details={"reason": "not connected"})
        return

    async def _on_activate(msg):
        _log("nats_kill_activate_received", level="error")
        KillSwitch.activate()

    async def _on_deactivate(msg):
        _log("nats_kill_deactivate_received")
        KillSwitch.deactivate()

    await nc.subscribe(NATS_KILL_ACTIVATE, cb=_on_activate)
    await nc.subscribe(NATS_KILL_DEACTIVATE, cb=_on_deactivate)
    _log("nats_listener_started", details={
        "subjects": [NATS_KILL_ACTIVATE, NATS_KILL_DEACTIVATE],
    })


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------


def cmd_status(config: dict):
    """Print kill switch status, active sessions, and circuit breakers."""
    status = KillSwitch.get_status()
    print(f"\nKill Switch: {'ACTIVE' if status['active'] else 'inactive'}")
    if status["active"]:
        print(f"  Activated at: {status.get('activated_at', 'unknown')}")
    print(f"  Kill file:    {status.get('kill_file', KillSwitch.KILL_FILE)}")

    if _sessions:
        print(f"\nActive Sessions ({len(_sessions)}):")
        print(f"  {'Session ID':<20} {'Agent':<12} {'Tokens':<10} {'Tools':<8} {'Cost':<10} {'Duration'}")
        print("  " + "-" * 75)
        for sid, tracker in _sessions.items():
            r = tracker.get_usage_report()
            print(
                f"  {r['session_id']:<20} {r['agent']:<12} "
                f"{r['tokens_used']:<10} {r['tool_calls']:<8} "
                f"${r['cost_usd']:<9.2f} {r['duration_seconds']:.0f}s"
            )
    else:
        print("\nNo active sessions.")

    if _circuit_breakers:
        print(f"\nCircuit Breakers:")
        for name, cb in _circuit_breakers.items():
            print(f"  {name:<15} state={cb.state:<10} failures={cb.failure_count}")
            if cb.trip_reason:
                print(f"  {'':15} reason: {cb.trip_reason}")
    else:
        print("\nNo circuit breakers initialized.")


def cmd_activate(config: dict):
    """Activate the kill switch."""
    KillSwitch.activate()
    print("Kill switch ACTIVATED — all agents stopped.")


def cmd_deactivate(config: dict):
    """Deactivate the kill switch."""
    KillSwitch.deactivate()
    print("Kill switch deactivated — agents may resume.")


def cmd_usage(config: dict):
    """Show usage for a specific agent or all agents."""
    agent = sys.argv[3] if len(sys.argv) > 3 else ""

    if agent:
        # Active session
        active = [t.get_usage_report() for t in _sessions.values() if t.agent_name == agent]
        # Persisted history
        history = query_usage(agent=agent, limit=10)

        if active:
            print(f"\nActive session for '{agent}':")
            for r in active:
                print(f"  Tokens:       {r['tokens_used']}/{r['limits']['max_tokens_per_session']}")
                print(f"  Tool calls:   {r['tool_calls']}/{r['limits']['max_tool_calls_per_session']}")
                print(f"  API calls:    {r['api_calls']}/{r['limits']['max_api_calls_per_minute']}/min")
                print(f"  File writes:  {r['file_writes']}/{r['limits']['max_file_writes_per_session']}")
                print(f"  Shell cmds:   {r['shell_commands']}/{r['limits']['max_shell_commands_per_session']}")
                print(f"  Cost:         ${r['cost_usd']:.2f}/${r['limits']['max_cost_per_session_usd']:.2f}")
                print(f"  Duration:     {r['duration_seconds']:.0f}s/{r['limits']['max_session_duration_seconds']}s")
        else:
            print(f"\nNo active session for '{agent}'.")

        if history:
            print(f"\nRecent usage history ({len(history)} entries):")
            for h in history:
                print(f"  {h['recorded_at'][:19]}  tokens={h['tokens']}  tools={h['tool_calls']}  cost=${h['cost_usd']:.2f}")
        else:
            print("No usage history.")
    else:
        # All agents
        if _sessions:
            print(f"\nActive sessions ({len(_sessions)}):")
            for sid, tracker in _sessions.items():
                r = tracker.get_usage_report()
                print(f"  {r['agent']}/{r['session_id']}: tokens={r['tokens_used']} tools={r['tool_calls']} cost=${r['cost_usd']:.2f}")
        else:
            print("No active sessions.")


def cmd_limits(config: dict):
    """Show configured limits."""
    print("\nGlobal limits:")
    for key, val in config["limits"].items():
        print(f"  {key:<35} {val}")

    cb_cfg = config.get("circuit_breaker", CIRCUIT_BREAKER_DEFAULTS)
    print(f"\nCircuit breaker:")
    for key, val in cb_cfg.items():
        print(f"  {key:<35} {val}")

    per_agent = config.get("per_agent", {})
    if per_agent:
        print(f"\nPer-agent overrides:")
        for agent_name, overrides in per_agent.items():
            print(f"  {agent_name}:")
            for key, val in overrides.items():
                print(f"    {key:<33} {val}")


def cmd_set_limit(config: dict):
    """Update a limit value (writes to config file)."""
    if len(sys.argv) < 5:
        print("usage: kill_switch.py set-limit <limit_name> <value>")
        sys.exit(1)

    limit_name = sys.argv[3]
    raw_value = sys.argv[4]

    canonical = LIMIT_FIELD_MAP.get(limit_name)
    if canonical is None:
        print(f"unknown limit: {limit_name}")
        print(f"valid names: {', '.join(sorted(LIMIT_FIELD_MAP.keys()))}")
        sys.exit(1)

    # Parse value
    if canonical == "max_cost_per_session_usd":
        new_value = float(raw_value)
    else:
        new_value = int(raw_value)

    # Load existing config or create new
    existing: dict = {}
    if CONFIG_PATH.exists() and yaml is not None:
        try:
            with open(CONFIG_PATH) as f:
                existing = yaml.safe_load(f) or {}
        except Exception:
            pass

    if "limits" not in existing:
        existing["limits"] = {}
    existing["limits"][canonical] = new_value

    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(existing, f, default_flow_style=False)

    _log("limit_updated", details={"limit": canonical, "value": new_value})
    print(f"Updated {canonical} = {new_value}")
    print("Restart kill-switch daemon to apply.")


def cmd_daemon(config: dict):
    """Run the HTTP API daemon."""
    if web is None:
        print("aiohttp is required: pip install aiohttp", file=sys.stderr)
        sys.exit(1)

    _log("daemon_starting", details={"port": 8901})

    app = build_api_app()

    async def _startup(app):
        app["start_time"] = time.time()
        asyncio.create_task(run_nats_listener())
        _log("daemon_started")

    async def _cleanup(app):
        if _nats_client and not _nats_client.is_closed:
            await _nats_client.close()

    app.on_startup.append(_startup)
    app.on_cleanup.append(_cleanup)

    try:
        web.run_app(app, host="0.0.0.0", port=8901, print=None)
    except KeyboardInterrupt:
        _log("daemon_stopped")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

COMMANDS = {
    "status": cmd_status,
    "activate": cmd_activate,
    "deactivate": cmd_deactivate,
    "usage": cmd_usage,
    "limits": cmd_limits,
    "set-limit": cmd_set_limit,
    "daemon": cmd_daemon,
}


def main():
    config = load_config()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"

    fn = COMMANDS.get(cmd)
    if fn is None:
        print(f"unknown command: {cmd}", file=sys.stderr)
        print(f"commands: {', '.join(COMMANDS.keys())}")
        sys.exit(1)

    fn(config)


if __name__ == "__main__":
    main()
