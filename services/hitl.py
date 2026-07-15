#!/usr/bin/env python3
"""
Starship OS — Human-in-the-Loop (HITL) Approval System

Gates high-stakes agent actions behind human approval. When an agent tries
to perform a dangerous operation, the system pauses and waits for explicit
human sign-off before proceeding.

Usage:
    python3 hitl.py serve                     # start approval API server
    python3 hitl.py pending                   # list pending approvals
    python3 hitl.py approve <id> [reason]     # approve a request
    python3 hitl.py deny <id> [reason]        # deny a request
    python3 hitl.py history                   # show approval history
    python3 hitl.py analyze <command>         # analyze shell command risk
    python3 hitl.py config                    # show current HITL config
"""

import sys
import os
import json
import time
import signal
import asyncio
import re
import sqlite3
import logging
import logging.handlers
import uuid
from enum import Enum
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Optional
from dataclasses import dataclass, field, asdict

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
CONFIG_PATH = Path("/etc/romatic/hitl.yaml")
SYSTEM_CONFIG_PATH = Path("/etc/agnetic/hitl.yaml")

_db_dir = Path("/var/lib/agnetic")
if not os.access(_db_dir, os.W_OK):
    _db_dir = Path("/tmp/agnetic-data")
_db_dir.mkdir(parents=True, exist_ok=True)
DB_DIR = _db_dir
DB_PATH = DB_DIR / "hitl.db"

_log_dir = Path("/var/log/agnetic")
if not os.access(_log_dir, os.W_OK):
    _log_dir = Path("/tmp/agnetic-data/logs")
_log_dir.mkdir(parents=True, exist_ok=True)
LOG_DIR = _log_dir
LOG_FILE = LOG_DIR / "hitl.log"

_pid_dir = Path("/var/run/agnetic")
if not os.access(_pid_dir, os.W_OK):
    _pid_dir = Path("/tmp/romatic-data")
_pid_dir.mkdir(parents=True, exist_ok=True)
PID_FILE = _pid_dir / "hitl.pid"

NATS_URL = os.getenv("NATS_URL", "nats://127.0.0.1:4222")

SUBJECT_APPROVAL_PENDING = "agnetic.approval.pending"
SUBJECT_APPROVAL_APPROVED = "agnetic.approval.approved"
SUBJECT_APPROVAL_DENIED = "matic.approval.denied"
SUBJECT_APPROVAL_EXPIRED = "agnetic.approval.expired"

DEFAULT_TIMEOUT = 300  # 5 minutes

# ---------------------------------------------------------------------------
# Approval Levels
# ---------------------------------------------------------------------------


class ApprovalLevel(Enum):
    AUTO = "auto"
    NOTIFICATION = "notify"
    APPROVE = "approve"
    CRITICAL = "critical"


# ---------------------------------------------------------------------------
# Default Tool Risk Matrix
# ---------------------------------------------------------------------------

DEFAULT_TOOL_RISK: dict[str, str] = {
    "read_file": ApprovalLevel.AUTO.value,
    "list_dir": ApprovalLevel.AUTO.value,
    "search_files": ApprovalLevel.AUTO.value,
    "http_get": ApprovalLevel.NOTIFICATION.value,
    "http_post": ApprovalLevel.NOTIFICATION.value,
    "delegate_to_agent": ApprovalLevel.NOTIFICATION.value,
    "shell": ApprovalLevel.APPROVE.value,
    "write_file": ApprovalLevel.APPROVE.value,
    "opencode": ApprovalLevel.CRITICAL.value,
    "opendesign": ApprovalLevel.CRITICAL.value,
}

# ---------------------------------------------------------------------------
# Default Config
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: dict[str, Any] = {
    "hitl": {
        "enabled": True,
        "timeout": DEFAULT_TIMEOUT,
        "server": {"host": "0.0.0.0", "port": 8910},
        "auto_approve": ["read_file", "list_dir", "search_files", "http_get"],
        "always_approve": ["shell", "write_file"],
        "critical": ["opencode", "opendesign"],
        "notification": ["http_get", "http_post", "delegate_to_agent"],
        "agent_overrides": {},
    },
}

# ---------------------------------------------------------------------------
# Shell Command Risk Analysis
# ---------------------------------------------------------------------------

SHELL_LOW_RISK = frozenset({
    "ls", "cat", "grep", "find", "echo", "pwd", "date", "whoami",
    "which", "file", "stat", "head", "tail", "wc", "sort", "uniq",
    "diff", "df", "du", "free", "uname", "hostname", "id", "env",
    "printenv", "type", "readlink", "basename", "dirname", "realpath",
})

SHELL_MEDIUM_RISK = frozenset({
    "curl", "wget", "pip", "pip3", "apt", "apt-get", "npm", "npx",
    "yarn", "cargo", "go", "docker", "podman", "ssh", "scp", "rsync",
    "git", "svn", "hg", "tar", "zip", "unzip", "gzip", "bzip2",
    "ffmpeg", "convert", "python", "python3", "node", "ruby", "perl",
})

