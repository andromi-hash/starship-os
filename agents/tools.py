#!/usr/bin/env python3
"""
Starship OS Tool System — sandboxed execution for agents.

Borrowed patterns:
- Hermes Agent: TOOLSETS compositing, tool call auto-repair, callback-driven streaming
- Flamingo Stack: CommandExecutor interface, typed errors, dry-run + redaction
"""

import os
import json
import asyncio
import logging
import subprocess
import shutil
import shlex
import signal
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

log = logging.getLogger("agnetic-tools")

MEMORY_DIR = Path(os.path.dirname(os.path.abspath(__file__))).parent / "memory"

try:
    from services.memory import MemoryManager, MemoryType as SQLiteMemoryType
    _memory_mgr = MemoryManager()
except ImportError:
    _memory_mgr = None
    SQLiteMemoryType = None


# ─── Typed Errors (Flamingo pattern) ────────────────────────────────
class ToolError(Exception):
    """Base tool error with code."""
    def __init__(self, message: str, code: str = "TOOL_ERROR", details: dict = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}

    def to_dict(self):
        return {"error": True, "code": self.code, "message": str(self), "details": self.details}


class SandboxError(ToolError):
    def __init__(self, message, command=""):
        super().__init__(message, code="SANDBOX_DENIED", details={"command": command})


class TimeoutError(ToolError):
    def __init__(self, command, timeout):
        super().__init__(f"Command timed out after {timeout}s", code="TIMEOUT", details={"command": command, "timeout": timeout})


class AccessDeniedError(ToolError):
    def __init__(self, path, operation="read"):
        super().__init__(f"Access denied: {path} ({operation})", code="ACCESS_DENIED", details={"path": path, "operation": operation})


# ─── Sandbox Configuration ──────────────────────────────────────────
BLOCKED_COMMANDS = [
    "rm -rf /", "mkfs", "dd if=", "> /dev/", ":(){ :|:&", "shutdown",
    "reboot", "halt", "poweroff", "init 0", "init 6",
]

PRIVILEGED_COMMANDS = ["sudo", "su ", "chmod 777", "chown", "passwd", "useradd", "userdel"]

ALLOWED_READ_PATHS = ["/home", "/tmp", "/opt/agnetic", "/etc/agnetic", "/var/log/agnetic"]
ALLOWED_WRITE_PATHS = ["/tmp", "/opt/agnetic", "/var/log/agnetic"]

MAX_OUTPUT_SIZE = 50000
MAX_FILE_SIZE = 1048576
DEFAULT_TIMEOUT = 30


# ─── Redaction (Flamingo pattern) ───────────────────────────────────
REDACT_PATTERNS = [
    (r'(?i)(password|token|secret|key)\s*[=:]\s*\S+', r'\1=***REDACTED***'),
    (r'ghp_[a-zA-Z0-9]+', 'ghp_***REDACTED***'),
    (r'sk-[a-zA-Z0-9]+', 'sk-***REDACTED***'),
]


def redact(text: str) -> str:
    """Redact secrets from output."""
    import re
    for pattern, replacement in REDACT_PATTERNS:
        text = re.sub(pattern, replacement, text)
    return text


# ─── CommandExecutor (Flamingo pattern) ─────────────────────────────
@dataclass
class ExecuteResult:
    """Result of a command execution."""
    exit_code: int
    stdout: str
    stderr: str = ""
    timed_out: bool = False
    command: str = ""

    @property
    def success(self):
        return self.exit_code == 0

    def to_dict(self):
        return {
            "output": self.stdout,
            "error_output": self.stderr,
            "exit_code": self.exit_code,
            "error": not self.success,
            "timed_out": self.timed_out,
        }


class CommandExecutor:
    """Sandboxed command executor with timeout and dry-run support.

    Borrowed from Flamingo Stack's CommandExecutor pattern.
    """

    def __init__(self, dry_run=False, sandbox=True, timeout=DEFAULT_TIMEOUT):
        self.dry_run = dry_run
        self.sandbox = sandbox
        self.default_timeout = timeout

    def _validate(self, command: str):
        """Validate command against sandbox rules."""
        if not self.sandbox:
            return

        cmd_lower = command.lower().strip()
        for blocked in BLOCKED_COMMANDS:
            if blocked in cmd_lower:
                raise SandboxError(f"Blocked: '{blocked}'", command)

        for priv in PRIVILEGED_COMMANDS:
            if priv in cmd_lower:
                raise SandboxError(f"Blocked: privileged command '{priv}'", command)

    async def execute(self, command: str, timeout: int = None, env: dict = None) -> ExecuteResult:
        """Execute a shell command with sandboxing."""
        timeout = timeout or self.default_timeout
        self._validate(command)

        if self.dry_run:
            log.info("[DRY RUN] Would execute: %s", redact(command))
            return ExecuteResult(exit_code=0, stdout=f"[DRY RUN] {command}", command=command)

        # Optional C11 policyexec (shared policy JSON) — STARSHIP_POLICY_NATIVE=1
        try:
            from policy_native import native_enabled as policy_native_on, check_command as policy_check_cmd
            if policy_native_on():
                denial = await asyncio.to_thread(policy_check_cmd, command)
                if denial:
                    raise SandboxError(denial, command)
        except SandboxError:
            raise
        except ImportError:
            pass
        except Exception as e:
            log.debug("native policy fallback: %s", e)

        # Optional C11 sandbox_run (ADR 0001) — STARSHIP_SANDBOX_NATIVE=1
        try:
            from sandbox_native import native_enabled, run_shell_native
            if native_enabled():
                nr = await asyncio.to_thread(run_shell_native, command, timeout=timeout)
                if nr.denied:
                    raise SandboxError(nr.stderr or "native sandbox denied", command)
                return ExecuteResult(
                    exit_code=nr.exit_code,
                    stdout=(nr.stdout or "")[:MAX_OUTPUT_SIZE],
                    stderr=(nr.stderr or "")[:MAX_OUTPUT_SIZE],
                    timed_out=nr.timed_out,
                    command=command,
                )
        except SandboxError:
            raise
        except ImportError:
            pass
        except Exception as e:
            log.debug("native sandbox fallback: %s", e)

        try:
            merged_env = {**os.environ, "TERM": "dumb"}
            if env:
                merged_env.update(env)

            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=merged_env,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return ExecuteResult(
                exit_code=proc.returncode,
                stdout=stdout.decode(errors="replace")[:MAX_OUTPUT_SIZE],
                stderr=stderr.decode(errors="replace")[:MAX_OUTPUT_SIZE],
                command=command,
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            return ExecuteResult(exit_code=-1, stdout="", stderr=f"Timeout after {timeout}s", timed_out=True, command=command)
        except Exception as e:
            return ExecuteResult(exit_code=-1, stdout="", stderr=str(e), command=command)


# ─── Tool Compositing (Hermes pattern) ──────────────────────────────
TOOLSETS = {
    "core": {
        "description": "Basic filesystem and shell operations",
        "tools": ["shell", "read_file", "write_file", "list_dir", "search_files"],
    },
    "network": {
        "description": "HTTP requests and API calls",
        "tools": ["http_get", "http_post"],
    },
    "delegation": {
        "description": "Multi-agent task delegation and parallel subagent spawning",
        "tools": ["delegate_to_agent", "delegate"],
    },
    "coding": {
        "description": "Code generation and OS expansion via OpenCode",
        "tools": ["opencode"],
    },
    "design": {
        "description": "Design artifact generation via Open Design",
        "tools": ["opendesign"],
    },
    "expansion": {
        "description": "Full OS expansion: coding + design",
        "tools": ["opencode", "opendesign"],
    },
    "memory": {
        "description": "Persistent memory — working notes, user profile, session archive, temporal graph, knowledge graph",
        "tools": ["memory_note", "user_profile", "archive_search", "temporal_graph", "temporal_chain", "temporal_snapshot", "kg_query", "kg_store", "preference_note", "preference_query"],
    },
    "scheduling": {
        "description": "Create and manage scheduled tasks (cron jobs)",
        "tools": ["create_schedule", "list_schedules", "remove_schedule"],
    },
    "vault": {
        "description": "Obsidian HITL vault — human-in-the-loop approval notes as markdown",
        "tools": ["vault_sync", "vault_list", "vault_note", "vault_approve", "vault_deny", "vault_stats"],
    },
    "goals": {
        "description": "Goals → Missions → Tasks — strategic planning hierarchy",
        "tools": ["goal_create", "goal_list", "goal_update", "mission_create", "mission_list", "task_create", "task_list", "task_complete"],
    },
    "full": {
        "description": "All available tools",
        "includes": ["core", "network", "delegation", "coding", "design", "memory", "scheduling", "vault", "goals"],
    },
    "readonly": {
        "description": "Read-only operations (no writes, no shell)",
        "tools": ["read_file", "list_dir", "search_files", "http_get"],
    },
    "webhook_safe": {
        "description": "Safe tools for untrusted input (no shell, no writes)",
        "tools": ["read_file", "list_dir", "search_files", "http_get"],
    },
    "red_team": {
        "description": "Red-team exercise tools (no OpenCode, no shell/write)",
        "tools": ["read_file", "list_dir", "search_files", "http_get", "delegate_to_agent"],
    },
    "blue_team": {
        "description": "Blue-team defensive tools (diagnostics, no OpenCode on range)",
        "includes": ["core", "network", "delegation"],
    },
    "security_audit": {
        "description": "Security audit toolset for red-team constrained use",
        "tools": ["read_file", "list_dir", "search_files", "http_get"],
    },
}


def resolve_toolset(name: str) -> list:
    """Resolve a toolset name to a flat list of tool names."""
    if name not in TOOLSETS:
        return []

    ts = TOOLSETS[name]
    tools = list(ts.get("tools", []))

    for include in ts.get("includes", []):
        tools.extend(resolve_toolset(include))

    return list(set(tools))


def get_tool_definitions(toolset: str = "full") -> list:
    """Get Ollama-compatible tool definitions for a toolset."""
    allowed = set(resolve_toolset(toolset))
    try:
        from fleet_policy import filter_toolset, current_context
        ctx = current_context()
        # Auto-select red/blue toolsets from fleet context
        if (ctx.get("team") or "").lower() == "red" or "red-team" in [r.lower() for r in ctx.get("roles", [])]:
            allowed = set(resolve_toolset("red_team"))
        elif (ctx.get("team") or "").lower() == "blue" or "blue-team" in [r.lower() for r in ctx.get("roles", [])]:
            if toolset == "full":
                allowed = set(resolve_toolset("blue_team"))
        allowed = set(filter_toolset(list(allowed), ctx))
    except ImportError:
        pass
    return [t for t in TOOL_DEFINITIONS if t["function"]["name"] in allowed]


# ─── Tool Call Auto-Repair (Hermes pattern) ─────────────────────────
def repair_tool_arguments(args: Any, tool_name: str) -> dict:
    """Attempt to repair malformed tool call arguments.

    Borrowed from Hermes Agent's _repair_tool_call_arguments().
    Models sometimes return corrupted JSON for tool arguments.
    """
    if isinstance(args, dict):
        return args

    if isinstance(args, str):
        # Try parsing as JSON
        try:
            parsed = json.loads(args)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

        # Try fixing common issues
        fixed = args.strip()
        if fixed.startswith("'") and fixed.endswith("'"):
            fixed = fixed[1:-1]
        if not fixed.startswith("{"):
            fixed = "{" + fixed
        if not fixed.endswith("}"):
            fixed = fixed + "}"

        try:
            parsed = json.loads(fixed)
            if isinstance(parsed, dict):
                log.warning("Repaired malformed arguments for %s", tool_name)
                return parsed
        except json.JSONDecodeError:
            pass

        # Last resort: wrap as {"command": args} for shell-like tools
        if tool_name == "shell":
            return {"command": args}
        if tool_name in ("read_file", "list_dir"):
            return {"path": args}

    log.warning("Could not repair arguments for %s: %s", tool_name, str(args)[:100])
    return {}


# ─── OpenCode Integration ──────────────────────────────────────────
OPENCODE_BINARY = shutil.which("opencode") or "/tmp/opencode/bin/opencode"
OPENCODE_TIMEOUT = 120


def _tool_opencode_definition():
    return {
        "type": "function",
        "function": {
            "name": "opencode",
            "description": "Invoke OpenCode AI coding agent for code generation, refactoring, debugging, or building new features. Agents can use this to expand and modify the OS from inside.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "The coding task or instruction for OpenCode"},
                    "model": {"type": "string", "description": "Model in provider/model format (e.g. anthropic/claude-sonnet-4-20250514)"},
                    "files": {"type": "array", "items": {"type": "string"}, "description": "File paths to attach as context"},
                    "session": {"type": "string", "description": "Session ID to continue a previous conversation"},
                    "continue_last": {"type": "boolean", "description": "Continue the last session"},
                    "format": {"type": "string", "enum": ["default", "json"], "description": "Output format (default: json)"},
                },
                "required": ["prompt"],
            },
        },
    }


