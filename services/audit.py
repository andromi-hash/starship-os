#!/usr/bin/env python3
"""
Starship OS — Structured Audit Trail System

Every action an agent takes is logged with enough context to reconstruct
what happened and why. Provides SQLite storage, search, stats, export,
action-chain reconstruction, an integration decorator, and a dashboard API.

Usage:
    python3 audit.py search --agent proxy --action shell --since 1h --limit 50
    python3 audit.py stats
    python3 audit.py chain <session-id>
    python3 audit.py export --since 24h --format json
    python3 audit.py serve                         # start dashboard API
"""

import sys
import os
import re
import json
import uuid
import time
import signal
import asyncio
import sqlite3
import hashlib
import logging
import logging.handlers
import argparse
from enum import Enum
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Any, Optional
from dataclasses import dataclass, field, asdict, fields

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

SCRIPT_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
PROJECT_ROOT = SCRIPT_DIR.parent

_db_dir = Path("/var/lib/agnetic")
if not os.access(_db_dir, os.W_OK):
    _db_dir = Path("/tmp/agnetic-data")
_db_dir.mkdir(parents=True, exist_ok=True)
DB_DIR = _db_dir
DB_PATH = DB_DIR / "audit.db"

_log_dir = Path("/var/log/agnetic")
if not os.access(_log_dir, os.W_OK):
    _log_dir = Path("/tmp/agnetic-data/logs")
_log_dir.mkdir(parents=True, exist_ok=True)
LOG_DIR = _log_dir
LOG_FILE = LOG_DIR / "audit.log"

_pid_dir = Path("/var/run/romatic")
if not os.access(_pid_dir, os.W_OK):
    _pid_dir = Path("/tmp/romatic-data")
_pid_dir.mkdir(parents=True, exist_ok=True)
PID_DIR = _pid_dir
PID_FILE = PID_DIR / "audit.pid"

NATS_URL = os.getenv("NATS_URL", "nats://127.0.0.1:4222")
NATS_AUDIT_SUBJECT = "agnetic.audit.entry"

API_HOST = os.getenv("AUDIT_API_HOST", "127.0.0.1")
API_PORT = int(os.getenv("AUDIT_API_PORT", "8406"))