SHELL_HIGH_RISK = frozenset({
    "rm", "mv", "cp", "chmod", "chown", "chgrp", "mkdir", "touch",
    "ln", "rmdir", "dd", "mkfs", "mount", "umount", "kill", "killall",
    "pkill", "su", "passwd", "useradd", "userdel", "groupadd",
    "iptables", "ip6tables", "nft", "sysctl", "modprobe", "rmmod",
})

SHELL_CRITICAL_RISK = frozenset({
    "rm -rf", "rm -fr", "mkfs", "dd", "> /dev/", "shutdown",
    "reboot", "halt", "poweroff", "init 0", "init 6",
    "sudo rm", "sudo rm -rf", "sudo rm -fr",
    ":(){ :|:& };:", "chmod -R 777", "chown -R",
    "> /etc/", "mv /*", "rm -rf /", "rm -rf /*",
})

HIGH_RISK_FLAGS = frozenset({
    "-rf", "-fr", "-f -r", "-r -f", "--no-preserve=root",
    "--recursive", "--force",
})


@dataclass
class ShellRisk:
    risk_level: str  # low, medium, high, critical
    reasons: list[str] = field(default_factory=list)
    suggestion: str = ""


class ShellRiskAnalyzer:
    """Analyze shell commands for risk level."""

    def analyze(self, command: str) -> ShellRisk:
        stripped = command.strip()
        if not stripped:
            return ShellRisk(risk_level="low", reasons=["empty command"])

        tokens = self._tokenize(stripped)
        base_cmd = tokens[0] if tokens else ""
        remainder = " ".join(tokens[1:]) if len(tokens) > 1 else ""
        flags = set(t for t in tokens[1:] if t.startswith("-"))

        reasons: list[str] = []

        # Critical patterns — regex-based for precision
        # rm -rf / (root itself) or rm -rf /* (everything) but NOT rm -rf /var/tmp
        rm_rf_root = re.match(r"^rm\s+-(?:r?f|f ?r)\s+/\s*$", stripped)
        rm_rf_star = re.match(r"^rm\s+-(?:r?f|f ?r)\s+/\*\s*$", stripped)
        if rm_rf_root or rm_rf_star:
            return ShellRisk(
                risk_level="critical",
                reasons=["Recursive delete of root filesystem"],
                suggestion="This operation is catastrophic and irreversible",
            )
        if re.search(r">\s*/dev/", stripped):
            return ShellRisk(
                risk_level="critical",
                reasons=["Direct write to device node"],
                suggestion="This operation can corrupt block devices",
            )
        if re.match(r"^mkfs\b", stripped):
            return ShellRisk(
                risk_level="critical",
                reasons=["Filesystem formatting detected"],
                suggestion="This will destroy all data on the target device",
            )
        if ":(){ :|:& };:" in stripped:
            return ShellRisk(
                risk_level="critical",
                reasons=["Fork bomb detected"],
                suggestion="This will consume all system resources",
            )
        if base_cmd in ("shutdown", "reboot", "halt", "poweroff"):
            return ShellRisk(
                risk_level="critical",
                reasons=[f"System {base_cmd} command detected"],
                suggestion="This will bring down the entire system",
            )

        # Sudo detection
        if base_cmd == "sudo":
            if len(tokens) > 1:
                inner_cmd = tokens[1]
                inner_args = tokens[2:]
                combined = f"{inner_cmd} {' '.join(inner_args)}"
                for crit in SHELL_CRITICAL_RISK:
                    if crit in combined:
                        return ShellRisk(
                            risk_level="critical",
                            reasons=[f"sudo with dangerous inner command: {inner_cmd}"],
                            suggestion="Run without sudo or use a safer alternative",
                        )
                if inner_cmd in SHELL_HIGH_RISK:
                    reasons.append(f"sudo elevates {inner_cmd} to root privileges")
                    return ShellRisk(
                        risk_level="high",
                        reasons=reasons,
                        suggestion=f"Consider running {inner_cmd} without sudo if possible",
                    )
            reasons.append("sudo elevates privileges")
            return ShellRisk(
                risk_level="high",
                reasons=reasons,
                suggestion="Ensure the operation truly requires root",
            )

        # Critical base commands
        if base_cmd in SHELL_CRITICAL_RISK:
            return ShellRisk(
                risk_level="critical",
                reasons=[f"Base command '{base_cmd}' is critical risk"],
                suggestion="Avoid this command entirely",
            )

        # High risk commands
        if base_cmd in SHELL_HIGH_RISK:
            reasons.append(f"Base command '{base_cmd}' can modify/delete data")
            if flags & HIGH_RISK_FLAGS:
                reasons.append(f"Dangerous flags detected: {', '.join(flags & HIGH_RISK_FLAGS)}")
            return ShellRisk(
                risk_level="high",
                reasons=reasons,
                suggestion=self._suggest_safer(base_cmd, tokens),
            )

        # Recursive flags on any command
        if flags & {"-r", "--recursive"} and base_cmd not in ("grep", "find", "ls", "du", "tar", "rsync", "cp"):
            reasons.append("Recursive flag on non-read command")
            return ShellRisk(
                risk_level="high",
                reasons=reasons,
                suggestion="Avoid recursive operations when possible",
            )

        # Force flags
        if flags & {"-f", "--force"}:
            reasons.append("Force flag detected — bypasses safety checks")
            return ShellRisk(
                risk_level="medium",
                reasons=reasons,
                suggestion="Remove force flag unless absolutely necessary",
            )

        # Medium risk commands
        if base_cmd in SHELL_MEDIUM_RISK:
            return ShellRisk(
                risk_level="medium",
                reasons=[f"Base command '{base_cmd}' has side effects"],
                suggestion="Review arguments carefully",
            )

        # Pipe to shell
        if "| sh" in stripped or "| bash" in stripped or "| sudo sh" in stripped:
            return ShellRisk(
                risk_level="critical",
                reasons=["Piped input to shell — possible code injection"],
                suggestion="Avoid piping untrusted input to shell interpreters",
            )

        # Redirection to system paths
        redirect_match = re.search(r">\s*/(?:etc|var|usr|boot|sbin|bin)/", stripped)
        if redirect_match:
            return ShellRisk(
                risk_level="critical",
                reasons=["Redirecting output to system directory"],
                suggestion="Write to a user directory instead",
            )

        if reasons:
            return ShellRisk(risk_level="medium", reasons=reasons)

        return ShellRisk(risk_level="low", reasons=["command appears safe"])

    @staticmethod
    def _tokenize(command: str) -> list[str]:
        """Simple shell tokenizer — handles quoted strings."""
        tokens: list[str] = []
        current: list[str] = []
        in_single = False
        in_double = False
        escaped = False

        for ch in command:
            if escaped:
                current.append(ch)
                escaped = False
                continue
            if ch == "\\":
                escaped = True
                continue
            if ch == "'" and not in_double:
                in_single = not in_single
                continue
            if ch == '"' and not in_single:
                in_double = not in_double
                continue
            if ch in (" ", "\t") and not in_single and not in_double:
                if current:
                    tokens.append("".join(current))
                    current = []
                continue
            current.append(ch)

        if current:
            tokens.append("".join(current))
        return tokens

    @staticmethod
    def _suggest_safer(cmd: str, tokens: list[str]) -> str:
        suggestions = {
            "rm": "Use 'trash' or move to a backup directory instead of permanent deletion",
            "mv": "Create a backup before moving. Use 'cp' first to verify",
            "cp": "Use '-v' (verbose) flag to see what's being copied",
            "chmod": "Be specific about permissions. Avoid 777. Use 755 for dirs, 644 for files",
            "chown": "Verify the target user/group before changing ownership",
            "mkdir": "Use '-p' to avoid errors on existing dirs",
            "dd": "Double-check input/output devices. Consider using 'cp' instead",
            "kill": "Send SIGTERM (15) before SIGKILL (9). Use 'kill -l' to list signals",
            "killall": "Verify the process name matches your target exactly",
        }
        return suggestions.get(cmd, "Review command arguments carefully before executing")