async def _tool_opencode(args: dict) -> dict:
    """Invoke OpenCode for code generation and OS expansion."""
    prompt = args.get("prompt", "")
    model = args.get("model", "")
    files = args.get("files", [])
    session = args.get("session", "")
    continue_last = args.get("continue_last", False)
    fmt = args.get("format", "json")

    if not prompt:
        return {"error": True, "message": "prompt is required"}

    if not Path(OPENCODE_BINARY).exists():
        return {"error": True, "message": f"OpenCode not found at {OPENCODE_BINARY}. Install: curl -fsSL https://opencode.ai/install | bash"}

    cmd_parts = [OPENCODE_BINARY, "run"]

    if model:
        cmd_parts.extend(["-m", model])
    if fmt:
        cmd_parts.extend(["--format", fmt])
    if continue_last:
        cmd_parts.append("-c")
    if session:
        cmd_parts.extend(["-s", session])
    for f in files:
        cmd_parts.extend(["-f", f])

    cmd_parts.append(prompt)
    command = " ".join(shlex.quote(p) for p in cmd_parts)

    result = await _executor.execute(command, timeout=OPENCODE_TIMEOUT)
    return result.to_dict()


# ─── Open Design Integration ───────────────────────────────────────
OPENDESIGN_DIR = Path(os.environ.get("OPENDESIGN_DIR", "/opt/open-design"))
OPENDESIGN_DAEMON_PORT = 7456
OPENDESIGN_TIMEOUT = 180


def _tool_opendesign_definition():
    return {
        "type": "function",
        "function": {
            "name": "opendesign",
            "description": "Generate design artifacts using Open Design — web prototypes, slide decks, mobile mockups, dashboards. Uses composable skills and brand-grade design systems.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Description of what to design (e.g. 'landing page for agnetic os')"},
                    "skill": {"type": "string", "description": "Open Design skill to use (e.g. 'web-prototype', 'slide-deck', 'dashboard', 'mobile-app')"},
                    "design_system": {"type": "string", "description": "Design system name (e.g. 'linear', 'stripe', 'vercel', 'notion')"},
                    "output_dir": {"type": "string", "description": "Directory to save artifacts (default: /tmp/agnetic-design)"},
                    "agent": {"type": "string", "description": "Coding agent to use (e.g. opencode, hermes, claude)"},
                },
                "required": ["prompt"],
            },
        },
    }


async def _tool_opendesign(args: dict) -> dict:
    """Generate design artifacts using Open Design."""
    prompt = args.get("prompt", "")
    skill = args.get("skill", "web-prototype")
    design_system = args.get("design_system", "linear")
    output_dir = args.get("output_dir", "/tmp/agnetic-design")
    agent = args.get("agent", "opencode")

    if not prompt:
        return {"error": True, "message": "prompt is required"}

    if not OPENDESIGN_DIR.exists():
        return {"error": True, "message": f"Open Design not found at {OPENDESIGN_DIR}. Install: git clone https://github.com/nexu-io/open-design.git /opt/open-design"}

    Path(output_dir).mkdir(parents=True, exist_ok=True)

    # Check if Open Design daemon is running
    daemon_running = await _check_port(OPENDESIGN_DAEMON_PORT)

    if daemon_running:
        # Use daemon API
        import httpx
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(f"http://localhost:{OPENDESIGN_DAEMON_PORT}/api/generate", json={
                    "prompt": prompt,
                    "skill": skill,
                    "design_system": design_system,
                    "agent": agent,
                    "output_dir": output_dir,
                })
                return {"status_code": resp.status_code, "output": resp.text, "error": resp.status_code >= 400}
        except Exception as e:
            return {"error": True, "message": f"Daemon API error: {e}"}
    else:
        # Use CLI fallback
        cmd = f"cd {OPENDESIGN_DIR} && {agent} run \"Design a {skill} for: {prompt} using {design_system} design system. Output to {output_dir}\""
        result = await _executor.execute(cmd, timeout=OPENDESIGN_TIMEOUT)
        return result.to_dict()


