#!/usr/bin/env python3
"""
Starship OS — Structured Agent Handoff System

Manages delegation chains between agents with structured handoff documents.
Ensures the receiving agent has all necessary context: task, deliverable,
decisions, constraints, and accumulated history.

Usage:
    python3 handoff.py create --from proxy --to romi --task "Summarize logs"
    python3 handoff.py list [--agent proxy] [--limit 20]
    python3 handoff.py show <id>
    python3 handoff.py chain <id>        # show full delegation chain
    python3 handoff.py serve             # HTTP API server
    python3 handoff.py validate <id>     # validate completeness
    python3 handoff.py format <id>       # format for receiving agent
"""

import sys
import os
import json
import time
import signal
import asyncio
import sqlite3
import logging
import logging.handlers
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Optional

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

SCRIPT_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
PROJECT_ROOT = SCRIPT_DIR.parent
CONFIG_PATH = Path("/etc/romatic/handoff.yaml")
SYSTEM_CONFIG_PATH = Path("/etc/agnetic/handoff.yaml")

_db_dir = Path("/var/lib/agnetic")
if not os.access(_db_dir, os.W_OK):
    _db_dir = Path("/tmp/agnetic-data")
_db_dir.mkdir(parents=True, exist_ok=True)
DB_DIR = _db_dir
DB_PATH = DB_DIR / "handoff.db"

_log_dir = Path("/var/log/agnetic")
if not os.access(_log_dir, os.W_OK):
    _log_dir = Path("/tmp/agnetic-data/logs")
_log_dir.mkdir(parents=True, exist_ok=True)
LOG_DIR = _log_dir
LOG_FILE = LOG_DIR / "handoff.log"

_pid_dir = Path("/var/run/agnetic")
if not os.access(_pid_dir, os.W_OK):
    _pid_dir = Path("/tmp/agnetic-data")
_pid_dir.mkdir(parents=True, exist_ok=True)
PID_FILE = _pid_dir / "handoff.pid"

NATS_URL = os.getenv("NATS_URL", "nats://127.0.0.1:4222")
NATS_HANDOFF_SUBJECT = "agnetic.handoff.incoming"

DEFAULT_TIMEOUT = 300

# ---------------------------------------------------------------------------
# Default Config
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: dict[str, Any] = {
    "handoff": {
        "max_chain_depth": 3,
        "require_deliverable": True,
        "require_constraints": True,
        "default_timeout": DEFAULT_TIMEOUT,
        "server": {"host": "0.0.0.0", "port": 8930},
    },
}

VALID_PRIORITIES = {"low", "medium", "high", "critical"}

# ---------------------------------------------------------------------------
# Structured Logger (hitl / planner pattern)
# ---------------------------------------------------------------------------


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "service": getattr(record, "service", "handoff"),
            "event": getattr(record, "event", record.getMessage()),
        }
        details = getattr(record, "details", None)
        if details:
            entry["details"] = details
        return json.dumps(entry, default=str)


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("handoff")
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
    extra: dict[str, Any] = {"service": "handoff", "event": event}
    if details:
        extra["details"] = details
    getattr(log, level, log.info)(event, extra=extra)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def load_config() -> dict:
    """Load handoff config from YAML files, falling back to defaults."""
    default = json.loads(json.dumps(DEFAULT_CONFIG))

    for path in (SYSTEM_CONFIG_PATH, CONFIG_PATH):
        if path.exists() and yaml is not None:
            try:
                with open(path) as f:
                    cfg = yaml.safe_load(f) or {}
                _log("config_loaded", details={"path": str(path)})
                return _deep_merge(default, cfg)
            except Exception as exc:
                _log("config_load_failed", level="warning", details={"path": str(path), "error": str(exc)})

    return default


def _deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