# ---------------------------------------------------------------------------
# Structured Logger
# ---------------------------------------------------------------------------


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "service": getattr(record, "service", "hitl"),
            "event": getattr(record, "event", record.getMessage()),
        }
        details = getattr(record, "details", None)
        if details:
            entry["details"] = details
        return json.dumps(entry, default=str)


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("hitl")
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
    extra: dict[str, Any] = {"service": "hitl", "event": event}
    if details:
        extra["details"] = details
    getattr(log, level, log.info)(event, extra=extra)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def load_config() -> dict:
    """Load HITL config from YAML files, falling back to defaults."""
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
# Database — persistent approval queue & history
# ---------------------------------------------------------------------------


def _get_db() -> sqlite3.Connection:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=3000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS approvals (
            id              TEXT PRIMARY KEY,
            tool            TEXT NOT NULL,
            arguments       TEXT NOT NULL DEFAULT '{}',
            risk_level      TEXT NOT NULL DEFAULT 'low',
            risk_reasons    TEXT NOT NULL DEFAULT '[]',
            risk_suggestion TEXT NOT NULL DEFAULT '',
            agent           TEXT NOT NULL DEFAULT '',
            context         TEXT NOT NULL DEFAULT '{}',
            status          TEXT NOT NULL DEFAULT 'pending',
            reason          TEXT NOT NULL DEFAULT '',
            decided_by      TEXT NOT NULL DEFAULT '',
            decided_at      TEXT,
            created_at      TEXT NOT NULL,
            expires_at      TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_appr_status ON approvals(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_appr_tool ON approvals(tool)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_appr_created ON approvals(created_at)")
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Approval Request Model
# ---------------------------------------------------------------------------


@dataclass
class ApprovalRequest:
    id: str
    tool: str
    arguments: dict
    risk_level: str = "low"
    risk_reasons: list[str] = field(default_factory=list)
    risk_suggestion: str = ""
    agent: str = ""
    context: dict = field(default_factory=dict)
    status: str = "pending"
    reason: str = ""
    decided_by: str = ""
    decided_at: str = ""
    created_at: str = ""
    expires_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str)

    @classmethod
    def from_dict(cls, data: dict) -> "ApprovalRequest":
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ApprovalRequest":
        return cls(
            id=row["id"],
            tool=row["tool"],
            arguments=json.loads(row["arguments"]),
            risk_level=row["risk_level"],
            risk_reasons=json.loads(row["risk_reasons"]),
            risk_suggestion=row["risk_suggestion"],
            agent=row["agent"],
            context=json.loads(row["context"]),
            status=row["status"],
            reason=row["reason"],
            decided_by=row["decided_by"],
            decided_at=row["decided_at"] or "",
            created_at=row["created_at"],
            expires_at=row["expires_at"],
        )


# ---------------------------------------------------------------------------
# HITL Manager
# ---------------------------------------------------------------------------


class HITLManager:
    """Core HITL approval manager."""

    def __init__(self, config: dict):
        self._config = config.get("hitl", DEFAULT_CONFIG["hitl"])
        self._enabled = self._config.get("enabled", True)
        self._timeout = self._config.get("timeout", DEFAULT_TIMEOUT)
        self._tool_risk = dict(DEFAULT_TOOL_RISK)
        self._agent_overrides = self._config.get("agent_overrides", {})
        self._shell_analyzer = ShellRiskAnalyzer()
        self._waiters: dict[str, asyncio.Future] = {}
        self._nc = None
        self._running = True

        self._apply_config_overrides()

    def _apply_config_overrides(self):
        """Build tool risk map from config lists."""
        auto = self._config.get("auto_approve", [])
        always = self._config.get("always_approve", [])
        critical = self._config.get("critical", [])
        notification = self._config.get("notification", [])

        for tool in auto:
            self._tool_risk[tool] = ApprovalLevel.AUTO.value
        for tool in always:
            self._tool_risk[tool] = ApprovalLevel.APPROVE.value
        for tool in critical:
            self._tool_risk[tool] = ApprovalLevel.CRITICAL.value
        for tool in notification:
            self._tool_risk[tool] = ApprovalLevel.NOTIFICATION.value

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def timeout(self) -> int:
        return self._timeout

    # -- Approval Level Resolution -------------------------------------------

    def get_approval_level(self, tool_name: str, agent: str = "") -> ApprovalLevel:
        """Determine the approval level for a tool call, considering agent overrides."""
        if not self._enabled:
            return ApprovalLevel.AUTO

        # Check agent-specific overrides
        if agent and agent in self._agent_overrides:
            agent_cfg = self._agent_overrides[agent]
            if isinstance(agent_cfg, dict) and tool_name in agent_cfg:
                override_val = agent_cfg[tool_name]
                try:
                    return ApprovalLevel(override_val)
                except ValueError:
                    pass

        # Check tool risk map
        risk_str = self._tool_risk.get(tool_name, ApprovalLevel.APPROVE.value)
        try:
            return ApprovalLevel(risk_str)
        except ValueError:
            return ApprovalLevel.APPROVE

    def analyze_risk(self, tool_name: str, arguments: dict) -> ShellRisk:
        """Analyze risk for a tool call (deep analysis for shell commands)."""
        if tool_name == "shell":
            command = arguments.get("command", arguments.get("cmd", ""))
            return self._shell_analyzer.analyze(command)

        # For non-shell tools, return a basic risk based on level
        level = self.get_approval_level(tool_name)
        if level == ApprovalLevel.CRITICAL:
            return ShellRisk(
                risk_level="high",
                reasons=[f"Tool '{tool_name}' requires critical-level approval"],
            )
        elif level == ApprovalLevel.APPROVE:
            return ShellRisk(
                risk_level="medium",
                reasons=[f"Tool '{tool_name}' requires human approval"],
            )
        elif level == ApprovalLevel.NOTIFICATION:
            return ShellRisk(
                risk_level="low",
                reasons=[f"Tool '{tool_name}' will notify but not block"],
            )
        return ShellRisk(risk_level="low", reasons=["auto-approved tool"])

    # -- NATS Connection ----------------------------------------------------

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

    # -- Approval CRUD (SQLite) ----------------------------------------------

    def create_request(
        self,
        tool: str,
        arguments: dict,
        agent: str = "",
        context: dict | None = None,
    ) -> ApprovalRequest:
        """Create and persist a new approval request."""
        now = datetime.now(timezone.utc)
        risk = self.analyze_risk(tool, arguments)
        req_id = uuid.uuid4().hex[:16]
        expires = now.timestamp() + self._timeout

        request = ApprovalRequest(
            id=req_id,
            tool=tool,
            arguments=arguments,
            risk_level=risk.risk_level,
            risk_reasons=risk.reasons,
            risk_suggestion=risk.suggestion,
            agent=agent,
            context=context or {},
            status="pending",
            created_at=now.isoformat(),
            expires_at=datetime.fromtimestamp(expires, tz=timezone.utc).isoformat(),
        )

        conn = _get_db()
        conn.execute(
            "INSERT INTO approvals "
            "(id, tool, arguments, risk_level, risk_reasons, risk_suggestion, "
            "agent, context, status, reason, decided_by, decided_at, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                request.id,
                request.tool,
                json.dumps(request.arguments, default=str),
                request.risk_level,
                json.dumps(request.risk_reasons),
                request.risk_suggestion,
                request.agent,
                json.dumps(request.context, default=str),
                request.status,
                request.reason,
                request.decided_by,
                request.decided_at,
                request.created_at,
                request.expires_at,
            ),
        )
        conn.commit()
        _log("approval_created", details={
            "id": request.id, "tool": tool, "risk": risk.risk_level, "agent": agent,
        })
        return request

    def approve_request(self, req_id: str, decided_by: str = "user", reason: str = "") -> ApprovalRequest | None:
        """Approve a pending request."""
        conn = _get_db()
        row = conn.execute("SELECT * FROM approvals WHERE id = ?", (req_id,)).fetchone()
        if not row:
            return None

        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE approvals SET status = 'approved', reason = ?, decided_by = ?, decided_at = ? WHERE id = ?",
            (reason, decided_by, now, req_id),
        )
        conn.commit()

        request = ApprovalRequest.from_row(conn.execute("SELECT * FROM approvals WHERE id = ?", (req_id,)).fetchone())
        _log("approval_granted", details={"id": req_id, "tool": request.tool, "decided_by": decided_by})

        # Wake up any waiting coroutine
        future = self._waiters.pop(req_id, None)
        if future and not future.done():
            future.set_result(True)

        return request

    def deny_request(self, req_id: str, decided_by: str = "user", reason: str = "") -> ApprovalRequest | None:
        """Deny a pending request."""
        conn = _get_db()
        row = conn.execute("SELECT * FROM approvals WHERE id = ?", (req_id,)).fetchone()
        if not row:
            return None

        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE approvals SET status = 'denied', reason = ?, decided_by = ?, decided_at = ? WHERE id = ?",
            (reason, decided_by, now, req_id),
        )
        conn.commit()

        request = ApprovalRequest.from_row(conn.execute("SELECT * FROM approvals WHERE id = ?", (req_id,)).fetchone())
        _log("approval_denied", details={"id": req_id, "tool": request.tool, "decided_by": decided_by, "reason": reason})

        future = self._waiters.pop(req_id, None)
        if future and not future.done():
            future.set_result(False)

        return request

    def expire_request(self, req_id: str) -> ApprovalRequest | None:
        """Mark a request as expired."""
        conn = _get_db()
        row = conn.execute("SELECT * FROM approvals WHERE id = ?", (req_id,)).fetchone()
        if not row:
            return None

        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE approvals SET status = 'expired', decided_at = ? WHERE id = ?",
            (now, req_id),
        )
        conn.commit()

        request = ApprovalRequest.from_row(conn.execute("SELECT * FROM approvals WHERE id = ?", (req_id,)).fetchone())
        _log("approval_expired", details={"id": req_id, "tool": request.tool})

        future = self._waiters.pop(req_id, None)
        if future and not future.done():
            future.set_result(False)

        return request

    def get_request(self, req_id: str) -> ApprovalRequest | None:
        conn = _get_db()
        row = conn.execute("SELECT * FROM approvals WHERE id = ?", (req_id,)).fetchone()
        if not row:
            return None
        return ApprovalRequest.from_row(row)

    def get_pending(self) -> list[ApprovalRequest]:
        conn = _get_db()
        rows = conn.execute(
            "SELECT * FROM approvals WHERE status = 'pending' ORDER BY created_at DESC"
        ).fetchall()
        return [ApprovalRequest.from_row(r) for r in rows]

    def get_history(self, limit: int = 50, status_filter: str = "") -> list[ApprovalRequest]:
        conn = _get_db()
        if status_filter:
            rows = conn.execute(
                "SELECT * FROM approvals WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status_filter, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM approvals ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [ApprovalRequest.from_row(r) for r in rows]

    def cleanup_expired(self) -> int:
        """Expire all requests past their deadline."""
        now = datetime.now(timezone.utc).isoformat()
        conn = _get_db()
        rows = conn.execute(
            "SELECT id FROM approvals WHERE status = 'pending' AND expires_at <= ?",
            (now,),
        ).fetchall()

        count = 0
        for row in rows:
            self.expire_request(row["id"])
            count += 1

        return count

    def stats(self) -> dict:
        conn = _get_db()
        total = conn.execute("SELECT COUNT(*) as cnt FROM approvals").fetchone()["cnt"]
        pending = conn.execute("SELECT COUNT(*) as cnt FROM approvals WHERE status = 'pending'").fetchone()["cnt"]
        approved = conn.execute("SELECT COUNT(*) as cnt FROM approvals WHERE status = 'approved'").fetchone()["cnt"]
        denied = conn.execute("SELECT COUNT(*) as cnt FROM approvals WHERE status = 'denied'").fetchone()["cnt"]
        expired = conn.execute("SELECT COUNT(*) as cnt FROM approvals WHERE status = 'expired'").fetchone()["cnt"]
        return {
            "total": total,
            "pending": pending,
            "approved": approved,
            "denied": denied,
            "expired": expired,
        }

    # -- Waiting for Approval ------------------------------------------------

    async def wait_for_approval(self, req_id: str, timeout: float | None = None) -> bool:
        """Block until the request is approved, denied, or times out."""
        timeout = timeout or self._timeout
        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        self._waiters[req_id] = future

        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            self._waiters.pop(req_id, None)
            self.expire_request(req_id)
            await self._publish(SUBJECT_APPROVAL_EXPIRED, {"id": req_id})
            return False

    # -- Execute with Approval -----------------------------------------------

    async def execute_with_approval(
        self,
        tool_name: str,
        arguments: dict,
        agent: str = "",
        context: dict | None = None,
        tool_executor=None,
    ) -> dict:
        """Gate a tool execution behind the HITL approval flow.

        Args:
            tool_name: Name of the tool being invoked.
            arguments: Tool arguments.
            agent: Name of the requesting agent.
            context: Additional execution context.
            tool_executor: Async callable that actually runs the tool.
                           Signature: async def executor(name, args) -> dict

        Returns:
            Tool result dict or error dict.
        """
        level = self.get_approval_level(tool_name, agent)

        if level == ApprovalLevel.AUTO:
            if tool_executor:
                return await tool_executor(tool_name, arguments)
            return {"status": "auto_approved", "tool": tool_name}

        if level == ApprovalLevel.NOTIFICATION:
            result = {}
            if tool_executor:
                result = await tool_executor(tool_name, arguments)
            await self._publish(SUBJECT_APPROVAL_PENDING, {
                "tool": tool_name,
                "arguments": arguments,
                "agent": agent,
                "level": "notification",
                "result_preview": str(result)[:500],
            })
            _log("notification_sent", details={"tool": tool_name, "agent": agent})
            return result

        if level in (ApprovalLevel.APPROVE, ApprovalLevel.CRITICAL):
            request = self.create_request(tool_name, arguments, agent, context)

            # Publish pending event
            await self._publish(SUBJECT_APPROVAL_PENDING, request.to_dict())

            # Wait for human decision
            approved = await self.wait_for_approval(request.id)

            if approved:
                await self._publish(SUBJECT_APPROVAL_APPROVED, {"id": request.id, "tool": tool_name})
                if tool_executor:
                    return await tool_executor(tool_name, arguments)
                return {"status": "approved", "tool": tool_name, "id": request.id}
            else:
                await self._publish(SUBJECT_APPROVAL_DENIED, {"id": request.id, "tool": tool_name})
                req = self.get_request(request.id)
                reason = req.reason if req else "denied"
                return {
                    "error": True,
                    "status": "denied",
                    "tool": tool_name,
                    "id": request.id,
                    "message": f"Action denied by user: {reason}",
                }

        return {"error": True, "message": f"Unknown approval level: {level}"}

    # -- Background Expiry Loop ----------------------------------------------

    async def expiry_loop(self):
        """Periodically expire pending requests that have passed their deadline."""
        while self._running:
            try:
                count = self.cleanup_expired()
                if count > 0:
                    _log("expired_requests", details={"count": count})
            except Exception as exc:
                _log("expiry_loop_error", level="warning", details={"error": str(exc)})
            await asyncio.sleep(30)


# ---------------------------------------------------------------------------
# HTTP API Handlers
# ---------------------------------------------------------------------------


def _json_response(data: dict, status: int = 200) -> web.Response:
    return web.json_response(data, status=status)


async def handle_list_pending(request: web.Request) -> web.Response:
    manager: HITLManager = request.app["hitl_manager"]
    pending = manager.get_pending()
    return _json_response({
        "status": "ok",
        "count": len(pending),
        "requests": [r.to_dict() for r in pending],
    })


async def handle_approve(request: web.Request) -> web.Response:
    manager: HITLManager = request.app["hitl_manager"]
    req_id = request.match_info.get("id", "")
    if not req_id:
        return _json_response({"error": "missing request id"}, 400)

    try:
        body = await request.json()
    except Exception:
        body = {}

    reason = body.get("reason", "")
    decided_by = body.get("decided_by", "dashboard")

    result = manager.approve_request(req_id, decided_by=decided_by, reason=reason)
    if not result:
        return _json_response({"error": f"request {req_id} not found"}, 404)

    return _json_response({"status": "ok", "request": result.to_dict()})


async def handle_deny(request: web.Request) -> web.Response:
    manager: HITLManager = request.app["hitl_manager"]
    req_id = request.match_info.get("id", "")
    if not req_id:
        return _json_response({"error": "missing request id"}, 400)

    try:
        body = await request.json()
    except Exception:
        body = {}

    reason = body.get("reason", "")
    decided_by = body.get("decided_by", "dashboard")

    result = manager.deny_request(req_id, decided_by=decided_by, reason=reason)
    if not result:
        return _json_response({"error": f"request {req_id} not found"}, 404)

    return _json_response({"status": "ok", "request": result.to_dict()})


async def handle_get_request(request: web.Request) -> web.Response:
    manager: HITLManager = request.app["hitl_manager"]
    req_id = request.match_info.get("id", "")
    if not req_id:
        return _json_response({"error": "missing request id"}, 400)

    result = manager.get_request(req_id)
    if not result:
        return _json_response({"error": f"request {req_id} not found"}, 404)

    return _json_response({"status": "ok", "request": result.to_dict()})


async def handle_history(request: web.Request) -> web.Response:
    manager: HITLManager = request.app["hitl_manager"]
    limit = int(request.query.get("limit", "50"))
    status_filter = request.query.get("status", "")
    history = manager.get_history(limit=limit, status_filter=status_filter)
    return _json_response({
        "status": "ok",
        "count": len(history),
        "requests": [r.to_dict() for r in history],
    })


async def handle_stats(request: web.Request) -> web.Response:
    manager: HITLManager = request.app["hitl_manager"]
    stats = manager.stats()
    return _json_response({"status": "ok", "stats": stats})


async def handle_analyze(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "invalid JSON"}, 400)

    command = body.get("command", "")
    if not command:
        return _json_response({"error": "missing 'command' field"}, 400)

    analyzer = ShellRiskAnalyzer()
    risk = analyzer.analyze(command)
    return _json_response({
        "status": "ok",
        "command": command,
        "risk": {
            "level": risk.risk_level,
            "reasons": risk.reasons,
            "suggestion": risk.suggestion,
        },
    })