SENSITIVE_KEYS = {"password", "secret", "token", "api_key", "apikey",
                  "authorization", "credentials", "private_key", "access_token"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("audit")

# ---------------------------------------------------------------------------
# Risk Levels
# ---------------------------------------------------------------------------


class RiskLevel(Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @staticmethod
    def from_str(val: str) -> "RiskLevel":
        try:
            return RiskLevel(val.lower())
        except ValueError:
            return RiskLevel.LOW


# ---------------------------------------------------------------------------
# AuditEntry dataclass
# ---------------------------------------------------------------------------

@dataclass
class AuditEntry:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    ts_epoch: float = field(default_factory=time.time)
    agent: str = ""
    action: str = ""
    tool: str = ""
    arguments: str = "{}"
    result_summary: str = ""
    risk_level: str = "low"
    approval_status: str = "none"
    evaluation_score: float = 0.0
    before_state: str = ""
    after_state: str = ""
    session_id: str = ""
    parent_action_id: str = ""
    metadata: str = "{}"

    def to_dict(self) -> dict:
        d = asdict(self)
        for key in ("arguments", "metadata"):
            if isinstance(d[key], str):
                try:
                    d[key] = json.loads(d[key])
                except (json.JSONDecodeError, TypeError):
                    pass
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str)


# ---------------------------------------------------------------------------
# SQLite helpers
# ---------------------------------------------------------------------------

def _get_db() -> sqlite3.Connection:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=3000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audit (
            id              TEXT PRIMARY KEY,
            timestamp       TEXT    NOT NULL,
            ts_epoch        REAL    NOT NULL,
            agent           TEXT    NOT NULL DEFAULT '',
            action          TEXT    NOT NULL DEFAULT '',
            tool            TEXT    NOT NULL DEFAULT '',
            arguments       TEXT    NOT NULL DEFAULT '{}',
            result_summary  TEXT    NOT NULL DEFAULT '',
            risk_level      TEXT    NOT NULL DEFAULT 'low',
            approval_status TEXT    NOT NULL DEFAULT 'none',
            evaluation_score REAL   NOT NULL DEFAULT 0.0,
            before_state    TEXT    NOT NULL DEFAULT '',
            after_state     TEXT    NOT NULL DEFAULT '',
            session_id      TEXT    NOT NULL DEFAULT '',
            parent_action_id TEXT   NOT NULL DEFAULT '',
            metadata        TEXT    NOT NULL DEFAULT '{}'
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts_epoch)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_agent ON audit(agent)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_action ON audit(action)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_risk ON audit(risk_level)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_session ON audit(session_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_tool ON audit(tool)")
    conn.commit()
    return conn


def _sanitize_dict(d: Any) -> Any:
    """Recursively replace sensitive values with [REDACTED]."""
    if isinstance(d, dict):
        return {
            k: "[REDACTED]" if k.lower() in SENSITIVE_KEYS and d[k] else _sanitize_dict(v)
            for k, v in d.items()
        }
    if isinstance(d, list):
        return [_sanitize_dict(item) for item in d]
    return d


def _parse_since(since: str) -> float:
    m = re.match(r"^(\d+)(m|h|d)$", since)
    if not m:
        raise ValueError(f"Invalid --since value: {since} (use e.g. 30m, 6h, 7d)")
    amount, unit = int(m.group(1)), m.group(2)
    delta = {"m": timedelta(minutes=amount), "h": timedelta(hours=amount),
             "d": timedelta(days=amount)}[unit]
    return (datetime.now(timezone.utc) - delta).timestamp()


def _row_to_entry(row: sqlite3.Row) -> AuditEntry:
    d = dict(row)
    return AuditEntry(**{f.name: d[f.name] for f in fields(AuditEntry) if f.name in d})


# ---------------------------------------------------------------------------
# AuditLogger
# ---------------------------------------------------------------------------

class AuditLogger:
    """Core audit logging engine — write, query, stats, export, chain."""

    def __init__(self, db_path: Optional[Path] = None):
        self._db_path = db_path or DB_PATH
        self._conn: Optional[sqlite3.Connection] = None

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            DB_DIR.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self._db_path), timeout=5)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=3000")
            self._ensure_schema(self._conn)
        return self._conn

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection):
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit (
                id              TEXT PRIMARY KEY,
                timestamp       TEXT    NOT NULL,
                ts_epoch        REAL    NOT NULL,
                agent           TEXT    NOT NULL DEFAULT '',
                action          TEXT    NOT NULL DEFAULT '',
                tool            TEXT    NOT NULL DEFAULT '',
                arguments       TEXT    NOT NULL DEFAULT '{}',
                result_summary  TEXT    NOT NULL DEFAULT '',
                risk_level      TEXT    NOT NULL DEFAULT 'low',
                approval_status TEXT    NOT NULL DEFAULT 'none',
                evaluation_score REAL   NOT NULL DEFAULT 0.0,
                before_state    TEXT    NOT NULL DEFAULT '',
                after_state     TEXT    NOT NULL DEFAULT '',
                session_id      TEXT    NOT NULL DEFAULT '',
                parent_action_id TEXT   NOT NULL DEFAULT '',
                metadata        TEXT    NOT NULL DEFAULT '{}'
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts_epoch)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_agent ON audit(agent)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_action ON audit(action)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_risk ON audit(risk_level)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_session ON audit(session_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_tool ON audit(tool)")
        conn.commit()

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    # -- write --

    def log(
        self,
        action: str,
        *,
        agent: str = "",
        tool: str = "",
        arguments: Any = None,
        result_summary: str = "",
        risk_level: str = "low",
        approval_status: str = "none",
        evaluation_score: float = 0.0,
        before_state: str = "",
        after_state: str = "",
        session_id: str = "",
        parent_action_id: str = "",
        metadata: Any = None,
    ) -> AuditEntry:
        args_json = json.dumps(_sanitize_dict(arguments)) if arguments else "{}"
        meta_json = json.dumps(metadata) if metadata else "{}"

        entry = AuditEntry(
            agent=agent,
            action=action,
            tool=tool,
            arguments=args_json,
            result_summary=result_summary,
            risk_level=risk_level,
            approval_status=approval_status,
            evaluation_score=evaluation_score,
            before_state=before_state,
            after_state=after_state,
            session_id=session_id,
            parent_action_id=parent_action_id,
            metadata=meta_json,
        )

        conn = self._get_conn()
        conn.execute(
            "INSERT INTO audit "
            "(id, timestamp, ts_epoch, agent, action, tool, arguments, result_summary, "
            " risk_level, approval_status, evaluation_score, before_state, after_state, "
            " session_id, parent_action_id, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entry.id, entry.timestamp, entry.ts_epoch,
                entry.agent, entry.action, entry.tool,
                entry.arguments, entry.result_summary,
                entry.risk_level, entry.approval_status,
                entry.evaluation_score, entry.before_state,
                entry.after_state, entry.session_id,
                entry.parent_action_id, entry.metadata,
            ),
        )
        conn.commit()
        log.info(
            "audit log id=%s agent=%s action=%s tool=%s risk=%s",
            entry.id[:8], entry.agent, entry.action, entry.tool, entry.risk_level,
        )
        return entry

    # -- query --

    def query(
        self,
        agent: str = "",
        action: str = "",
        tool: str = "",
        risk_level: str = "",
        since: str = "",
        limit: int = 100,
    ) -> list[AuditEntry]:
        conn = self._get_conn()
        clauses: list[str] = []
        params: list = []

        if agent:
            clauses.append("agent = ?")
            params.append(agent)
        if action:
            clauses.append("action = ?")
            params.append(action)
        if tool:
            clauses.append("tool = ?")
            params.append(tool)
        if risk_level:
            clauses.append("risk_level = ?")
            params.append(risk_level.lower())
        if since:
            clauses.append("ts_epoch >= ?")
            params.append(_parse_since(since))

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT * FROM audit{where} ORDER BY ts_epoch DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(sql, params).fetchall()
        return [_row_to_entry(r) for r in rows]

    # -- stats --

    def stats(self) -> dict:
        conn = self._get_conn()
        total = conn.execute("SELECT COUNT(*) as cnt FROM audit").fetchone()["cnt"]

        by_agent = dict(
            conn.execute(
                "SELECT agent, COUNT(*) as cnt FROM audit GROUP BY agent ORDER BY cnt DESC"
            ).fetchall()
        )
        by_action = dict(
            conn.execute(
                "SELECT action, COUNT(*) as cnt FROM audit GROUP BY action ORDER BY cnt DESC"
            ).fetchall()
        )
        by_risk = dict(
            conn.execute(
                "SELECT risk_level, COUNT(*) as cnt FROM audit GROUP BY risk_level ORDER BY cnt DESC"
            ).fetchall()
        )
        by_tool = dict(
            conn.execute(
                "SELECT tool, COUNT(*) as cnt FROM audit WHERE tool != '' GROUP BY tool ORDER BY cnt DESC"
            ).fetchall()
        )
        by_approval = dict(
            conn.execute(
                "SELECT approval_status, COUNT(*) as cnt FROM audit GROUP BY approval_status ORDER BY cnt DESC"
            ).fetchall()
        )

        newest = conn.execute(
            "SELECT timestamp FROM audit ORDER BY ts_epoch DESC LIMIT 1"
        ).fetchone()
        oldest = conn.execute(
            "SELECT timestamp FROM audit ORDER BY ts_epoch ASC LIMIT 1"
        ).fetchone()

        avg_score_row = conn.execute(
            "SELECT AVG(evaluation_score) as avg_score FROM audit WHERE evaluation_score > 0"
        ).fetchone()

        high_risk_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM audit WHERE risk_level IN ('high', 'critical')"
        ).fetchone()["cnt"]

        return {
            "total": total,
            "by_agent": by_agent,
            "by_action": by_action,
            "by_risk_level": by_risk,
            "by_tool": by_tool,
            "by_approval_status": by_approval,
            "high_risk_count": high_risk_count,
            "avg_evaluation_score": round(avg_score_row["avg_score"] or 0, 3),
            "newest": newest["timestamp"] if newest else None,
            "oldest": oldest["timestamp"] if oldest else None,
        }

    # -- export --

    def export_json(self, since: str = "") -> list[dict]:
        entries = self.query(since=since, limit=100000)
        return [e.to_dict() for e in entries]

    # -- chain --

    def get_chain(self, session_id: str) -> list[AuditEntry]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM audit WHERE session_id = ? ORDER BY ts_epoch ASC",
            (session_id,),
        ).fetchall()
        if not rows:
            return []

        entries = [_row_to_entry(r) for r in rows]
        known_ids = {e.id for e in entries}

        missing_parents = set()
        for e in entries:
            if e.parent_action_id and e.parent_action_id not in known_ids:
                missing_parents.add(e.parent_action_id)

        while missing_parents:
            pid = missing_parents.pop()
            parent_row = conn.execute(
                "SELECT * FROM audit WHERE id = ?", (pid,)
            ).fetchone()
            if parent_row:
                parent = _row_to_entry(parent_row)
                entries.append(parent)
                known_ids.add(parent.id)
                if parent.parent_action_id and parent.parent_action_id not in known_ids:
                    missing_parents.add(parent.parent_action_id)

        by_parent: dict[str, list[AuditEntry]] = {}
        for e in entries:
            by_parent.setdefault(e.parent_action_id or "", []).append(e)

        ordered: list[AuditEntry] = []
        queue = [""]
        visited: set[str] = set()
        while queue:
            pid = queue.pop(0)
            if pid in visited:
                continue
            visited.add(pid)
            for child in sorted(by_parent.get(pid, []), key=lambda x: x.ts_epoch):
                ordered.append(child)
                if child.id in by_parent:
                    queue.append(child.id)

        return ordered

    # -- get single entry --

    def get(self, entry_id: str) -> Optional[AuditEntry]:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM audit WHERE id = ?", (entry_id,)
        ).fetchone()
        return _row_to_entry(row) if row else None