def _get_db() -> sqlite3.Connection:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=3000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS handoffs (
            id              TEXT PRIMARY KEY,
            from_agent      TEXT NOT NULL,
            to_agent        TEXT NOT NULL,
            task            TEXT NOT NULL,
            deliverable     TEXT NOT NULL DEFAULT '',
            key_decisions    TEXT NOT NULL DEFAULT '[]',
            open_questions   TEXT NOT NULL DEFAULT '[]',
            constraints      TEXT NOT NULL DEFAULT '[]',
            context          TEXT NOT NULL DEFAULT '{}',
            priority         TEXT NOT NULL DEFAULT 'medium',
            deadline         TEXT NOT NULL DEFAULT '',
            history          TEXT NOT NULL DEFAULT '[]',
            parent_id        TEXT DEFAULT '',
            chain_id         TEXT NOT NULL DEFAULT '',
            chain_depth      INTEGER NOT NULL DEFAULT 0,
            status           TEXT NOT NULL DEFAULT 'pending',
            result           TEXT NOT NULL DEFAULT '',
            created_at       TEXT NOT NULL,
            accepted_at      TEXT,
            completed_at     TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ho_from ON handoffs(from_agent)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ho_to ON handoffs(to_agent)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ho_status ON handoffs(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ho_chain ON handoffs(chain_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ho_created ON handoffs(created_at)")
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------


@dataclass
class HandoffDocument:
    from_agent: str = ""
    to_agent: str = ""
    task: str = ""
    deliverable: str = ""
    key_decisions: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    context: dict = field(default_factory=dict)
    priority: str = "medium"
    deadline: str = ""
    history: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "HandoffDocument":
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class HandoffRecord:
    id: str = ""
    from_agent: str = ""
    to_agent: str = ""
    task: str = ""
    deliverable: str = ""
    key_decisions: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    context: dict = field(default_factory=dict)
    priority: str = "medium"
    deadline: str = ""
    history: list[dict] = field(default_factory=list)
    parent_id: str = ""
    chain_id: str = ""
    chain_depth: int = 0
    status: str = "pending"
    result: str = ""
    created_at: str = ""
    accepted_at: str = ""
    completed_at: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "from_agent": self.from_agent,
            "to_agent": self.to_agent,
            "task": self.task,
            "deliverable": self.deliverable,
            "key_decisions": self.key_decisions,
            "open_questions": self.open_questions,
            "constraints": self.constraints,
            "context": self.context,
            "priority": self.priority,
            "deadline": self.deadline,
            "history": self.history,
            "parent_id": self.parent_id,
            "chain_id": self.chain_id,
            "chain_depth": self.chain_depth,
            "status": self.status,
            "result": self.result,
            "created_at": self.created_at,
            "accepted_at": self.accepted_at,
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "HandoffRecord":
        return cls(
            id=row["id"],
            from_agent=row["from_agent"],
            to_agent=row["to_agent"],
            task=row["task"],
            deliverable=row["deliverable"],
            key_decisions=json.loads(row["key_decisions"]),
            open_questions=json.loads(row["open_questions"]),
            constraints=json.loads(row["constraints"]),
            context=json.loads(row["context"]),
            priority=row["priority"],
            deadline=row["deadline"],
            history=json.loads(row["history"]),
            parent_id=row["parent_id"],
            chain_id=row["chain_id"],
            chain_depth=row["chain_depth"],
            status=row["status"],
            result=row["result"],
            created_at=row["created_at"],
            accepted_at=row["accepted_at"] or "",
            completed_at=row["completed_at"] or "",
        )

    def to_document(self) -> HandoffDocument:
        return HandoffDocument(
            from_agent=self.from_agent,
            to_agent=self.to_agent,
            task=self.task,
            deliverable=self.deliverable,
            key_decisions=self.key_decisions,
            open_questions=self.open_questions,
            constraints=self.constraints,
            context=self.context,
            priority=self.priority,
            deadline=self.deadline,
            history=self.history,
        )


# ---------------------------------------------------------------------------
# HandoffManager
# ---------------------------------------------------------------------------


class HandoffManager:
    """Core handoff management: create, validate, chain, format, send."""

    def __init__(self, config: dict):
        self._cfg = config.get("handoff", DEFAULT_CONFIG["handoff"])
        self._max_depth = self._cfg.get("max_chain_depth", 3)
        self._require_deliverable = self._cfg.get("require_deliverable", True)
        self._require_constraints = self._cfg.get("require_constraints", True)
        self._default_timeout = self._cfg.get("default_timeout", DEFAULT_TIMEOUT)
        self._nc = None

    @property
    def max_chain_depth(self) -> int:
        return self._max_depth

    @property
    def default_timeout(self) -> int:
        return self._default_timeout

    # -- NATS ----------------------------------------------------------------

    async def connect_nats(self):
        if nats_mod is None:
            _log("nats_unavailable", level="warning")
            return
        try:
            self._nc = await nats_mod.connect(NATS_URL)
            _log("nats_connected", details={"url": NATS_URL})
        except Exception as exc:
            _log("nats_connect_failed", level="warning", details={"error": str(exc)})

    async def close_nats(self):
        if self._nc and not self._nc.is_closed:
            try:
                await self._nc.close()
            except Exception:
                pass

    async def _publish(self, subject: str, payload: dict):
        if not self._nc:
            return
        try:
            data = json.dumps(payload, default=str).encode()
            await self._nc.publish(subject, data)
        except Exception as exc:
            _log("publish_failed", level="warning", details={"subject": subject, "error": str(exc)})

    # -- Validation ----------------------------------------------------------

    def validate_handoff(self, doc: HandoffDocument) -> tuple[bool, list[str]]:
        """Validate a handoff document for completeness.

        Returns (is_valid, list of error messages).
        """
        errors: list[str] = []

        if not doc.from_agent:
            errors.append("from_agent is required")
        if not doc.to_agent:
            errors.append("to_agent is required")
        if not doc.task:
            errors.append("task is required")
        if doc.from_agent == doc.to_agent:
            errors.append("from_agent and to_agent must be different")
        if doc.priority not in VALID_PRIORITIES:
            errors.append(f"priority must be one of {sorted(VALID_PRIORITIES)}, got '{doc.priority}'")
        if self._require_deliverable and not doc.deliverable:
            errors.append("deliverable is required (require_deliverable=true)")
        if self._require_constraints and not doc.constraints:
            errors.append("at least one constraint is required (require_constraints=true)")

        return (len(errors) == 0, errors)

    def validate_chain_depth(self, parent_id: str) -> tuple[bool, str]:
        """Check if a new handoff would exceed the chain depth limit."""
        if not parent_id:
            return True, ""

        conn = _get_db()
        try:
            parent = conn.execute(
                "SELECT chain_depth FROM handoffs WHERE id = ?", (parent_id,)
            ).fetchone()
            if not parent:
                return True, ""
            depth = parent["chain_depth"] + 1
            if depth > self._max_depth:
                return False, (
                    f"Chain depth {depth} exceeds maximum of {self._max_depth}. "
                    f"Delegation chain: {' -> '.join(self._trace_chain(parent_id))}"
                )
            return True, ""
        finally:
            conn.close()

    def _trace_chain(self, handoff_id: str) -> list[str]:
        """Walk up the parent chain to build agent list."""
        conn = _get_db()
        try:
            chain: list[str] = []
            current = handoff_id
            visited = set()
            while current and current not in visited:
                visited.add(current)
                row = conn.execute(
                    "SELECT from_agent, to_agent, parent_id FROM handoffs WHERE id = ?",
                    (current,),
                ).fetchone()
                if not row:
                    break
                chain.append(f"{row['from_agent']}->{row['to_agent']}")
                current = row["parent_id"]
            chain.reverse()
            return chain
        finally:
            conn.close()

    # -- Create Handoff ------------------------------------------------------

    def create_handoff(
        self,
        document: HandoffDocument,
        parent_id: str = "",
    ) -> HandoffRecord:
        """Create and persist a new handoff record.

        Validates the document, checks chain depth, assigns chain ID,
        and records the handoff in SQLite.
        """
        is_valid, errors = self.validate_handoff(document)
        if not is_valid:
            raise ValueError(f"Invalid handoff: {'; '.join(errors)}")

        can_chain, chain_err = self.validate_chain_depth(parent_id)
        if not can_chain:
            raise ValueError(chain_err)

        now = datetime.now(timezone.utc)
        handoff_id = f"ho_{int(now.timestamp() * 1000)}_{uuid.uuid4().hex[:6]}"

        # Determine chain ID and depth
        chain_id = ""
        chain_depth = 0
        if parent_id:
            conn = _get_db()
            try:
                parent = conn.execute(
                    "SELECT chain_id, chain_depth FROM handoffs WHERE id = ?",
                    (parent_id,),
                ).fetchone()
                if parent:
                    chain_id = parent["chain_id"] or parent_id
                    chain_depth = parent["chain_depth"] + 1
            finally:
                conn.close()
        else:
            chain_id = handoff_id

        # Build history entry for this handoff
        history_entry = {
            "from_agent": document.from_agent,
            "to_agent": document.to_agent,
            "task": document.task[:200],
            "timestamp": now.isoformat(),
            "handoff_id": handoff_id,
        }

        # Merge incoming history with accumulated history
        full_history = list(document.history) + [history_entry]

        record = HandoffRecord(
            id=handoff_id,
            from_agent=document.from_agent,
            to_agent=document.to_agent,
            task=document.task,
            deliverable=document.deliverable,
            key_decisions=list(document.key_decisions),
            open_questions=list(document.open_questions),
            constraints=list(document.constraints),
            context=dict(document.context),
            priority=document.priority,
            deadline=document.deadline,
            history=full_history,
            parent_id=parent_id,
            chain_id=chain_id,
            chain_depth=chain_depth,
            status="pending",
            created_at=now.isoformat(),
        )

        conn = _get_db()
        conn.execute(
            "INSERT INTO handoffs "
            "(id, from_agent, to_agent, task, deliverable, key_decisions, "
            "open_questions, constraints, context, priority, deadline, "
            "history, parent_id, chain_id, chain_depth, status, result, "
            "created_at, accepted_at, completed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.id,
                record.from_agent,
                record.to_agent,
                record.task,
                record.deliverable,
                json.dumps(record.key_decisions),
                json.dumps(record.open_questions),
                json.dumps(record.constraints),
                json.dumps(record.context, default=str),
                record.priority,
                record.deadline,
                json.dumps(record.history, default=str),
                record.parent_id,
                record.chain_id,
                record.chain_depth,
                record.status,
                record.result,
                record.created_at,
                record.accepted_at,
                record.completed_at,
            ),
        )
        conn.commit()

        _log("handoff_created", details={
            "id": record.id,
            "from": record.from_agent,
            "to": record.to_agent,
            "chain_id": record.chain_id,
            "depth": record.chain_depth,
            "priority": record.priority,
        })

        return record

    # -- Accept / Complete ---------------------------------------------------

    def accept_handoff(self, handoff_id: str) -> HandoffRecord | None:
        """Mark a handoff as accepted by the receiving agent."""
        conn = _get_db()
        row = conn.execute("SELECT * FROM handoffs WHERE id = ?", (handoff_id,)).fetchone()
        if not row:
            return None

        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE handoffs SET status = 'accepted', accepted_at = ? WHERE id = ?",
            (now, handoff_id),
        )
        conn.commit()

        record = HandoffRecord.from_row(
            conn.execute("SELECT * FROM handoffs WHERE id = ?", (handoff_id,)).fetchone()
        )
        _log("handoff_accepted", details={"id": handoff_id, "to": record.to_agent})
        return record

    def complete_handoff(self, handoff_id: str, result: str = "") -> HandoffRecord | None:
        """Mark a handoff as completed with an optional result."""
        conn = _get_db()
        row = conn.execute("SELECT * FROM handoffs WHERE id = ?", (handoff_id,)).fetchone()
        if not row:
            return None

        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE handoffs SET status = 'completed', completed_at = ?, result = ? WHERE id = ?",
            (now, result, handoff_id),
        )
        conn.commit()

        record = HandoffRecord.from_row(
            conn.execute("SELECT * FROM handoffs WHERE id = ?", (handoff_id,)).fetchone()
        )
        _log("handoff_completed", details={
            "id": handoff_id,
            "from": record.from_agent,
            "to": record.to_agent,
            "result_len": len(result),
        })
        return record

    def fail_handoff(self, handoff_id: str, reason: str = "") -> HandoffRecord | None:
        """Mark a handoff as failed."""
        conn = _get_db()
        row = conn.execute("SELECT * FROM handoffs WHERE id = ?", (handoff_id,)).fetchone()
        if not row:
            return None

        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE handoffs SET status = 'failed', completed_at = ?, result = ? WHERE id = ?",
            (now, reason, handoff_id),
        )
        conn.commit()

        record = HandoffRecord.from_row(
            conn.execute("SELECT * FROM handoffs WHERE id = ?", (handoff_id,)).fetchone()
        )
        _log("handoff_failed", details={"id": handoff_id, "reason": reason[:200]})
        return record

    # -- Query ---------------------------------------------------------------

    def get_handoff(self, handoff_id: str) -> HandoffRecord | None:
        conn = _get_db()
        row = conn.execute("SELECT * FROM handoffs WHERE id = ?", (handoff_id,)).fetchone()
        if not row:
            return None
        return HandoffRecord.from_row(row)

    def list_handoffs(
        self,
        agent: str = "",
        status: str = "",
        chain_id: str = "",
        limit: int = 50,
    ) -> list[HandoffRecord]:
        conn = _get_db()
        clauses: list[str] = []
        params: list = []

        if agent:
            clauses.append("(from_agent = ? OR to_agent = ?)")
            params.extend([agent, agent])
        if status:
            clauses.append("status = ?")
            params.append(status)
        if chain_id:
            clauses.append("chain_id = ?")
            params.append(chain_id)

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT * FROM handoffs{where} ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(sql, params).fetchall()
        return [HandoffRecord.from_row(r) for r in rows]

    def get_chain(self, handoff_id: str) -> list[HandoffRecord]:
        """Get all handoffs in the same delegation chain, ordered by creation."""
        conn = _get_db()
        row = conn.execute("SELECT chain_id FROM handoffs WHERE id = ?", (handoff_id,)).fetchone()
        if not row:
            return []

        chain_id = row["chain_id"]
        rows = conn.execute(
            "SELECT * FROM handoffs WHERE chain_id = ? ORDER BY created_at ASC",
            (chain_id,),
        ).fetchall()
        return [HandoffRecord.from_row(r) for r in rows]

    # -- Format for Agent ----------------------------------------------------

    def format_for_agent(self, record: HandoffRecord) -> str:
        """Format a handoff record as LLM-readable text for the receiving agent."""
        lines = []

        lines.append("=" * 60)
        lines.append("AGENT HANDOFF DOCUMENT")
        lines.append("=" * 60)
        lines.append("")

        priority_icon = {
            "critical": "!!!",
            "high": "!!",
            "medium": "!",
            "low": "",
        }.get(record.priority, "!")

        lines.append(f"FROM:     {record.from_agent}")
        lines.append(f"TO:       {record.to_agent}")
        lines.append(f"PRIORITY: {record.priority.upper()} {priority_icon}")
        if record.deadline:
            lines.append(f"DEADLINE: {record.deadline}")
        if record.chain_depth > 0:
            lines.append(f"CHAIN:    depth {record.chain_depth} of {self._max_depth} max")
        lines.append("")

        lines.append("--- TASK ---")
        lines.append(record.task)
        lines.append("")

        lines.append("--- DELIVERABLE ---")
        lines.append(record.deliverable or "(not specified)")
        lines.append("")

        if record.key_decisions:
            lines.append("--- KEY DECISIONS (already made) ---")
            for i, decision in enumerate(record.key_decisions, 1):
                lines.append(f"  {i}. {decision}")
            lines.append("")

        if record.open_questions:
            lines.append("--- OPEN QUESTIONS (decide these) ---")
            for i, q in enumerate(record.open_questions, 1):
                lines.append(f"  {i}. {q}")
            lines.append("")

        if record.constraints:
            lines.append("--- CONSTRAINTS (rules & limitations) ---")
            for i, c in enumerate(record.constraints, 1):
                lines.append(f"  {i}. {c}")
            lines.append("")

        if record.context:
            lines.append("--- ACCUMULATED CONTEXT ---")
            for key, value in record.context.items():
                val_str = json.dumps(value, default=str) if isinstance(value, (dict, list)) else str(value)
                if len(val_str) > 500:
                    val_str = val_str[:500] + "..."
                lines.append(f"  {key}: {val_str}")
            lines.append("")

        if record.history:
            lines.append("--- DELEGATION HISTORY ---")
            for entry in record.history:
                from_ag = entry.get("from_agent", "?")
                to_ag = entry.get("to_agent", "?")
                task_preview = entry.get("task", "")[:80]
                lines.append(f"  {from_ag} -> {to_ag}: {task_preview}")
            lines.append("")

        lines.append(f"Handoff ID: {record.id}")
        lines.append(f"Created:    {record.created_at}")
        lines.append("=" * 60)

        return "\n".join(lines)

    # -- Stats ---------------------------------------------------------------

    def stats(self) -> dict:
        conn = _get_db()
        total = conn.execute("SELECT COUNT(*) as cnt FROM handoffs").fetchone()["cnt"]
        pending = conn.execute("SELECT COUNT(*) as cnt FROM handoffs WHERE status = 'pending'").fetchone()["cnt"]
        accepted = conn.execute("SELECT COUNT(*) as cnt FROM handoffs WHERE status = 'accepted'").fetchone()["cnt"]
        completed = conn.execute("SELECT COUNT(*) as cnt FROM handoffs WHERE status = 'completed'").fetchone()["cnt"]
        failed = conn.execute("SELECT COUNT(*) as cnt FROM handoffs WHERE status = 'failed'").fetchone()["cnt"]
        chains = conn.execute("SELECT COUNT(DISTINCT chain_id) as cnt FROM handoffs").fetchone()["cnt"]
        return {
            "total": total,
            "pending": pending,
            "accepted": accepted,
            "completed": completed,
            "failed": failed,
            "active_chains": chains,
        }