async def _check_port(port: int) -> bool:
    """Check if a port is listening."""
    import socket
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        result = sock.connect_ex(("127.0.0.1", port))
        sock.close()
        return result == 0
    except Exception:
        return False


# ─── Memory tool implementations ────────────────────────────────────

def _agent_memory_path(agent_name: str, kind: str = "MEMORY.md") -> Path:
    return MEMORY_DIR / agent_name / kind


def _load_memory_file(agent_name: str, kind: str = "MEMORY.md") -> str:
    p = _agent_memory_path(agent_name, kind)
    if p.exists():
        return p.read_text().strip()
    return ""


def _save_memory_file(agent_name: str, content: str, kind: str = "MEMORY.md") -> bool:
    p = _agent_memory_path(agent_name, kind)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return True


def _tool_memory_note(args: dict) -> dict:
    action = args.get("action", "add")
    content = args.get("content", "")
    topic = args.get("topic", "")
    agent = args.get("_agent", "default")

    md = _load_memory_file(agent)
    lines = md.split("\n") if md else []

    if action == "add":
        if topic:
            lines.append(f"\n## {topic}\n{content}")
        else:
            lines.append(content)
        _save_memory_file(agent, "\n".join(lines).strip())
        return {"status": "appended", "topic": topic or "general", "agent": agent}

    elif action == "replace":
        if not topic:
            return {"error": "topic required for replace"}
        new_lines = []
        in_section = False
        section_found = False
        for line in lines:
            if line.startswith(f"## {topic}"):
                in_section = True
                section_found = True
                new_lines.append(f"## {topic}")
                new_lines.append(content)
                continue
            if in_section:
                if line.startswith("## "):
                    in_section = False
                    new_lines.append(line)
                continue
            new_lines.append(line)
        if not section_found:
            new_lines.append(f"\n## {topic}\n{content}")
        _save_memory_file(agent, "\n".join(new_lines).strip())
        return {"status": "replaced", "topic": topic, "agent": agent}

    elif action == "remove":
        if not topic:
            return {"error": "topic required for remove"}
        new_lines = []
        in_section = False
        for line in lines:
            if line.startswith(f"## {topic}"):
                in_section = True
                continue
            if in_section:
                if line.startswith("## "):
                    in_section = False
                    new_lines.append(line)
                continue
            new_lines.append(line)
        _save_memory_file(agent, "\n".join(new_lines).strip())
        return {"status": "removed", "topic": topic, "agent": agent}

    return {"error": f"unknown action: {action}"}


def _tool_user_profile(args: dict) -> dict:
    action = args.get("action", "add")
    content = args.get("content", "")
    topic = args.get("topic", "")
    agent = args.get("_agent", "default")

    md = _load_memory_file(agent, "USER.md")
    lines = md.split("\n") if md else []

    if action == "add":
        if topic:
            lines.append(f"\n## {topic}\n{content}")
        else:
            lines.append(f"- {content}")
        _save_memory_file(agent, "\n".join(lines).strip(), "USER.md")
        return {"status": "added", "topic": topic or "general", "agent": agent}

    elif action == "replace":
        if not topic:
            return {"error": "topic required for replace"}
        new_lines = []
        in_section = False
        section_found = False
        for line in lines:
            if line.startswith(f"## {topic}"):
                in_section = True
                section_found = True
                new_lines.append(f"## {topic}")
                new_lines.append(content)
                continue
            if in_section:
                if line.startswith("## "):
                    in_section = False
                    new_lines.append(line)
                continue
            new_lines.append(line)
        if not section_found:
            new_lines.append(f"\n## {topic}\n{content}")
        _save_memory_file(agent, "\n".join(new_lines).strip(), "USER.md")
        return {"status": "replaced", "topic": topic, "agent": agent}

    return {"error": f"unknown action: {action}"}


async def _tool_archive_search(args: dict) -> dict:
    query = args.get("query", "")
    agent_filter = args.get("agent")
    limit = int(args.get("limit", 10))

    try:
        from services.archive import ArchiveService
        arch = ArchiveService()
        results = arch.search(query, agent=agent_filter, limit=limit)
        arch.close()
        return {"query": query, "count": len(results), "results": results}
    except Exception:
        pass

    results = []
    for d in [Path("/tmp/agnetic-history"), Path("/tmp/starship-history")]:
        if not d.exists():
            continue
        for f in sorted(d.glob("*.jsonl"), reverse=True)[:5]:
            try:
                for line in f.read_text(errors="replace").split("\n"):
                    if not line.strip():
                        continue
                    try:
                        entry = json.loads(line)
                        if query.lower() in json.dumps(entry).lower():
                            if agent_filter and agent_filter not in str(entry.get("subject", "")):
                                continue
                            results.append({
                                "file": f.name, "timestamp": entry.get("timestamp", ""),
                                "agent": entry.get("subject", "").split(".")[-1],
                                "command": entry.get("command", ""),
                                "summary": (entry.get("response") or entry.get("content", ""))[:200],
                            })
                            if len(results) >= limit:
                                break
                    except json.JSONDecodeError:
                        continue
                if len(results) >= limit:
                    break
            except Exception:
                continue
        if len(results) >= limit:
            break
    return {"query": query, "count": len(results), "results": results}


async def _tool_temporal_graph(args: dict) -> dict:
    entity_id = args.get("entity_id", "")
    since = args.get("since", "")
    depth = int(args.get("depth", 1))
    if not entity_id:
        return {"error": "entity_id is required", "transitions": []}
    try:
        from services.audit import get_logger
        logger = get_logger()
        entries = logger.query(agent=entity_id, since=since, limit=100)
        transitions = []
        seen = set()
        for e in entries:
            if e.id in seen:
                continue
            seen.add(e.id)
            transitions.append({
                "id": e.id,
                "timestamp": e.timestamp,
                "action": e.action,
                "tool": e.tool,
                "before_state": e.before_state,
                "after_state": e.after_state,
                "result_summary": e.result_summary[:200] if e.result_summary else "",
                "risk_level": e.risk_level,
                "session_id": e.session_id,
            })
            if depth > 1 and e.parent_action_id:
                parent_entries = logger.query(agent=entity_id, limit=50)
                for p in parent_entries:
                    if p.id == e.parent_action_id and p.id not in seen:
                        seen.add(p.id)
                        transitions.append({
                            "id": p.id,
                            "timestamp": p.timestamp,
                            "action": p.action,
                            "tool": p.tool,
                            "before_state": p.before_state,
                            "after_state": p.after_state,
                            "result_summary": p.result_summary[:200] if p.result_summary else "",
                            "risk_level": p.risk_level,
                            "session_id": p.session_id,
                        })
        return {"entity_id": entity_id, "depth": depth, "transitions": transitions, "count": len(transitions)}
    except Exception as e:
        log.warning("temporal_graph failed: %s", e)
        return {"entity_id": entity_id, "depth": depth, "transitions": [], "error": str(e)}


async def _tool_temporal_chain(args: dict) -> dict:
    entity_id = args.get("entity_id", "")
    since = args.get("since", "")
    if not entity_id:
        return {"error": "entity_id is required", "chain": []}
    try:
        from services.audit import get_logger
        logger = get_logger()
        entries = logger.query(agent=entity_id, since=since, limit=50)
        session_ids = set()
        for e in entries:
            if e.session_id:
                session_ids.add(e.session_id)
        chains = {}
        for sid in list(session_ids)[:5]:
            chain = logger.get_chain(sid)
            chains[sid] = [
                {
                    "id": e.id,
                    "timestamp": e.timestamp,
                    "action": e.action,
                    "tool": e.tool,
                    "before_state": e.before_state,
                    "after_state": e.after_state,
                    "result_summary": e.result_summary[:200] if e.result_summary else "",
                    "risk_level": e.risk_level,
                    "parent_action_id": e.parent_action_id,
                }
                for e in chain
            ]
        return {"entity_id": entity_id, "chain": chains, "session_count": len(chains)}
    except Exception as e:
        log.warning("temporal_chain failed: %s", e)
        return {"entity_id": entity_id, "chain": {}, "error": str(e)}