# ---------------------------------------------------------------------------
# Singleton logger instance
# ---------------------------------------------------------------------------

_logger: Optional[AuditLogger] = None


def get_logger() -> AuditLogger:
    global _logger
    if _logger is None:
        _logger = AuditLogger()
    return _logger


# ---------------------------------------------------------------------------
# Integration decorator
# ---------------------------------------------------------------------------

def audit_log(tool: str = "", agent: str = "", risk_level: str = "low"):
    """Decorator that wraps a function and logs every call to the audit trail.

    Usage:
        @audit_log(tool="shell", agent="proxy")
        async def execute_shell(command):
            ...

        @audit_log(tool="file_write", agent="romi", risk_level="medium")
        def write_config(path, data):
            ...
    """
    def decorator(func):
        async def async_wrapper(*args, **kwargs):
            logger = get_logger()
            sanitized_args = _sanitize_dict({
                "args": [str(a)[:500] for a in args],
                "kwargs": {k: str(v)[:500] for k, v in kwargs.items()},
            })

            before_state = kwargs.get("before_state", "")
            session_id = kwargs.get("session_id", "")
            parent_action_id = kwargs.get("parent_action_id", "")

            entry = logger.log(
                action=func.__name__,
                agent=agent,
                tool=tool,
                arguments=sanitized_args,
                risk_level=risk_level,
                before_state=before_state,
                session_id=session_id,
                parent_action_id=parent_action_id,
            )

            try:
                result = await func(*args, **kwargs)
                result_summary = str(result)[:1000] if result is not None else ""
                logger.log(
                    action=f"{func.__name__}.complete",
                    agent=agent,
                    tool=tool,
                    result_summary=result_summary,
                    risk_level=risk_level,
                    session_id=session_id,
                    parent_action_id=entry.id,
                )
                return result
            except Exception as exc:
                logger.log(
                    action=f"{func.__name__}.error",
                    agent=agent,
                    tool=tool,
                    result_summary=f"{type(exc).__name__}: {exc}"[:1000],
                    risk_level="high",
                    session_id=session_id,
                    parent_action_id=entry.id,
                )
                raise

        def sync_wrapper(*args, **kwargs):
            logger = get_logger()
            sanitized_args = _sanitize_dict({
                "args": [str(a)[:500] for a in args],
                "kwargs": {k: str(v)[:500] for k, v in kwargs.items()},
            })

            before_state = kwargs.get("before_state", "")
            session_id = kwargs.get("session_id", "")
            parent_action_id = kwargs.get("parent_action_id", "")

            entry = logger.log(
                action=func.__name__,
                agent=agent,
                tool=tool,
                arguments=sanitized_args,
                risk_level=risk_level,
                before_state=before_state,
                session_id=session_id,
                parent_action_id=parent_action_id,
            )

            try:
                result = func(*args, **kwargs)
                result_summary = str(result)[:1000] if result is not None else ""
                logger.log(
                    action=f"{func.__name__}.complete",
                    agent=agent,
                    tool=tool,
                    result_summary=result_summary,
                    risk_level=risk_level,
                    session_id=session_id,
                    parent_action_id=entry.id,
                )
                return result
            except Exception as exc:
                logger.log(
                    action=f"{func.__name__}.error",
                    agent=agent,
                    tool=tool,
                    result_summary=f"{type(exc).__name__}: {exc}"[:1000],
                    risk_level="high",
                    session_id=session_id,
                    parent_action_id=entry.id,
                )
                raise

        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper

    return decorator