# ---------------------------------------------------------------------------
# Convenience: delegate_with_handoff (for agents/tools.py integration)
# ---------------------------------------------------------------------------


async def delegate_with_handoff(
    nats,
    from_agent: str,
    to_agent: str,
    handoff: HandoffDocument,
    manager: HandoffManager | None = None,
) -> dict:
    """Send a structured handoff via NATS.

    This is the integration point for agents/tools.py. It creates a
    handoff record, validates it, formats it for the receiving agent,
    and publishes it on the NATS bus.
    """
    if manager is None:
        config = load_config()
        manager = HandoffManager(config)

    is_valid, errors = manager.validate_handoff(handoff)
    if not is_valid:
        return {"error": True, "message": f"Invalid handoff: {'; '.join(errors)}"}

    record = manager.create_handoff(handoff)
    formatted = manager.format_for_agent(record)

    # Publish to NATS
    subject = f"agnetic.handoff.{to_agent}"
    payload = {
        "type": "handoff",
        "handoff_id": record.id,
        "from_agent": from_agent,
        "to_agent": to_agent,
        "task": handoff.task,
        "priority": handoff.priority,
        "formatted": formatted,
        "record": record.to_dict(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    if nats and not getattr(nats, "is_closed", True):
        try:
            data = json.dumps(payload, default=str).encode()
            await nats.publish(subject, data)
            await nats.publish(NATS_HANDOFF_SUBJECT, data)
        except Exception as exc:
            _log("handoff_nats_publish_failed", level="warning", details={
                "handoff_id": record.id, "error": str(exc),
            })

    _log("delegate_with_handoff", details={
        "handoff_id": record.id,
        "from": from_agent,
        "to": to_agent,
    })

    return {
        "status": "sent",
        "handoff_id": record.id,
        "from_agent": from_agent,
        "to_agent": to_agent,
        "chain_id": record.chain_id,
        "chain_depth": record.chain_depth,
        "priority": handoff.priority,
    }


# ---------------------------------------------------------------------------
# HTTP API (hitl / planner pattern)
# ---------------------------------------------------------------------------


def _json_response(data: dict, status: int = 200) -> web.Response:
    return web.json_response(data, status=status)


async def api_create_handoff(request: web.Request) -> web.Response:
    """POST /api/handoffs/create — create a new handoff."""
    manager: HandoffManager = request.app["handoff_manager"]

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _json_response({"error": "invalid JSON"}, 400)

    from_agent = body.get("from_agent", "")
    to_agent = body.get("to_agent", "")
    task = body.get("task", "")

    if not all([from_agent, to_agent, task]):
        return _json_response({"error": "from_agent, to_agent, and task are required"}, 400)

    doc = HandoffDocument(
        from_agent=from_agent,
        to_agent=to_agent,
        task=task,
        deliverable=body.get("deliverable", ""),
        key_decisions=body.get("key_decisions", []),
        open_questions=body.get("open_questions", []),
        constraints=body.get("constraints", []),
        context=body.get("context", {}),
        priority=body.get("priority", "medium"),
        deadline=body.get("deadline", ""),
        history=body.get("history", []),
    )

    parent_id = body.get("parent_id", "")

    try:
        record = manager.create_handoff(doc, parent_id=parent_id)
    except ValueError as exc:
        return _json_response({"error": str(exc)}, 400)

    # Publish via NATS
    if manager._nc and not manager._nc.is_closed:
        subject = f"agnetic.handoff.{to_agent}"
        payload = {
            "type": "handoff",
            "handoff_id": record.id,
            "from_agent": from_agent,
            "to_agent": to_agent,
            "task": task,
            "priority": doc.priority,
            "record": record.to_dict(),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        await manager._publish(subject, payload)
        await manager._publish(NATS_HANDOFF_SUBJECT, payload)

    return _json_response({"status": "ok", "handoff": record.to_dict()})


async def api_list_handoffs(request: web.Request) -> web.Response:
    """GET /api/handoffs — list handoffs with optional filters."""
    manager: HandoffManager = request.app["handoff_manager"]
    agent = request.query.get("agent", "")
    status = request.query.get("status", "")
    chain_id = request.query.get("chain_id", "")
    limit = int(request.query.get("limit", "50"))

    records = manager.list_handoffs(agent=agent, status=status, chain_id=chain_id, limit=limit)
    return _json_response({
        "status": "ok",
        "count": len(records),
        "handoffs": [r.to_dict() for r in records],
    })


async def api_get_handoff(request: web.Request) -> web.Response:
    """GET /api/handoffs/{id} — get a single handoff."""
    manager: HandoffManager = request.app["handoff_manager"]
    handoff_id = request.match_info.get("id", "")
    record = manager.get_handoff(handoff_id)
    if not record:
        return _json_response({"error": f"handoff {handoff_id} not found"}, 404)
    return _json_response({"status": "ok", "handoff": record.to_dict()})


async def api_get_chain(request: web.Request) -> web.Response:
    """GET /api/handoffs/{id}/chain — get full delegation chain."""
    manager: HandoffManager = request.app["handoff_manager"]
    handoff_id = request.match_info.get("id", "")
    records = manager.get_chain(handoff_id)
    if not records:
        return _json_response({"error": f"no chain found for {handoff_id}"}, 404)
    return _json_response({
        "status": "ok",
        "chain_id": records[0].chain_id,
        "depth": len(records),
        "handoffs": [r.to_dict() for r in records],
    })


async def api_accept_handoff(request: web.Request) -> web.Response:
    """POST /api/handoffs/{id}/accept — mark as accepted."""
    manager: HandoffManager = request.app["handoff_manager"]
    handoff_id = request.match_info.get("id", "")
    record = manager.accept_handoff(handoff_id)
    if not record:
        return _json_response({"error": f"handoff {handoff_id} not found"}, 404)
    return _json_response({"status": "ok", "handoff": record.to_dict()})


async def api_complete_handoff(request: web.Request) -> web.Response:
    """POST /api/handoffs/{id}/complete — mark as completed with result."""
    manager: HandoffManager = request.app["handoff_manager"]
    handoff_id = request.match_info.get("id", "")
    try:
        body = await request.json()
    except Exception:
        body = {}
    result = body.get("result", "")
    record = manager.complete_handoff(handoff_id, result=result)
    if not record:
        return _json_response({"error": f"handoff {handoff_id} not found"}, 404)
    return _json_response({"status": "ok", "handoff": record.to_dict()})


async def api_fail_handoff(request: web.Request) -> web.Response:
    """POST /api/handoffs/{id}/fail — mark as failed."""
    manager: HandoffManager = request.app["handoff_manager"]
    handoff_id = request.match_info.get("id", "")
    try:
        body = await request.json()
    except Exception:
        body = {}
    reason = body.get("reason", "")
    record = manager.fail_handoff(handoff_id, reason=reason)
    if not record:
        return _json_response({"error": f"handoff {handoff_id} not found"}, 404)
    return _json_response({"status": "ok", "handoff": record.to_dict()})


async def api_validate(request: web.Request) -> web.Response:
    """POST /api/handoffs/validate — validate a handoff document without creating it."""
    manager: HandoffManager = request.app["handoff_manager"]
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _json_response({"error": "invalid JSON"}, 400)

    doc = HandoffDocument(
        from_agent=body.get("from_agent", ""),
        to_agent=body.get("to_agent", ""),
        task=body.get("task", ""),
        deliverable=body.get("deliverable", ""),
        key_decisions=body.get("key_decisions", []),
        open_questions=body.get("open_questions", []),
        constraints=body.get("constraints", []),
        context=body.get("context", {}),
        priority=body.get("priority", "medium"),
        deadline=body.get("deadline", ""),
        history=body.get("history", []),
    )

    is_valid, errors = manager.validate_handoff(doc)
    return _json_response({"valid": is_valid, "errors": errors})


async def api_format(request: web.Request) -> web.Response:
    """GET /api/handoffs/{id}/format — format handoff as agent-readable text."""
    manager: HandoffManager = request.app["handoff_manager"]
    handoff_id = request.match_info.get("id", "")
    record = manager.get_handoff(handoff_id)
    if not record:
        return _json_response({"error": f"handoff {handoff_id} not found"}, 404)
    formatted = manager.format_for_agent(record)
    return _json_response({"status": "ok", "formatted": formatted})


async def api_stats(request: web.Request) -> web.Response:
    """GET /api/handoffs/stats — handoff statistics."""
    manager: HandoffManager = request.app["handoff_manager"]
    stats = manager.stats()
    return _json_response({"status": "ok", "stats": stats})


async def api_health(request: web.Request) -> web.Response:
    """GET /api/handoffs/health — health check."""
    manager: HandoffManager = request.app["handoff_manager"]
    return _json_response({
        "status": "healthy",
        "service": "handoff",
        "max_chain_depth": manager.max_chain_depth,
        "default_timeout": manager.default_timeout,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "uptime": time.time() - request.app.get("start_time", time.time()),
    })


# ---------------------------------------------------------------------------
# HTTP Server
# ---------------------------------------------------------------------------


def build_app(manager: HandoffManager) -> web.Application:
    app = web.Application()
    app["handoff_manager"] = manager
    app["start_time"] = time.time()

    app.router.add_post("/api/handoffs/create", api_create_handoff)
    app.router.add_get("/api/handoffs", api_list_handoffs)
    app.router.add_get("/api/handoffs/stats", api_stats)
    app.router.add_get("/api/handoffs/health", api_health)
    app.router.add_post("/api/handoffs/validate", api_validate)
    app.router.add_get("/api/handoffs/{id}", api_get_handoff)
    app.router.add_get("/api/handoffs/{id}/chain", api_get_chain)
    app.router.add_get("/api/handoffs/{id}/format", api_format)
    app.router.add_post("/api/handoffs/{id}/accept", api_accept_handoff)
    app.router.add_post("/api/handoffs/{id}/complete", api_complete_handoff)
    app.router.add_post("/api/handoffs/{id}/fail", api_fail_handoff)

    return app


async def start_background_tasks(app: web.Application):
    manager: HandoffManager = app["handoff_manager"]
    await manager.connect_nats()


async def cleanup_background_tasks(app: web.Application):
    manager: HandoffManager = app["handoff_manager"]
    await manager.close_nats()


# ---------------------------------------------------------------------------
# NATS Subscriber — listen for handoff events
# ---------------------------------------------------------------------------


async def setup_nats_subscribers(manager: HandoffManager):
    """Subscribe to NATS subjects for handoff lifecycle events."""
    if not manager._nc:
        return

    async def on_handoff_complete(msg):
        try:
            data = json.loads(msg.data.decode())
            handoff_id = data.get("handoff_id", "")
            if handoff_id:
                manager.complete_handoff(handoff_id, result=data.get("result", ""))
        except Exception as exc:
            _log("nats_handoff_complete_error", level="warning", details={"error": str(exc)})

    async def on_handoff_fail(msg):
        try:
            data = json.loads(msg.data.decode())
            handoff_id = data.get("handoff_id", "")
            if handoff_id:
                manager.fail_handoff(handoff_id, reason=data.get("reason", ""))
        except Exception as exc:
            _log("nats_handoff_fail_error", level="warning", details={"error": str(exc)})

    await manager._nc.subscribe("agnetic.handoff.command.complete", cb=on_handoff_complete)
    await manager._nc.subscribe("agnetic.handoff.command.fail", cb=on_handoff_fail)
    _log("nats_subscribers_setup")


# ---------------------------------------------------------------------------
# CLI Commands
# ---------------------------------------------------------------------------


def cmd_create(args: list[str]):
    """CLI: create --from AGENT --to AGENT --task TASK [options]"""
    import argparse
    parser = argparse.ArgumentParser(description="Create a structured handoff")
    parser.add_argument("--from", dest="from_agent", required=True, help="Source agent")
    parser.add_argument("--to", dest="to_agent", required=True, help="Target agent")
    parser.add_argument("--task", required=True, help="Task description")
    parser.add_argument("--deliverable", default="", help="What success looks like")
    parser.add_argument("--priority", default="medium", choices=sorted(VALID_PRIORITIES))
    parser.add_argument("--deadline", default="", help="Optional deadline")
    parser.add_argument("--parent", default="", help="Parent handoff ID for chaining")
    parser.add_argument("--json", action="store_true", dest="as_json")
    opts = parser.parse_args(args)

    config = load_config()
    manager = HandoffManager(config)

    doc = HandoffDocument(
        from_agent=opts.from_agent,
        to_agent=opts.to_agent,
        task=opts.task,
        deliverable=opts.deliverable,
        priority=opts.priority,
        deadline=opts.deadline,
    )

    try:
        record = manager.create_handoff(doc, parent_id=opts.parent)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    if opts.as_json:
        print(json.dumps(record.to_dict(), indent=2, default=str))
    else:
        print(manager.format_for_agent(record))


def cmd_list(args: list[str]):
    """CLI: list [--agent AGENT] [--status STATUS] [--limit N] [--json]"""
    import argparse
    parser = argparse.ArgumentParser(description="List handoffs")
    parser.add_argument("--agent", default="", help="Filter by agent")
    parser.add_argument("--status", default="", help="Filter by status")
    parser.add_argument("--chain", default="", help="Filter by chain ID")
    parser.add_argument("--limit", type=int, default=20, help="Max results")
    parser.add_argument("--json", action="store_true", dest="as_json")
    opts = parser.parse_args(args)

    config = load_config()
    manager = HandoffManager(config)

    records = manager.list_handoffs(
        agent=opts.agent,
        status=opts.status,
        chain_id=opts.chain,
        limit=opts.limit,
    )

    if opts.as_json:
        print(json.dumps([r.to_dict() for r in records], indent=2, default=str))
        return

    if not records:
        print("No handoffs found.")
        return

    print(f"\n{'ID':<28} {'From':<10} {'To':<10} {'Priority':<10} {'Status':<10} {'Task'}")
    print("-" * 90)
    for r in records:
        task_short = r.task[:30] + ("..." if len(r.task) > 30 else "")
        print(f"{r.id:<28} {r.from_agent:<10} {r.to_agent:<10} {r.priority:<10} {r.status:<10} {task_short}")
    print(f"\n{len(records)} handoff(s)")


def cmd_show(args: list[str]):
    """CLI: show <id> [--json] [--format]"""
    import argparse
    parser = argparse.ArgumentParser(description="Show handoff details")
    parser.add_argument("id", help="Handoff ID")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--format", action="store_true", dest="as_formatted",
                        help="Show formatted for receiving agent")
    opts = parser.parse_args(args)

    config = load_config()
    manager = HandoffManager(config)

    record = manager.get_handoff(opts.id)
    if not record:
        print(f"Handoff {opts.id} not found", file=sys.stderr)
        sys.exit(1)

    if opts.as_formatted:
        print(manager.format_for_agent(record))
    elif opts.as_json:
        print(json.dumps(record.to_dict(), indent=2, default=str))
    else:
        _print_handoff(record)


def cmd_chain(args: list[str]):
    """CLI: chain <id> [--json]"""
    import argparse
    parser = argparse.ArgumentParser(description="Show delegation chain")
    parser.add_argument("id", help="Any handoff ID in the chain")
    parser.add_argument("--json", action="store_true", dest="as_json")
    opts = parser.parse_args(args)

    config = load_config()
    manager = HandoffManager(config)

    records = manager.get_chain(opts.id)
    if not records:
        print(f"No chain found for {opts.id}", file=sys.stderr)
        sys.exit(1)

    if opts.as_json:
        print(json.dumps({
            "chain_id": records[0].chain_id,
            "depth": len(records),
            "handoffs": [r.to_dict() for r in records],
        }, indent=2, default=str))
        return

    chain_id = records[0].chain_id
    print(f"\nChain: {chain_id} ({len(records)} handoff(s))\n")
    for i, r in enumerate(records):
        arrow = " -> " if i < len(records) - 1 else ""
        icon = {"completed": "✓", "accepted": "►", "failed": "✗", "pending": "○"}.get(r.status, "?")
        print(f"  {icon} {r.from_agent} -> {r.to_agent} [{r.status}]")
        print(f"    Task: {r.task[:80]}")
        if r.deliverable:
            print(f"    Deliverable: {r.deliverable[:80]}")
        if i < len(records) - 1:
            print(f"    |")
    print()


def cmd_validate(args: list[str]):
    """CLI: validate <id>"""
    if not args:
        print("usage: handoff.py validate <id>", file=sys.stderr)
        sys.exit(1)

    config = load_config()
    manager = HandoffManager(config)

    record = manager.get_handoff(args[0])
    if not record:
        print(f"Handoff {args[0]} not found", file=sys.stderr)
        sys.exit(1)

    doc = record.to_document()
    is_valid, errors = manager.validate_handoff(doc)

    if is_valid:
        print(f"Handoff {args[0]} is valid.")
    else:
        print(f"Handoff {args[0]} has issues:")
        for err in errors:
            print(f"  - {err}")
        sys.exit(1)


def cmd_format(args: list[str]):
    """CLI: format <id>"""
    if not args:
        print("usage: handoff.py format <id>", file=sys.stderr)
        sys.exit(1)

    config = load_config()
    manager = HandoffManager(config)

    record = manager.get_handoff(args[0])
    if not record:
        print(f"Handoff {args[0]} not found", file=sys.stderr)
        sys.exit(1)

    print(manager.format_for_agent(record))


def cmd_serve(args: list[str]):
    """CLI: serve — start HTTP API server."""
    if web is None:
        print("aiohttp is required: pip install aiohttp", file=sys.stderr)
        sys.exit(1)

    config = load_config()
    manager = HandoffManager(config)
    host = config.get("handoff", {}).get("server", {}).get("host", "0.0.0.0")
    port = config.get("handoff", {}).get("server", {}).get("port", 8930)

    _log("server_starting", details={"host": host, "port": port})

    app = build_app(manager)
    app.on_startup.append(start_background_tasks)
    app.on_cleanup.append(cleanup_background_tasks)

    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))

    def _shutdown(signum, _frame):
        _log("signal_received", details={"signal": signum})
        try:
            PID_FILE.unlink()
        except OSError:
            pass

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        web.run_app(app, host=host, port=port, print=None)
    finally:
        try:
            PID_FILE.unlink()
        except OSError:
            pass