async def _tool_temporal_snapshot(args: dict) -> dict:
    entity_id = args.get("entity_id", "")
    before_state = args.get("before_state", "")
    after_state = args.get("after_state", "")
    action = args.get("action", "")
    summary = args.get("summary", "")
    if not entity_id or not before_state or not after_state or not action:
        return {"error": "entity_id, before_state, after_state, and action are required"}
    try:
        from services.audit import get_logger
        logger = get_logger()
        entry = logger.log(
            action=action,
            agent=entity_id,
            tool="temporal_snapshot",
            arguments={"before_state": before_state, "after_state": after_state, "summary": summary},
            result_summary=summary or f"{entity_id}: {before_state} → {after_state}",
            before_state=before_state,
            after_state=after_state,
        )
        return {
            "status": "recorded",
            "id": entry.id,
            "timestamp": entry.timestamp,
            "entity_id": entity_id,
            "action": action,
            "before_state": before_state,
            "after_state": after_state,
        }
    except Exception as e:
        log.warning("temporal_snapshot failed: %s", e)
        return {"error": str(e), "status": "failed"}


async def _tool_kg_query(args: dict) -> dict:
    entity = args.get("entity", "")
    relation = args.get("relation", "")
    depth = int(args.get("depth", 1))
    if _memory_mgr is None:
        return {"entity": entity, "relation": relation or "any", "depth": depth, "triples": [], "note": "services.memory not available"}
    try:
        query = entity
        if relation:
            query = f"{entity} {relation}"
        results = _memory_mgr.search(query, mem_type=SQLiteMemoryType.KNOWLEDGE_GRAPH, limit=20)
        triples = []
        for m in results:
            meta = m.metadata or {}
            triples.append({
                "subject": meta.get("subject", ""),
                "predicate": meta.get("predicate", ""),
                "object": meta.get("object", ""),
                "content": m.content[:300],
                "source": meta.get("source", "agent"),
                "created_at": m.created_at,
                "importance": m.importance,
            })
        return {"entity": entity, "relation": relation or "any", "depth": depth, "triples": triples, "count": len(triples)}
    except Exception as e:
        log.warning("kg_query failed: %s", e)
        return {"entity": entity, "relation": relation or "any", "depth": depth, "triples": [], "error": str(e)}


async def _tool_kg_store(args: dict) -> dict:
    subject = args.get("subject", "")
    predicate = args.get("predicate", "")
    obj = args.get("object", "")
    source = args.get("source", "agent")
    if not subject or not predicate or not obj:
        return {"error": "subject, predicate, and object are required", "status": "failed"}
    if _memory_mgr is None:
        return {"status": "stored", "triple": f"{subject} → {predicate} → {obj}", "source": source, "note": "services.memory not available — recorded locally"}
    try:
        triple_str = f"{subject} | {predicate} | {obj}"
        mem_id = _memory_mgr.store(
            agent=args.get("_agent", "default"),
            mem_type=SQLiteMemoryType.KNOWLEDGE_GRAPH,
            content=triple_str,
            summary=f"{subject} → {predicate} → {obj}",
            metadata={"subject": subject, "predicate": predicate, "object": obj, "source": source},
            importance=0.8,
        )
        return {"status": "stored", "id": mem_id, "triple": f"{subject} → {predicate} → {obj}", "source": source}
    except Exception as e:
        log.warning("kg_store failed: %s", e)
        return {"status": "stored", "triple": f"{subject} → {predicate} → {obj}", "source": source, "note": f"Fallback: {e}"}


# ─── Preference memory implementations ──────────────────────────────

async def _tool_preference_note(args: dict) -> dict:
    key = args.get("key", "")
    value = args.get("value", "")
    context = args.get("context", "")
    if not key or not value:
        return {"error": "key and value are required", "status": "failed"}
    agent = args.get("_agent", "default")
    if _memory_mgr is None:
        return {"status": "noted", "key": key, "value": value, "note": "services.memory not available"}
    try:
        pref_str = f"{key} = {value}"
        if context:
            pref_str += f" [{context}]"
        mem_id = _memory_mgr.store(
            agent=agent,
            mem_type=SQLiteMemoryType.PREFERENCE,
            content=pref_str,
            summary=f"preference: {key} = {value}",
            metadata={"key": key, "value": value, "context": context, "source": "conversation"},
            importance=0.6,
        )
        return {"status": "stored", "id": mem_id, "key": key, "value": value}
    except Exception as e:
        log.warning("preference_note failed: %s", e)
        return {"status": "noted", "key": key, "value": value, "note": str(e)}


async def _tool_preference_query(args: dict) -> dict:
    key = args.get("key", "")
    if not key:
        return {"error": "key is required", "preferences": []}
    if _memory_mgr is None:
        return {"key": key, "preferences": [], "note": "services.memory not available"}
    try:
        results = _memory_mgr.search(key, mem_type=SQLiteMemoryType.PREFERENCE, limit=20)
        prefs = []
        for m in results:
            meta = m.metadata or {}
            prefs.append({
                "key": meta.get("key", ""),
                "value": meta.get("value", ""),
                "context": meta.get("context", ""),
                "content": m.content[:300],
                "created_at": m.created_at,
                "importance": m.importance,
            })
        return {"key": key, "preferences": prefs, "count": len(prefs)}
    except Exception as e:
        log.warning("preference_query failed: %s", e)
        return {"key": key, "preferences": [], "error": str(e)}


# ─── Vault tool implementations ────────────────────────────────────

async def _tool_vault_sync(args: dict) -> dict:
    try:
        from services.hitl_vault import HITLVault
        vault = HITLVault()
        result = vault.sync()
        return {"status": "synced", **result}
    except Exception as e:
        log.warning("vault_sync failed: %s", e)
        return {"error": str(e)}


async def _tool_vault_list(args: dict) -> dict:
    try:
        from services.hitl_vault import HITLVault
        vault = HITLVault()
        entries = vault.list(status=args.get("status"))
        return {"count": len(entries), "entries": entries}
    except Exception as e:
        log.warning("vault_list failed: %s", e)
        return {"error": str(e), "entries": []}


async def _tool_vault_note(args: dict) -> dict:
    title = args.get("title", "")
    body = args.get("body", "")
    tags_str = args.get("tags", "")
    if not title or not body:
        return {"error": "title and body are required"}
    try:
        from services.hitl_vault import HITLVault
        vault = HITLVault()
        tags = [t.strip() for t in tags_str.split(",") if t.strip()] if tags_str else []
        result = vault.create(title, body, tags=tags)
        return {"status": "created", **result}
    except Exception as e:
        log.warning("vault_note failed: %s", e)
        return {"error": str(e)}


async def _tool_vault_approve(args: dict) -> dict:
    note_id = args.get("note_id", "")
    reason = args.get("reason", "")
    if not note_id:
        return {"error": "note_id is required"}
    try:
        from services.hitl_vault import HITLVault
        vault = HITLVault()
        result = vault.approve(note_id, decided_by="agent", reason=reason)
        return result
    except Exception as e:
        log.warning("vault_approve failed: %s", e)
        return {"error": str(e)}


async def _tool_vault_deny(args: dict) -> dict:
    note_id = args.get("note_id", "")
    reason = args.get("reason", "")
    if not note_id:
        return {"error": "note_id is required"}
    try:
        from services.hitl_vault import HITLVault
        vault = HITLVault()
        result = vault.deny(note_id, decided_by="agent", reason=reason)
        return result
    except Exception as e:
        log.warning("vault_deny failed: %s", e)
        return {"error": str(e)}


async def _tool_vault_stats(args: dict) -> dict:
    try:
        from services.hitl_vault import HITLVault
        vault = HITLVault()
        return vault.stats()
    except Exception as e:
        log.warning("vault_stats failed: %s", e)
        return {"error": str(e)}


# ─── Goals tool implementations ────────────────────────────────────

async def _tool_goal_create(args: dict) -> dict:
    try:
        from services.goals import get_db
        db = get_db()
        labels = [l.strip() for l in args.get("labels", "").split(",") if l.strip()] if args.get("labels") else None
        goal = db.goal_create(
            title=args.get("title", ""),
            description=args.get("description", ""),
            priority=args.get("priority", "medium"),
            owner=args.get("owner", ""),
            target_date=args.get("target_date", ""),
            labels=labels,
        )
        return {"status": "created", "goal": goal.to_dict()}
    except Exception as e:
        log.warning("goal_create failed: %s", e)
        return {"error": str(e)}


async def _tool_goal_list(args: dict) -> dict:
    try:
        from services.goals import get_db
        db = get_db()
        goals = db.goal_list(status=args.get("status") or None, owner=args.get("owner") or None)
        return {"count": len(goals), "goals": [g.to_dict() for g in goals]}
    except Exception as e:
        log.warning("goal_list failed: %s", e)
        return {"error": str(e), "goals": []}