# ---------------------------------------------------------------------------
# NATS publishing (optional)
# ---------------------------------------------------------------------------

_nats_conn = None


async def _get_nats():
    global _nats_conn
    if nats_mod is None:
        return None
    if _nats_conn is None or _nats_conn.is_closed:
        try:
            _nats_conn = await nats_mod.connect(NATS_URL)
        except Exception as exc:
            log.warning("NATS connect failed: %s", exc)
            return None
    return _nats_conn


async def _publish_audit(entry: AuditEntry):
    nc = await _get_nats()
    if nc is None:
        return
    try:
        await nc.publish(NATS_AUDIT_SUBJECT, entry.to_json().encode())
    except Exception as exc:
        log.warning("NATS publish failed: %s", exc)


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------

def _json_response(data: Any, status: int = 200) -> web.Response:
    return web.Response(
        text=json.dumps(data, default=str, indent=2),
        status=status,
        content_type="application/json",
    )


async def api_search(request: web.Request) -> web.Response:
    """GET /api/audit/search?agent=&action=&tool=&risk_level=&since=&limit="""
    logger: AuditLogger = request.app["audit_logger"]
    entries = logger.query(
        agent=request.query.get("agent", ""),
        action=request.query.get("action", ""),
        tool=request.query.get("tool", ""),
        risk_level=request.query.get("risk_level", ""),
        since=request.query.get("since", ""),
        limit=int(request.query.get("limit", "100")),
    )
    return _json_response({
        "count": len(entries),
        "entries": [e.to_dict() for e in entries],
    })