async def handle_config(request: web.Request) -> web.Response:
    manager: HITLManager = request.app["hitl_manager"]
    return _json_response({
        "status": "ok",
        "enabled": manager.enabled,
        "timeout": manager.timeout,
        "tool_risk": manager._tool_risk,
        "agent_overrides": manager._agent_overrides,
    })


async def handle_health(request: web.Request) -> web.Response:
    manager: HITLManager = request.app["hitl_manager"]
    return _json_response({
        "status": "healthy",
        "service": "hitl",
        "enabled": manager.enabled,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "uptime": time.time() - request.app.get("start_time", time.time()),
    })


# ---------------------------------------------------------------------------
# HTTP Server
# ---------------------------------------------------------------------------


def build_app(manager: HITLManager) -> web.Application:
    app = web.Application()
    app["hitl_manager"] = manager
    app["start_time"] = time.time()

    app.router.add_get("/api/approvals/pending", handle_list_pending)
    app.router.add_post("/api/approvals/{id}/approve", handle_approve)
    app.router.add_post("/api/approvals/{id}/deny", handle_deny)
    app.router.add_get("/api/approvals/{id}", handle_get_request)
    app.router.add_get("/api/approvals/history", handle_history)
    app.router.add_get("/api/approvals/stats", handle_stats)
    app.router.add_post("/api/analyze", handle_analyze)
    app.router.add_get("/api/config", handle_config)
    app.router.add_get("/health", handle_health)

    return app