async def _tool_goal_update(args: dict) -> dict:
    gid = args.get("goal_id", "")
    if not gid:
        return {"error": "goal_id is required"}
    try:
        from services.goals import get_db
        db = get_db()
        goal = db.goal_update(gid, title=args.get("title"), description=args.get("description"),
                              status=args.get("status"), priority=args.get("priority"),
                              owner=args.get("owner"), target_date=args.get("target_date"))
        if not goal:
            return {"error": f"goal {gid} not found"}
        return {"status": "updated", "goal": goal.to_dict()}
    except Exception as e:
        log.warning("goal_update failed: %s", e)
        return {"error": str(e)}


async def _tool_mission_create(args: dict) -> dict:
    try:
        from services.goals import get_db
        db = get_db()
        teams = [t.strip() for t in args.get("teams", "").split(",") if t.strip()] if args.get("teams") else None
        mission = db.mission_create(
            goal_id=args.get("goal_id", ""),
            title=args.get("title", ""),
            description=args.get("description", ""),
            lead=args.get("lead", ""),
            target_date=args.get("target_date", ""),
            teams=teams,
        )
        if not mission:
            return {"error": f"goal {args.get('goal_id')} not found"}
        return {"status": "created", "mission": mission.to_dict()}
    except Exception as e:
        log.warning("mission_create failed: %s", e)
        return {"error": str(e)}


async def _tool_mission_list(args: dict) -> dict:
    try:
        from services.goals import get_db
        db = get_db()
        missions = db.mission_list(goal_id=args.get("goal_id") or None, status=args.get("status") or None)
        return {"count": len(missions), "missions": [m.to_dict() for m in missions]}
    except Exception as e:
        log.warning("mission_list failed: %s", e)
        return {"error": str(e), "missions": []}


async def _tool_task_create(args: dict) -> dict:
    try:
        from services.goals import get_db
        db = get_db()
        depends = [d.strip() for d in args.get("depends_on", "").split(",") if d.strip()] if args.get("depends_on") else None
        task = db.task_create(
            mission_id=args.get("mission_id", ""),
            title=args.get("title", ""),
            description=args.get("description", ""),
            priority=args.get("priority", "medium"),
            assignee=args.get("assignee", ""),
            depends_on=depends,
        )
        if not task:
            return {"error": f"mission {args.get('mission_id')} not found"}
        return {"status": "created", "task": task.to_dict()}
    except Exception as e:
        log.warning("task_create failed: %s", e)
        return {"error": str(e)}


async def _tool_task_list(args: dict) -> dict:
    try:
        from services.goals import get_db
        db = get_db()
        tasks = db.task_list(mission_id=args.get("mission_id") or None,
                             status=args.get("status") or None,
                             assignee=args.get("assignee") or None)
        return {"count": len(tasks), "tasks": [t.to_dict() for t in tasks]}
    except Exception as e:
        log.warning("task_list failed: %s", e)
        return {"error": str(e), "tasks": []}


async def _tool_task_complete(args: dict) -> dict:
    tid = args.get("task_id", "")
    if not tid:
        return {"error": "task_id is required"}
    try:
        from services.goals import get_db
        db = get_db()
        task = db.task_update(tid, status="done")
        if not task:
            return {"error": f"task {tid} not found"}
        return {"status": "completed", "task": task.to_dict()}
    except Exception as e:
        log.warning("task_complete failed: %s", e)
        return {"error": str(e)}


# ─── Schedule tool implementations ─────────────────────────────────

async def _tool_create_schedule(args: dict) -> dict:
    description = args.get("description", "")
    agent = args.get("agent", "")
    action = args.get("action", "")
    schedule = args.get("schedule", "")
    if not all([description, agent, action, schedule]):
        return {"error": "description, agent, action, and schedule are required"}
    try:
        from services.webhooks import schedule_add
        cron_expr = _parse_natural_cron(schedule)
        sched_id = schedule_add(cron_expr, agent, action, description=description)
        return {"status": "created", "id": sched_id, "cron": cron_expr, "agent": agent, "action": action}
    except Exception as e:
        return {"error": str(e)}


async def _tool_list_schedules(args: dict) -> dict:
    try:
        from services.webhooks import schedule_list
        rows = schedule_list()
        schedules = [
            {"id": r["id"], "cron": r["cron_expr"], "agent": r["agent"],
             "action": r["action"], "description": r.get("description", ""),
             "enabled": r.get("enabled", 1), "next_run": r.get("next_run", "")}
            for r in rows
        ]
        return {"count": len(schedules), "schedules": schedules}
    except Exception as e:
        return {"error": str(e), "schedules": []}


async def _tool_remove_schedule(args: dict) -> dict:
    sched_id = args.get("schedule_id", "")
    if not sched_id:
        return {"error": "schedule_id is required"}
    try:
        from services.webhooks import schedule_remove
        removed = schedule_remove(sched_id)
        return {"status": "removed" if removed else "not_found", "id": sched_id}
    except Exception as e:
        return {"error": str(e)}


# ─── Memory tool definitions ────────────────────────────────────────

def _tool_memory_note_definition():
    return {
        "type": "function",
        "function": {
            "name": "memory_note",
            "description": "Manage your MEMORY.md working notes. Use add to append a note, replace to overwrite a topic, remove to delete a topic.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["add", "replace", "remove"], "description": "What to do with the note"},
                    "content": {"type": "string", "description": "Note content (for add/replace)"},
                    "topic": {"type": "string", "description": "Topic heading to replace or remove (omit for add)"},
                },
                "required": ["action", "content"],
            },
        },
    }


def _tool_user_profile_definition():
    return {
        "type": "function",
        "function": {
            "name": "user_profile",
            "description": "Manage USER.md — your model of the user's preferences, context, and past requests.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["add", "replace"], "description": "Add a new preference or replace an existing one"},
                    "content": {"type": "string", "description": "Preference or profile detail"},
                    "topic": {"type": "string", "description": "Topic heading to add under or replace"},
                },
                "required": ["action", "content"],
            },
        },
    }


def _tool_archive_search_definition():
    return {
        "type": "function",
        "function": {
            "name": "archive_search",
            "description": "Search past sessions by keyword or agent. Returns matching session history.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search keyword or phrase"},
                    "agent": {"type": "string", "description": "Filter by agent name (optional)"},
                    "limit": {"type": "integer", "description": "Max results (default 10)"},
                },
                "required": ["query"],
            },
        },
    }


def _tool_temporal_graph_definition():
    return {
        "type": "function",
        "function": {
            "name": "temporal_graph",
            "description": "Query temporal state transitions for an entity. Shows how an entity changed over time.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string", "description": "Entity identifier (e.g. machine-07, workflow-42)"},
                    "since": {"type": "string", "description": "ISO timestamp or relative (e.g. 24h, 7d) (optional)"},
                    "depth": {"type": "integer", "description": "How many hops to traverse (default 1)"},
                },
                "required": ["entity_id"],
            },
        },
    }


def _tool_temporal_chain_definition():
    return {
        "type": "function",
        "function": {
            "name": "temporal_chain",
            "description": "Trace complete history of an entity through all state transitions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string", "description": "Entity identifier"},
                    "since": {"type": "string", "description": "ISO timestamp or relative (optional)"},
                },
                "required": ["entity_id"],
            },
        },
    }


def _tool_temporal_snapshot_definition():
    return {
        "type": "function",
        "function": {
            "name": "temporal_snapshot",
            "description": "Record a state transition for an entity. Stores before/after snapshot in the audit trail for compliance tracking.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string", "description": "Entity identifier (e.g. machine-07, workflow-42, config-nginx)"},
                    "before_state": {"type": "string", "description": "State before the transition (JSON or description)"},
                    "after_state": {"type": "string", "description": "State after the transition (JSON or description)"},
                    "action": {"type": "string", "description": "Action that caused the transition (e.g. update, deploy, rollback)"},
                    "summary": {"type": "string", "description": "Human-readable summary of what changed"},
                },
                "required": ["entity_id", "before_state", "after_state", "action"],
            },
        },
    }


def _tool_kg_query_definition():
    return {
        "type": "function",
        "function": {
            "name": "kg_query",
            "description": "Query the knowledge graph for entity relationships.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity": {"type": "string", "description": "Entity to query (e.g. CNC-machine-07)"},
                    "relation": {"type": "string", "description": "Optional relation filter (e.g. requires_maintenance_every)"},
                    "depth": {"type": "integer", "description": "How many hops (default 1)"},
                },
                "required": ["entity"],
            },
        },
    }