def _print_handoff(record: HandoffRecord):
    """Pretty-print a handoff record."""
    status_icons = {
        "completed": "✓",
        "accepted": "►",
        "failed": "✗",
        "pending": "○",
    }
    icon = status_icons.get(record.status, "?")

    print(f"\n{'='*60}")
    print(f"Handoff: {record.id}")
    print(f"Status:  {icon} {record.status}")
    print(f"Chain:   {record.chain_id} (depth {record.chain_depth})")
    print(f"{'='*60}\n")
    print(f"  From:       {record.from_agent}")
    print(f"  To:         {record.to_agent}")
    print(f"  Priority:   {record.priority}")
    if record.deadline:
        print(f"  Deadline:   {record.deadline}")
    print(f"  Created:    {record.created_at}")
    if record.accepted_at:
        print(f"  Accepted:   {record.accepted_at}")
    if record.completed_at:
        print(f"  Completed:  {record.completed_at}")
    print()
    print(f"  Task: {record.task}")
    if record.deliverable:
        print(f"  Deliverable: {record.deliverable}")
    if record.result:
        print(f"  Result: {record.result[:300]}")
    print()

    if record.key_decisions:
        print("  Key Decisions:")
        for d in record.key_decisions:
            print(f"    - {d}")
        print()

    if record.open_questions:
        print("  Open Questions:")
        for q in record.open_questions:
            print(f"    - {q}")
        print()

    if record.constraints:
        print("  Constraints:")
        for c in record.constraints:
            print(f"    - {c}")
        print()

    if record.context:
        print("  Context:")
        for k, v in record.context.items():
            val_str = json.dumps(v, default=str) if isinstance(v, (dict, list)) else str(v)
            print(f"    {k}: {val_str[:200]}")
        print()

    if record.history:
        print("  Delegation History:")
        for entry in record.history:
            print(f"    {entry.get('from_agent', '?')} -> {entry.get('to_agent', '?')}: {entry.get('task', '')[:60]}")
        print()