async def start_background_tasks(app: web.Application):
    manager: HITLManager = app["hitl_manager"]
    await manager.connect_nats()
    app["expiry_task"] = asyncio.create_task(manager.expiry_loop())


async def cleanup_background_tasks(app: web.Application):
    manager: HITLManager = app["hitl_manager"]
    manager._running = False
    task = app.get("expiry_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    await manager.close_nats()


# ---------------------------------------------------------------------------
# NATS Subscriber — listen for approval decisions from external sources
# ---------------------------------------------------------------------------


async def setup_nats_subscribers(manager: HITLManager, nc):
    """Subscribe to NATS subjects for approval decisions via message bus."""
    async def on_approve(msg):
        try:
            data = json.loads(msg.data.decode())
            req_id = data.get("id", "")
            if req_id:
                manager.approve_request(req_id, decided_by=data.get("decided_by", "nats"), reason=data.get("reason", ""))
        except Exception as exc:
            _log("nats_approve_error", level="warning", details={"error": str(exc)})

    async def on_deny(msg):
        try:
            data = json.loads(msg.data.decode())
            req_id = data.get("id", "")
            if req_id:
                manager.deny_request(req_id, decided_by=data.get("decided_by", "nats"), reason=data.get("reason", ""))
        except Exception as exc:
            _log("nats_deny_error", level="warning", details={"error": str(exc)})

    await nc.subscribe("agnetic.approval.command.approve", cb=on_approve)
    await nc.subscribe("agnetic.approval.command.deny", cb=on_deny)
    _log("nats_subscribers_setup")


# ---------------------------------------------------------------------------
# CLI Commands
# ---------------------------------------------------------------------------


def cmd_serve():
    if web is None:
        print("aiohttp is required: pip install aiohttp", file=sys.stderr)
        sys.exit(1)

    config = load_config()
    manager = HITLManager(config)
    host = config.get("hitl", {}).get("server", {}).get("host", "0.0.0.0")
    port = config.get("hitl", {}).get("server", {}).get("port", 8910)

    _log("server_starting", details={"host": host, "port": port})

    app = build_app(manager)
    app.on_startup.append(start_background_tasks)
    app.on_cleanup.append(cleanup_background_tasks)

    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))

    def _shutdown(signum, _frame):
        _log("signal_received", details={"signal": signum})
        manager._running = False

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        web.run_app(app, host=host, port=port, print=None)
    finally:
        try:
            PID_FILE.unlink()
        except OSError:
            pass