def _tool_kg_store_definition():
    return {
        "type": "function",
        "function": {
            "name": "kg_store",
            "description": "Store a knowledge triple in the knowledge graph.",
            "parameters": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string", "description": "Subject entity"},
                    "predicate": {"type": "string", "description": "Relationship predicate"},
                    "object": {"type": "string", "description": "Object entity or value"},
                    "source": {"type": "string", "description": "Source document or context (optional)"},
                },
                "required": ["subject", "predicate", "object"],
            },
        },
    }


def _tool_preference_note_definition():
    return {
        "type": "function",
        "function": {
            "name": "preference_note",
            "description": "Store a user preference learned from the current conversation. Persisted with PREFERENCE memory type.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Preference key (e.g. theme, notification_level, language)"},
                    "value": {"type": "string", "description": "Preference value (e.g. dark, quiet, en)"},
                    "context": {"type": "string", "description": "Optional context about when this preference applies"},
                },
                "required": ["key", "value"],
            },
        },
    }


def _tool_preference_query_definition():
    return {
        "type": "function",
        "function": {
            "name": "preference_query",
            "description": "Retrieve stored user preferences matching a key or query.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Preference key to look up (e.g. theme, notification_level)"},
                },
                "required": ["key"],
            },
        },
    }


def _tool_vault_sync_definition():
    return {
        "type": "function",
        "function": {
            "name": "vault_sync",
            "description": "Sync HITL approval requests from hitl.db into the Obsidian vault as markdown notes.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    }


def _tool_vault_list_definition():
    return {
        "type": "function",
        "function": {
            "name": "vault_list",
            "description": "List all entries in the HITL vault, optionally filtered by status.",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "description": "Filter by status: pending, approved, denied, expired (optional)"},
                },
            },
        },
    }


def _tool_vault_note_definition():
    return {
        "type": "function",
        "function": {
            "name": "vault_note",
            "description": "Create an ad-hoc HITL vault note (markdown with frontmatter) for human review.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Note title"},
                    "body": {"type": "string", "description": "Note body (markdown)"},
                    "tags": {"type": "string", "description": "Comma-separated tags (optional)"},
                },
                "required": ["title", "body"],
            },
        },
    }


def _tool_vault_approve_definition():
    return {
        "type": "function",
        "function": {
            "name": "vault_approve",
            "description": "Approve a HITL vault entry (marks pending approval as approved in both vault and hitl.db).",
            "parameters": {
                "type": "object",
                "properties": {
                    "note_id": {"type": "string", "description": "Vault note ID to approve"},
                    "reason": {"type": "string", "description": "Optional approval reason"},
                },
                "required": ["note_id"],
            },
        },
    }


def _tool_vault_deny_definition():
    return {
        "type": "function",
        "function": {
            "name": "vault_deny",
            "description": "Deny a HITL vault entry (marks approval as denied in both vault and hitl.db).",
            "parameters": {
                "type": "object",
                "properties": {
                    "note_id": {"type": "string", "description": "Vault note ID to deny"},
                    "reason": {"type": "string", "description": "Optional denial reason"},
                },
                "required": ["note_id"],
            },
        },
    }


def _tool_vault_stats_definition():
    return {
        "type": "function",
        "function": {
            "name": "vault_stats",
            "description": "Get HITL vault statistics (total, pending, approved, denied, expired counts).",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    }


def _parse_natural_cron(text: str) -> str:
    """Convert natural language to a 5-field cron expression."""
    t = text.lower().strip()
    mappings = {
        "every minute": "* * * * *",
        "every hour": "0 * * * *",
        "every 2 hours": "0 */2 * * *",
        "every 3 hours": "0 */3 * * *",
        "every 6 hours": "0 */6 * * *",
        "every 12 hours": "0 */12 * * *",
        "every day": "0 0 * * *",
        "daily": "0 0 * * *",
        "every weekday": "0 0 * * 1-5",
        "every week": "0 0 * * 0",
        "weekly": "0 0 * * 0",
        "every month": "0 0 1 * *",
        "monthly": "0 0 1 * *",
    }
    if t in mappings:
        return mappings[t]
    for pattern, expr in [
        (r"every (\d+) minutes?", lambda m: f"*/{m.group(1)} * * * *"),
        (r"every (\d+) hours?", lambda m: f"0 */{m.group(1)} * * *"),
        (r"every (\d+) days?", lambda m: f"0 0 */{m.group(1)} * *"),
        (r"every day at (\d+)(?::(\d+))?(\s*[ap]m)?", _parse_at),
        (r"daily at (\d+)(?::(\d+))?(\s*[ap]m)?", _parse_at),
        (r"at (\d+)(?::(\d+))?(\s*[ap]m)?", _parse_at),
    ]:
        import re
        m = re.search(pattern, t)
        if m:
            return expr(m)
    return t


def _parse_at(m):
    hour = int(m.group(1))
    minute = int(m.group(2)) if m.group(2) else 0
    ampm = (m.group(3) or "").strip().lower()
    if ampm == "pm" and hour < 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    return f"{minute} {hour} * * *"


def _tool_create_schedule_definition():
    return {
        "type": "function",
        "function": {
            "name": "create_schedule",
            "description": "Create a scheduled task using natural language (e.g. 'every hour', 'daily at 9am', 'every 30 minutes'). The job will be dispatched to an agent at the specified interval.",
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {"type": "string", "description": "What to do, e.g. 'check system health' or 'remind me every hour'"},
                    "agent": {"type": "string", "description": "Agent to execute the task (proxy, romi, ergo, or your name)"},
                    "action": {"type": "string", "description": "Command to send to the agent, e.g. 'check system health'"},
                    "schedule": {"type": "string", "description": "Cron expression or natural language (e.g. '0 * * * *', 'every hour', 'daily at 9am')"},
                },
                "required": ["description", "agent", "action", "schedule"],
            },
        },
    }


def _tool_list_schedules_definition():
    return {
        "type": "function",
        "function": {
            "name": "list_schedules",
            "description": "List all scheduled tasks.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    }


def _tool_remove_schedule_definition():
    return {
        "type": "function",
        "function": {
            "name": "remove_schedule",
            "description": "Remove a scheduled task by ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "schedule_id": {"type": "string", "description": "Schedule ID to remove"},
                },
                "required": ["schedule_id"],
            },
        },
    }


def _tool_goal_create_definition():
    return {
        "type": "function",
        "function": {
            "name": "goal_create",
            "description": "Create a new strategic Goal. Goals are top-level objectives that contain Missions and Tasks.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Goal title"},
                    "description": {"type": "string", "description": "Goal description (optional)"},
                    "priority": {"type": "string", "enum": ["none", "urgent", "high", "medium", "low"], "description": "Priority level (default medium)"},
                    "owner": {"type": "string", "description": "Agent responsible (e.g. ergo, proxy)"},
                    "target_date": {"type": "string", "description": "Target completion date (ISO or quarter, optional)"},
                    "labels": {"type": "string", "description": "Comma-separated labels (optional)"},
                },
                "required": ["title"],
            },
        },
    }


def _tool_goal_list_definition():
    return {
        "type": "function",
        "function": {
            "name": "goal_list",
            "description": "List all Goals, optionally filtered by status or owner.",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "description": "Filter by status (optional): proposed, planned, active, completed, canceled"},
                    "owner": {"type": "string", "description": "Filter by owner agent name (optional)"},
                },
            },
        },
    }


def _tool_goal_update_definition():
    return {
        "type": "function",
        "function": {
            "name": "goal_update",
            "description": "Update a Goal's status, priority, owner, or target date.",
            "parameters": {
                "type": "object",
                "properties": {
                    "goal_id": {"type": "string", "description": "Goal ID to update"},
                    "title": {"type": "string", "description": "New title (optional)"},
                    "status": {"type": "string", "description": "New status: proposed, planned, active, completed, canceled"},
                    "priority": {"type": "string", "description": "New priority: none, urgent, high, medium, low"},
                    "owner": {"type": "string", "description": "New owner agent name"},
                    "target_date": {"type": "string", "description": "New target date"},
                },
                "required": ["goal_id"],
            },
        },
    }


def _tool_mission_create_definition():
    return {
        "type": "function",
        "function": {
            "name": "mission_create",
            "description": "Create a Mission under a Goal. Missions are tactical workstreams that execute toward a Goal.",
            "parameters": {
                "type": "object",
                "properties": {
                    "goal_id": {"type": "string", "description": "Parent Goal ID"},
                    "title": {"type": "string", "description": "Mission title"},
                    "description": {"type": "string", "description": "Mission description (optional)"},
                    "lead": {"type": "string", "description": "Agent leading this mission (optional)"},
                    "target_date": {"type": "string", "description": "Target date (optional)"},
                    "teams": {"type": "string", "description": "Comma-separated agent team names (optional)"},
                },
                "required": ["goal_id", "title"],
            },
        },
    }