def cmd_stats(args: list[str]):
    """CLI: stats"""
    config = load_config()
    manager = HandoffManager(config)
    stats = manager.stats()
    print(f"\nHandoff Statistics")
    print(f"  Total:          {stats['total']}")
    print(f"  Pending:        {stats['pending']}")
    print(f"  Accepted:       {stats['accepted']}")
    print(f"  Completed:      {stats['completed']}")
    print(f"  Failed:         {stats['failed']}")
    print(f"  Active Chains:  {stats['active_chains']}")
    print()


def cmd_help():
    print("""\
Starship OS Structured Agent Handoff System

Usage:
  python3 handoff.py <command> [args]

Commands:
  create                              Create a new handoff
    --from AGENT                      Source agent (required)
    --to AGENT                        Target agent (required)
    --task TASK                       Task description (required)
    --deliverable TEXT                What success looks like
    --priority PRIORITY               low/medium/high/critical
    --deadline TEXT                   Optional deadline
    --parent ID                       Parent handoff ID for chaining
    --json                            Output as JSON

  list                                List handoffs
    --agent NAME                      Filter by agent name
    --status STATUS                   Filter by status
    --chain ID                        Filter by chain ID
    --limit N                         Max results (default 20)
    --json                            Output as JSON

  show <id>                           Show handoff details
    --json                            Output as JSON
    --format                          Show formatted for receiving agent

  chain <id>                          Show full delegation chain
    --json                            Output as JSON

  validate <id>                       Validate handoff completeness
  format <id>                         Format handoff as agent-readable text
  stats                               Show handoff statistics

  serve                               Start HTTP API server (port 8930)

  help                                Show this help message

API Endpoints (when running serve):
  POST /api/handoffs/create           Create a handoff
  GET  /api/handoffs                  List handoffs
  GET  /api/handoffs/stats            Statistics
  GET  /api/handoffs/health           Health check
  POST /api/handoffs/validate         Validate without creating
  GET  /api/handoffs/{id}             Get a single handoff
  GET  /api/handoffs/{id}/chain       Get delegation chain
  GET  /api/handoffs/{id}/format      Format for receiving agent
  POST /api/handoffs/{id}/accept      Mark as accepted
  POST /api/handoffs/{id}/complete    Mark as completed
  POST /api/handoffs/{id}/fail        Mark as failed
""")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    _get_db()  # ensure tables

    if len(sys.argv) < 2:
        cmd_help()
        sys.exit(0)

    command = sys.argv[1]
    rest = sys.argv[2:]

    commands = {
        "create": lambda: cmd_create(rest),
        "list": lambda: cmd_list(rest),
        "show": lambda: cmd_show(rest),
        "chain": lambda: cmd_chain(rest),
        "validate": lambda: cmd_validate(rest),
        "format": lambda: cmd_format(rest),
        "stats": lambda: cmd_stats(rest),
        "serve": lambda: cmd_serve(rest),
        "help": lambda: cmd_help(),
    }

    handler = commands.get(command)
    if handler:
        handler()
    else:
        print(f"Unknown command: {command}", file=sys.stderr)
        cmd_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