def cmd_pending():
    conn = _get_db()
    rows = conn.execute(
        "SELECT * FROM approvals WHERE status = 'pending' ORDER BY created_at DESC"
    ).fetchall()

    if not rows:
        print("No pending approvals.")
        return

    print(f"\n{'ID':<18} {'Tool':<20} {'Risk':<10} {'Agent':<12} {'Created':<22}")
    print("-" * 82)
    for r in rows:
        print(
            f"{r['id']:<18} {r['tool']:<20} {r['risk_level']:<10} "
            f"{r['agent'] or '-':<12} {r['created_at']:<22}"
        )
    print(f"\n{len(rows)} pending request(s)\n")


def cmd_approve(args):
    if not args:
        print("usage: hitl.py approve <id> [reason]")
        sys.exit(1)

    config = load_config()
    manager = HITLManager(config)
    req_id = args[0]
    reason = " ".join(args[1:]) if len(args) > 1 else ""

    result = manager.approve_request(req_id, decided_by="cli", reason=reason)
    if result:
        print(f"Approved request {req_id} ({result.tool})")
    else:
        print(f"Request {req_id} not found", file=sys.stderr)
        sys.exit(1)


def cmd_deny(args):
    if not args:
        print("usage: hitl.py deny <id> [reason]")
        sys.exit(1)

    config = load_config()
    manager = HITLManager(config)
    req_id = args[0]
    reason = " ".join(args[1:]) if len(args) > 1 else ""

    result = manager.deny_request(req_id, decided_by="cli", reason=reason)
    if result:
        print(f"Denied request {req_id} ({result.tool})")
    else:
        print(f"Request {req_id} not found", file=sys.stderr)
        sys.exit(1)