def _tool_mission_list_definition():
    return {
        "type": "function",
        "function": {
            "name": "mission_list",
            "description": "List Missions, optionally filtered by Goal ID or status.",
            "parameters": {
                "type": "object",
                "properties": {
                    "goal_id": {"type": "string", "description": "Filter by parent Goal ID (optional)"},
                    "status": {"type": "string", "description": "Filter by status: backlog, planned, in_progress, done, canceled"},
                },
            },
        },
    }


def _tool_task_create_definition():
    return {
        "type": "function",
        "function": {
            "name": "task_create",
            "description": "Create a Task under a Mission. Tasks are the smallest assignable unit of work.",
            "parameters": {
                "type": "object",
                "properties": {
                    "mission_id": {"type": "string", "description": "Parent Mission ID"},
                    "title": {"type": "string", "description": "Task title"},
                    "description": {"type": "string", "description": "Task description (optional)"},
                    "priority": {"type": "string", "enum": ["none", "urgent", "high", "medium", "low"], "description": "Priority (default medium)"},
                    "assignee": {"type": "string", "description": "Agent assigned (optional)"},
                    "depends_on": {"type": "string", "description": "Comma-separated task IDs this depends on (optional)"},
                },
                "required": ["mission_id", "title"],
            },
        },
    }


def _tool_task_list_definition():
    return {
        "type": "function",
        "function": {
            "name": "task_list",
            "description": "List Tasks, optionally filtered by Mission ID, status, or assignee.",
            "parameters": {
                "type": "object",
                "properties": {
                    "mission_id": {"type": "string", "description": "Filter by parent Mission ID (optional)"},
                    "status": {"type": "string", "description": "Filter by status: todo, in_progress, review, done"},
                    "assignee": {"type": "string", "description": "Filter by assignee agent name"},
                },
            },
        },
    }


def _tool_task_complete_definition():
    return {
        "type": "function",
        "function": {
            "name": "task_complete",
            "description": "Mark a Task as done. Updates Mission and Goal progress and health automatically.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Task ID to mark complete"},
                },
                "required": ["task_id"],
            },
        },
    }


def _tool_delegate_definition():
    return {
        "type": "function",
        "function": {
            "name": "delegate",
            "description": "Fan-out parallel delegation — execute multiple tasks across agents simultaneously and return aggregated results.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tasks": {
                        "type": "array",
                        "description": "List of tasks to execute in parallel",
                        "items": {
                            "type": "object",
                            "properties": {
                                "agent": {"type": "string", "description": "Target agent to delegate to"},
                                "command": {"type": "string", "description": "Command string to send"},
                                "args": {"type": "object", "description": "Optional extra arguments"},
                            },
                            "required": ["agent", "command"],
                        },
                    },
                },
                "required": ["tasks"],
            },
        },
    }


# ─── Tool Definitions (Ollama function calling format) ──────────────
TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "shell",
            "description": "Execute a shell command and return its output. Use for running programs, checking system status, installing packages, etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command to execute"},
                    "timeout": {"type": "integer", "description": "Timeout in seconds (default 30)"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file to read"},
                    "lines": {"type": "integer", "description": "Max lines to read (optional)"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file. Creates the file if it doesn't exist.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file to write"},
                    "content": {"type": "string", "description": "Content to write"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List contents of a directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the directory"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "http_get",
            "description": "Make an HTTP GET request and return the response.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to fetch"},
                    "headers": {"type": "object", "description": "Optional headers"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "http_post",
            "description": "Make an HTTP POST request with JSON body.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to post to"},
                    "body": {"type": "object", "description": "JSON body"},
                    "headers": {"type": "object", "description": "Optional headers"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Search for files by name pattern or grep content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern or grep regex"},
                    "path": {"type": "string", "description": "Directory to search in"},
                    "content": {"type": "string", "description": "If set, grep for this in file contents"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delegate_to_agent",
            "description": "Delegate a task to another agent. Use for multi-agent coordination.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent": {"type": "string", "description": "Agent name (proxy, romi, ergo)"},
                    "command": {"type": "string", "description": "Command to send"},
                    "args": {"type": "object", "description": "Optional arguments"},
                    "plant": {"type": "string", "description": "Target plant id (cross-plant ACL enforced)"},
                    "target_plant": {"type": "string", "description": "Alias for plant"},
                },
                "required": ["agent", "command"],
            },
        },
    },
    _tool_opencode_definition(),
    _tool_opendesign_definition(),
    _tool_memory_note_definition(),
    _tool_user_profile_definition(),
    _tool_archive_search_definition(),
    _tool_temporal_graph_definition(),
    _tool_temporal_chain_definition(),
    _tool_temporal_snapshot_definition(),
    _tool_kg_query_definition(),
    _tool_kg_store_definition(),
    _tool_create_schedule_definition(),
    _tool_list_schedules_definition(),
    _tool_remove_schedule_definition(),
    _tool_preference_note_definition(),
    _tool_preference_query_definition(),
    _tool_vault_sync_definition(),
    _tool_vault_list_definition(),
    _tool_vault_note_definition(),
    _tool_vault_approve_definition(),
    _tool_vault_deny_definition(),
    _tool_vault_stats_definition(),
    _tool_goal_create_definition(),
    _tool_goal_list_definition(),
    _tool_goal_update_definition(),
    _tool_mission_create_definition(),
    _tool_mission_list_definition(),
    _tool_task_create_definition(),
    _tool_task_list_definition(),
    _tool_task_complete_definition(),
    _tool_delegate_definition(),
]


# ─── Tool Execution ─────────────────────────────────────────────────
_executor = CommandExecutor(sandbox=True)


async def execute_tool(name: str, arguments: dict, nats=None, callbacks: dict = None) -> dict:
    """Execute a tool by name with given arguments.

    Args:
        name: Tool name
        arguments: Tool arguments dict
        nats: NATS connection for delegation
        callbacks: Optional dict of callbacks for streaming progress
    """
    callbacks = callbacks or {}

    # Auto-repair arguments (Hermes pattern)
    arguments = repair_tool_arguments(arguments, name)

    # Fleet red/blue + cross-plant ACL (never unrestricted OpenCode for red-team)
    try:
        from fleet_policy import check_tool
        denial = check_tool(name, arguments=arguments)
        if denial:
            result = {"error": True, "message": denial, "policy": "fleet"}
            if "tool_complete" in callbacks:
                callbacks["tool_complete"](name, result)
            return result
    except ImportError:
        pass

    # Optional C11 policyexec tool gate — STARSHIP_POLICY_NATIVE=1
    try:
        from policy_native import native_enabled as policy_native_on, check_tool as policy_check_tool
        if policy_native_on():
            denial = policy_check_tool(name)
            if denial:
                result = {"error": True, "message": denial, "policy": "policyexec"}
                if "tool_complete" in callbacks:
                    callbacks["tool_complete"](name, result)
                return result
    except ImportError:
        pass
    except Exception as e:
        log.debug("policyexec tool check fallback: %s", e)

    # Emit tool start (Hermes callback pattern)
    if "tool_start" in callbacks:
        callbacks["tool_start"](name, arguments)

    try:
        if name == "shell":
            result = await _tool_shell(arguments)
        elif name == "read_file":
            result = _tool_read_file(arguments)
        elif name == "write_file":
            result = _tool_write_file(arguments)
        elif name == "list_dir":
            result = _tool_list_dir(arguments)
        elif name == "http_get":
            result = await _tool_http_get(arguments)
        elif name == "http_post":
            result = await _tool_http_post(arguments)
        elif name == "search_files":
            result = _tool_search_files(arguments)
        elif name == "delegate_to_agent":
            result = await _tool_delegate(nats, arguments)
        elif name == "delegate":
            result = await _tool_delegate_parallel(nats, arguments)
        elif name == "opencode":
            result = await _tool_opencode(arguments)
        elif name == "opendesign":
            result = await _tool_opendesign(arguments)
        elif name == "memory_note":
            result = _tool_memory_note(arguments)
        elif name == "user_profile":
            result = _tool_user_profile(arguments)
        elif name == "archive_search":
            result = await _tool_archive_search(arguments)
        elif name == "temporal_graph":
            result = await _tool_temporal_graph(arguments)
        elif name == "temporal_chain":
            result = await _tool_temporal_chain(arguments)
        elif name == "temporal_snapshot":
            result = await _tool_temporal_snapshot(arguments)
        elif name == "kg_query":
            result = await _tool_kg_query(arguments)
        elif name == "kg_store":
            result = await _tool_kg_store(arguments)
        elif name == "preference_note":
            result = await _tool_preference_note(arguments)
        elif name == "preference_query":
            result = await _tool_preference_query(arguments)
        elif name == "vault_sync":
            result = await _tool_vault_sync(arguments)
        elif name == "vault_list":
            result = await _tool_vault_list(arguments)
        elif name == "vault_note":
            result = await _tool_vault_note(arguments)
        elif name == "vault_approve":
            result = await _tool_vault_approve(arguments)
        elif name == "vault_deny":
            result = await _tool_vault_deny(arguments)
        elif name == "vault_stats":
            result = await _tool_vault_stats(arguments)
        elif name == "goal_create":
            result = await _tool_goal_create(arguments)
        elif name == "goal_list":
            result = await _tool_goal_list(arguments)
        elif name == "goal_update":
            result = await _tool_goal_update(arguments)
        elif name == "mission_create":
            result = await _tool_mission_create(arguments)
        elif name == "mission_list":
            result = await _tool_mission_list(arguments)
        elif name == "task_create":
            result = await _tool_task_create(arguments)
        elif name == "task_list":
            result = await _tool_task_list(arguments)
        elif name == "task_complete":
            result = await _tool_task_complete(arguments)
        elif name == "create_schedule":
            result = await _tool_create_schedule(arguments)
        elif name == "list_schedules":
            result = await _tool_list_schedules(arguments)
        elif name == "remove_schedule":
            result = await _tool_remove_schedule(arguments)
        else:
            result = {"error": True, "message": f"Unknown tool: {name}"}
    except ToolError as e:
        result = e.to_dict()
    except Exception as e:
        log.error("Tool execution error (%s): %s", name, e)
        result = {"error": True, "message": str(e)}

    # Redact secrets from output (Flamingo pattern)
    if "output" in result:
        result["output"] = redact(result["output"])

    # Emit tool complete (Hermes callback pattern)
    if "tool_complete" in callbacks:
        callbacks["tool_complete"](name, result)

    return result