async def api_stats(request: web.Request) -> web.Response:
    """GET /api/audit/stats"""
    logger: AuditLogger = request.app["audit_logger"]
    return _json_response(logger.stats())


async def api_chain(request: web.Request) -> web.Response:
    """GET /api/audit/chain/:session_id"""
    logger: AuditLogger = request.app["audit_logger"]
    session_id = request.match_info["session_id"]
    entries = logger.get_chain(session_id)
    return _json_response({
        "session_id": session_id,
        "count": len(entries),
        "entries": [e.to_dict() for e in entries],
    })


async def api_entry(request: web.Request) -> web.Response:
    """GET /api/audit/entry/:id"""
    logger: AuditLogger = request.app["audit_logger"]
    entry_id = request.match_info["id"]
    entry = logger.get(entry_id)
    if entry is None:
        return _json_response({"error": "not found"}, 404)
    return _json_response(entry.to_dict())


async def api_export(request: web.Request) -> web.Response:
    """GET /api/audit/export?since="""
    logger: AuditLogger = request.app["audit_logger"]
    since = request.query.get("since", "")
    return _json_response(logger.export_json(since=since))


async def api_health(request: web.Request) -> web.Response:
    """GET /health"""
    return _json_response({
        "status": "ok",
        "service": "audit",
        "db": str(DB_PATH),
        "uptime": time.time() - request.app.get("start_time", time.time()),
    })