def cmd_history():
    config = load_config()
    manager = HITLManager(config)
    history = manager.get_history(limit=30)

    if not history:
        print("No approval history.")
        return

    print(f"\n{'ID':<18} {'Tool':<20} {'Status':<10} {'Risk':<10} {'Decided By':<12} {'Created':<22}")
    print("-" * 92)
    for r in history:
        print(
            f"{r.id:<18} {r.tool:<20} {r.status:<10} {r.risk_level:<10} "
            f"{r.decided_by or '-':<12} {r.created_at:<22}"
        )

    stats = manager.stats()
    print(f"\nTotal: {stats['total']}  |  Pending: {stats['pending']}  |  "
          f"Approved: {stats['approved']}  |  Denied: {stats['denied']}  |  "
          f"Expired: {stats['expired']}\n")


def cmd_analyze(args):
    if not args:
        print("usage: hitl.py analyze <command>")
        sys.exit(1)

    command = " ".join(args)
    analyzer = ShellRiskAnalyzer()
    risk = analyzer.analyze(command)

    print(f"\nCommand:   {command}")
    print(f"Risk:      {risk.risk_level}")
    print(f"Reasons:   {'; '.join(risk.reasons)}")
    if risk.suggestion:
        print(f"Suggestion: {risk.suggestion}")
    print()