async def _tool_shell(args: dict) -> dict:
    cmd = args.get("command", "")
    timeout = args.get("timeout", DEFAULT_TIMEOUT)
    result = await _executor.execute(cmd, timeout=timeout)
    return result.to_dict()


def _tool_read_file(args: dict) -> dict:
    path = args.get("path", "")
    lines = args.get("lines", 0)

    if not _check_path(path, "read"):
        raise AccessDeniedError(path, "read")

    try:
        p = Path(path)
        if not p.exists():
            return {"content": f"File not found: {path}", "error": True}
        if p.stat().st_size > MAX_FILE_SIZE:
            return {"content": f"File too large ({p.stat().st_size} bytes)", "error": True}

        content = p.read_text(errors="replace")
        if lines > 0:
            content = "\n".join(content.splitlines()[:lines])
        return {"content": content, "error": False}
    except ToolError:
        raise
    except Exception as e:
        return {"content": str(e), "error": True}


def _tool_write_file(args: dict) -> dict:
    path = args.get("path", "")
    content = args.get("content", "")

    if not _check_path(path, "write"):
        raise AccessDeniedError(path, "write")

    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return {"output": f"Written {len(content)} bytes to {path}", "error": False}
    except Exception as e:
        return {"output": str(e), "error": True}


def _tool_list_dir(args: dict) -> dict:
    path = args.get("path", ".")

    if not _check_path(path, "read"):
        raise AccessDeniedError(path, "list")

    try:
        p = Path(path)
        if not p.exists():
            return {"entries": [], "error": True, "message": f"Not found: {path}"}
        entries = []
        for item in sorted(p.iterdir()):
            entry = {"name": item.name, "type": "dir" if item.is_dir() else "file"}
            try:
                entry["size"] = item.stat().st_size
            except Exception:
                entry["size"] = 0
            entries.append(entry)
        return {"entries": entries, "error": False}
    except Exception as e:
        return {"entries": [], "error": True, "message": str(e)}


async def _tool_http_get(args: dict) -> dict:
    import httpx
    url = args.get("url", "")
    headers = args.get("headers", {})

    if not url.startswith(("http://", "https://")):
        return {"status_code": 0, "body": "Invalid URL", "error": True}

    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            body = resp.text[:MAX_OUTPUT_SIZE]
            return {"status_code": resp.status_code, "body": redact(body), "error": resp.status_code >= 400}
    except Exception as e:
        return {"status_code": 0, "body": str(e), "error": True}


async def _tool_http_post(args: dict) -> dict:
    import httpx
    url = args.get("url", "")
    body = args.get("body", {})
    headers = args.get("headers", {})
    headers.setdefault("Content-Type", "application/json")

    if not url.startswith(("http://", "https://")):
        return {"status_code": 0, "body": "Invalid URL", "error": True}

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, json=body, headers=headers)
            resp_body = redact(resp.text[:MAX_OUTPUT_SIZE])
            return {"status_code": resp.status_code, "body": resp_body, "error": resp.status_code >= 400}
    except Exception as e:
        return {"status_code": 0, "body": str(e), "error": True}


def _tool_search_files(args: dict) -> dict:
    pattern = args.get("pattern", "*")
    path = args.get("path", ".")
    content = args.get("content", "")

    if not _check_path(path, "read"):
        raise AccessDeniedError(path, "search")

    try:
        p = Path(path)
        if content:
            results = []
            for f in p.rglob("*"):
                if f.is_file() and f.stat().st_size < MAX_FILE_SIZE:
                    try:
                        text = f.read_text(errors="replace")
                        for i, line in enumerate(text.splitlines(), 1):
                            if content.lower() in line.lower():
                                results.append({"file": str(f), "line": i, "match": line.strip()[:200]})
                                if len(results) >= 50:
                                    break
                    except Exception:
                        continue
                if len(results) >= 50:
                    break
            return {"results": results, "error": False}
        else:
            results = []
            for f in p.glob(pattern):
                results.append({"path": str(f), "type": "dir" if f.is_dir() else "file"})
                if len(results) >= 100:
                    break
            return {"results": results, "error": False}
    except Exception as e:
        return {"results": [], "error": True, "message": str(e)}


async def _tool_delegate(nats, args: dict) -> dict:
    agent = args.get("agent", "")
    command = args.get("command", "")
    extra_args = args.get("args", {}) or {}
    target_plant = args.get("plant") or args.get("target_plant")

    if not nats:
        return {"error": "NATS not connected — cannot delegate"}

    # Dual-publish primary starship.* + legacy agnetic.*
    try:
        from nats_subjects import dual
        subjects = dual(f"starship.agent.{agent}.command.{command.replace(' ', '.')}")
    except ImportError:
        cmd = command.replace(" ", ".")
        subjects = [
            f"starship.agent.{agent}.command.{cmd}",
            f"agnetic.agent.{agent}.command.{cmd}",
        ]

    reply = f"starship.delegate.{datetime.now().timestamp()}"
    payload = json.dumps({
        "command": command,
        "args": extra_args,
        "reply_to": reply,
        "plant": target_plant,
    }).encode()

    try:
        sub = await nats.subscribe(reply, max_msgs=1)
        for subject in subjects:
            await nats.publish(subject, payload)

        msg = await sub.next_msg(timeout=60)
        result = json.loads(msg.data.decode())
        return result
    except asyncio.TimeoutError:
        return {"error": f"Agent '{agent}' did not respond in 60s"}
    except Exception as e:
        return {"error": str(e)}


async def _tool_delegate_parallel(nats, args: dict) -> dict:
    import asyncio
    tasks = args.get("tasks", [])
    if not tasks:
        return {"error": "No tasks provided", "results": []}

    async def run_one(t):
        result = await _tool_delegate(nats, {"agent": t.get("agent", ""), "command": t.get("command", ""), "args": t.get("args", {})})
        return {"agent": t.get("agent", ""), "command": t.get("command", ""), "result": result}

    results = await asyncio.gather(*[run_one(t) for t in tasks], return_exceptions=True)
    output = []
    for r in results:
        if isinstance(r, Exception):
            output.append({"error": str(r)})
        else:
            output.append(r)
    return {"count": len(output), "results": output}


def _check_path(path: str, operation: str = "read") -> bool:
    """Validate path against allowed directories."""
    try:
        resolved = Path(path).resolve()
        paths = ALLOWED_READ_PATHS if operation == "read" else ALLOWED_WRITE_PATHS
        return any(str(resolved).startswith(allowed) for allowed in paths)
    except Exception:
        return False