def build_app() -> web.Application:
    app = web.Application()
    app["audit_logger"] = get_logger()
    app["start_time"] = time.time()

    app.router.add_get("/api/audit/search", api_search)
    app.router.add_get("/api/audit/stats", api_stats)
    app.router.add_get("/api/audit/chain/{session_id}", api_chain)
    app.router.add_get("/api/audit/entry/{id}", api_entry)
    app.router.add_get("/api/audit/export", api_export)
    app.router.add_get("/health", api_health)

    return app


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_entries(entries: list[AuditEntry], compact: bool = False):
    if not entries:
        print("No results.")
        return

    if compact:
        for e in entries:
            ts = e.timestamp[:19]
            agent = e.agent.ljust(10)
            action = e.action.ljust(20)
            risk = e.risk_level.ljust(8)
            summary = e.result_summary[:60] if e.result_summary else ""
            print(f"{ts}  {agent}  {action}  {risk}  {summary}")
    else:
        for e in entries:
            print(f"  ID:        {e.id}")
            print(f"  Timestamp: {e.timestamp}")
            print(f"  Agent:     {e.agent}")
            print(f"  Action:    {e.action}")
            print(f"  Tool:      {e.tool or '-'}")
            print(f"  Risk:      {e.risk_level}")
            print(f"  Approval:  {e.approval_status}")
            print(f"  Score:     {e.evaluation_score}")
            if e.result_summary:
                print(f"  Result:    {e.result_summary[:120]}")
            if e.session_id:
                print(f"  Session:   {e.session_id}")
            if e.parent_action_id:
                print(f"  Parent:    {e.parent_action_id}")
            print("  " + "-" * 56)


def cmd_search(args):
    logger = get_logger()
    entries = logger.query(
        agent=args.agent,
        action=args.action,
        tool=args.tool,
        risk_level=args.risk,
        since=args.since,
        limit=args.limit,
    )
    if args.json:
        print(json.dumps([e.to_dict() for e in entries], indent=2, default=str))
    else:
        _print_entries(entries, compact=True)


def cmd_stats(args):
    logger = get_logger()
    s = logger.stats()
    if args.json:
        print(json.dumps(s, indent=2))
    else:
        print(f"\nAudit Trail Statistics")
        print(f"  Total entries:      {s['total']}")
        print(f"  High-risk entries:  {s['high_risk_count']}")
        print(f"  Avg eval score:     {s['avg_evaluation_score']}")
        print(f"  Date range:         {s['oldest'] or 'N/A'}  ->  {s['newest'] or 'N/A'}")

        if s["by_agent"]:
            print(f"\n  By agent:")
            for k, v in s["by_agent"].items():
                print(f"    {k or '(none)':<15} {v}")

        if s["by_action"]:
            print(f"\n  By action:")
            for k, v in s["by_action"].items():
                print(f"    {k or '(none)':<25} {v}")

        if s["by_risk_level"]:
            print(f"\n  By risk level:")
            for k, v in s["by_risk_level"].items():
                print(f"    {k:<15} {v}")

        if s["by_tool"]:
            print(f"\n  By tool:")
            for k, v in s["by_tool"].items():
                print(f"    {k:<20} {v}")

        if s["by_approval_status"]:
            print(f"\n  By approval status:")
            for k, v in s["by_approval_status"].items():
                print(f"    {k:<15} {v}")