def cmd_config():
    config = load_config()
    hitl_cfg = config.get("hitl", DEFAULT_CONFIG["hitl"])

    print(f"\nHITL Configuration")
    print(f"  Enabled:  {hitl_cfg.get('enabled', True)}")
    print(f"  Timeout:  {hitl_cfg.get('timeout', DEFAULT_TIMEOUT)}s")
    print(f"  Server:   {hitl_cfg.get('server', {}).get('host', '0.0.0.0')}:{hitl_cfg.get('server', {}).get('port', 8910)}")
    print(f"\n  Auto-approve:    {', '.join(hitl_cfg.get('auto_approve', []))}")
    print(f"  Always-approve:  {', '.join(hitl_cfg.get('always_approve', []))}")
    print(f"  Critical:        {', '.join(hitl_cfg.get('critical', []))}")
    print(f"  Notification:    {', '.join(hitl_cfg.get('notification', []))}")

    overrides = hitl_cfg.get("agent_overrides", {})
    if overrides:
        print(f"\n  Agent Overrides:")
        for agent, tools in overrides.items():
            for tool, level in tools.items():
                print(f"    {agent}/{tool}: {level}")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    if len(sys.argv) < 2:
        print("usage: hitl.py <command> [args]")
        print("commands: serve, pending, approve, deny, history, analyze, config")
        sys.exit(1)

    cmd = sys.argv[1]
    rest = sys.argv[2:]

    cmds = {
        "serve": lambda: cmd_serve(),
        "pending": lambda: cmd_pending(),
        "approve": lambda: cmd_approve(rest),
        "deny": lambda: cmd_deny(rest),
        "history": lambda: cmd_history(),
        "analyze": lambda: cmd_analyze(rest),
        "config": lambda: cmd_config(),
    }

    fn = cmds.get(cmd)
    if fn is None:
        print(f"unknown command: {cmd}", file=sys.stderr)
        print("commands: serve, pending, approve, deny, history, analyze, config")
        sys.exit(1)

    fn()


if __name__ == "__main__":
    main()