def cmd_chain(args):
    logger = get_logger()
    entries = logger.get_chain(args.session_id)
    if not entries:
        print(f"No entries found for session {args.session_id}")
        return

    if args.json:
        print(json.dumps([e.to_dict() for e in entries], indent=2, default=str))
    else:
        print(f"\nAction chain for session: {args.session_id}")
        print(f"  Entries: {len(entries)}")
        print("  " + "-" * 56)
        for i, e in enumerate(entries):
            arrow = "-> " if i > 0 else "   "
            ts = e.timestamp[:19]
            action = e.action
            agent = e.agent
            risk = e.risk_level
            summary = e.result_summary[:50] if e.result_summary else ""
            parent_short = e.parent_action_id[:8] if e.parent_action_id else "root"
            print(f"  {arrow}[{ts}] {agent}/{action} (risk={risk}, parent={parent_short})")
            if summary:
                print(f"      {summary}")
        print("  " + "-" * 56)


def cmd_export(args):
    logger = get_logger()
    data = logger.export_json(since=args.since)

    if args.format == "json":
        print(json.dumps(data, indent=2, default=str))
    elif args.format == "jsonl":
        for entry in data:
            print(json.dumps(entry, default=str))
    else:
        print(json.dumps(data, indent=2, default=str))
        return

    print(f"\nExported {len(data)} entries.", file=sys.stderr)


def cmd_serve(args):
    if web is None:
        print("aiohttp is required: pip install aiohttp", file=sys.stderr)
        sys.exit(1)

    app = build_app()
    log.info("Audit API starting on %s:%s", API_HOST, API_PORT)
    web.run_app(app, host=API_HOST, port=API_PORT, print=log.info)


def main():
    _get_db()

    parser = argparse.ArgumentParser(
        description="Starship OS — Audit Trail",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")

    sp_search = sub.add_parser("search", help="Search audit entries")
    sp_search.add_argument("--agent", default="", help="Filter by agent")
    sp_search.add_argument("--action", default="", help="Filter by action")
    sp_search.add_argument("--tool", default="", help="Filter by tool")
    sp_search.add_argument("--risk", default="", help="Filter by risk level (low/medium/high/critical)")
    sp_search.add_argument("--since", default="", help="Time window (e.g. 30m, 6h, 7d)")
    sp_search.add_argument("--limit", type=int, default=100, help="Max results")
    sp_search.add_argument("--json", action="store_true", help="Output as JSON")

    sp_stats = sub.add_parser("stats", help="Show audit statistics")
    sp_stats.add_argument("--json", action="store_true", help="Output as JSON")

    sp_chain = sub.add_parser("chain", help="Reconstruct action chain")
    sp_chain.add_argument("session_id", help="Session ID to reconstruct")
    sp_chain.add_argument("--json", action="store_true", help="Output as JSON")

    sp_export = sub.add_parser("export", help="Export audit log for compliance")
    sp_export.add_argument("--since", default="", help="Time window (e.g. 30m, 6h, 7d)")
    sp_export.add_argument("--format", choices=["json", "jsonl"], default="json", help="Export format")

    sp_serve = sub.add_parser("serve", help="Start dashboard API server")

    args = parser.parse_args()

    if args.command == "search":
        cmd_search(args)
    elif args.command == "stats":
        cmd_stats(args)
    elif args.command == "chain":
        cmd_chain(args)
    elif args.command == "export":
        cmd_export(args)
    elif args.command == "serve":
        cmd_serve(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
