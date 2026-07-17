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
import sys
import uuid
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

log = logging.getLogger("agnetic-tools")

try:
    from services.governance import GovernanceManager
except Exception:
    GovernanceManager = None

try:
    import yaml
except ImportError:
    yaml = None

try:
    from services.memory import MemoryManager, MemoryType, get_memory_manager, ProspectiveMemoryManager, MEMORY_DESCRIPTIONS
except Exception:
    MemoryManager = None
    MemoryType = None
    get_memory_manager = None
    ProspectiveMemoryManager = None
    MEMORY_DESCRIPTIONS = {}

try:
    from services.mcp import init_mcp, get_mcp_tool_definitions, call_mcp_tool
except Exception:
    init_mcp = None
    get_mcp_tool_definitions = lambda: []
    call_mcp_tool = None

try:
    from services.checkpoint import get_checkpoint_manager
except Exception:
    get_checkpoint_manager = None

try:
    from services.context_loader import discover_context_files, load_context, find_project_root
except Exception:
    discover_context_files = lambda: []
    load_context = lambda: ""
    find_project_root = None

try:
    from services.event_hooks import get_hook_manager, emit_event
except Exception:
    get_hook_manager = None
    emit_event = None

try:
    from services.credential_pool import get_credential_manager
except Exception:
    get_credential_manager = None

try:
    from services.browser import ensure_browser
except Exception:
    ensure_browser = None

try:
    from lib.plugin_manager import get_plugin_manager
except Exception:
    get_plugin_manager = None

try:
    from services.skills_hub import search_skills_hub, preview_skill, test_skill_sandboxed, install_skill, list_installed_skills
except Exception:
    search_skills_hub = None
    preview_skill = None
    test_skill_sandboxed = None
    install_skill = None
    list_installed_skills = None

try:
    from services.agent_email import get_email_service
except Exception:
    get_email_service = None


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

        if self.dry_run:
            log.info("[DRY RUN] Would execute: %s", redact(command))
            return ExecuteResult(exit_code=0, stdout=f"[DRY RUN] {command}", command=command)

        try:
            self._validate(command)
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
        except SandboxError as e:
            return ExecuteResult(exit_code=-1, stdout="", stderr=str(e), command=command)
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
        "description": "Multi-agent task delegation and dynamic subagent spawning",
        "tools": ["delegate_to_agent", "spawn_subagent", "list_subagents", "kill_subagent"],
    },
    "planning": {
        "description": "Backlog and task management (linear/kanban)",
        "tools": ["create_backlog_item", "update_backlog_item", "list_backlog"],
    },
    "coding": {
        "description": "Code generation and OS expansion via OpenCode/Codex",
        "tools": ["opencode", "codex"],
    },
    "design": {
        "description": "Design artifact generation via Open Design",
        "tools": ["opendesign"],
    },
    "expansion": {
        "description": "Full OS expansion: coding + design + planning",
        "tools": ["opencode", "codex", "opendesign", "create_backlog_item", "update_backlog_item", "list_backlog"],
    },
    "checkpoint": {
        "description": "Filesystem checkpoint and rollback operations",
        "tools": ["checkpoint_create", "checkpoint_list", "checkpoint_restore", "checkpoint_diff"],
    },
    "browser": {
        "description": "Browser automation (Playwright-based web interaction)",
        "tools": ["browser_navigate", "browser_screenshot", "browser_get_content", "browser_click", "browser_fill", "browser_evaluate"],
    },
    "mcp": {
        "description": "Model Context Protocol — external tool integration",
        "tools": [],  # populated dynamically from MCP servers
    },
    "plugins": {
        "description": "Plugin management — install, enable, disable plugins",
        "tools": ["plugins_list", "plugins_enable", "plugins_disable"],
    },
    "context": {
        "description": "Context file auto-discovery and loading",
        "tools": ["context_load"],
    },
    "hooks": {
        "description": "Event hooks system for lifecycle events",
        "tools": ["hook_emit"],
    },
    "credentials": {
        "description": "Credential pool status and management",
        "tools": ["credential_pool_status"],
    },
    "skillshub": {
        "description": "Skills.sh marketplace — search, preview, test, install agent skills",
        "tools": ["skills_search", "skills_preview", "skills_test", "skills_install", "skills_installed"],
    },
    "memory": {
        "description": "Memory systems — store/recall semantic facts, manage prospective intentions, working notes, user profile, temporal graph, knowledge graph, session archive",
        "tools": ["memory_store", "memory_search", "memory_audit", "memory_prospective_create", "memory_prospective_list", "memory_note", "user_profile", "archive_search", "temporal_graph", "temporal_chain", "temporal_snapshot", "kg_query", "kg_store", "preference_note", "preference_query"],
    },
    "email": {
        "description": "Send emails via SMTP and Mailchain Web3, manage agent email addresses",
        "tools": ["send_email", "email_list_inbox", "email_register_address", "email_list_addresses", "email_remove_address"],
    },
    "scheduling": {
        "description": "Create and manage scheduled tasks (cron jobs) with natural language scheduling",
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
        "description": "All available tools (including MCP, browser, checkpoint, plugins, context, hooks, credentials, memory, email, scheduling, vault, goals)",
        "includes": ["core", "network", "delegation", "coding", "design", "planning", "checkpoint", "browser", "mcp", "plugins", "context", "hooks", "credentials", "skillshub", "memory", "email", "scheduling", "vault", "goals"],
    },
    "readonly": {
        "description": "Read-only operations (no writes, no shell)",
        "tools": ["read_file", "list_dir", "search_files", "http_get", "checkpoint_list", "checkpoint_diff", "context_load", "plugins_list", "credential_pool_status", "skills_search", "skills_preview", "skills_test", "skills_installed"],
    },
    "webhook_safe": {
        "description": "Safe tools for untrusted input (no shell, no writes)",
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
OPENCODE_BINARY = shutil.which("opencode") or os.path.expanduser("~/.opencode/bin/opencode") or "/tmp/opencode/bin/opencode"
OPENCODE_TIMEOUT = 300  # 5 min for agent coding tasks


def _tool_opencode_definition():
    return {
        "type": "function",
        "function": {
            "name": "opencode",
            "description": "Invoke OpenCode (or Codex via codex: prefix) for code gen/refactor/debug. Supports codex:review, codex:adversarial-review, codex:rescue, codex:transfer. opencode auto-delegates to Codex on hard problems.",
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
    """Invoke OpenCode for code generation and OS expansion.
    Supports codex:review, codex:adversarial-review, codex:rescue, codex:transfer
    for autonomous Codex CLI delegation. Output matches native Codex.
    """
    prompt = args.get("prompt", "")
    model = args.get("model", "")
    files = args.get("files", [])
    session = args.get("session", "")
    continue_last = args.get("continue_last", False)
    fmt = args.get("format", "json")

    if not prompt:
        return {"error": True, "message": "prompt is required"}

    mode, extra = _parse_codex_mode(prompt)
    if mode:
        ctx = args.get("context", "") or session or ""
        return await _run_codex(mode, extra, ctx)

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


# ─── Codex CLI Integration (via opencode delegation) ─────────────────
CODEX_BINARY = shutil.which("codex") or os.path.expanduser("~/.codex/bin/codex") or "/usr/local/bin/codex"
CODEX_TIMEOUT = 180


async def _run_codex(mode: str, extra: str = "", context: str = "") -> dict:
    """Execute Codex CLI natively for review/adversarial/rescue/transfer.
    Output identical to native codex invocation.
    """
    if not Path(CODEX_BINARY).exists():
        return {"error": True, "message": f"Codex CLI not found at {CODEX_BINARY}. Install Codex CLI to use codex:* modes."}

    cmd_parts = [CODEX_BINARY]
    if mode == "review":
        cmd_parts.extend(["review", "."])
        if extra: cmd_parts.append(extra)
    elif mode == "adversarial-review":
        cmd_parts.extend(["adversarial-review", "."])
        if extra: cmd_parts.append(extra)
    elif mode == "rescue":
        cmd_parts.append("rescue")
        if context: cmd_parts.extend(["--context", context])
        if extra: cmd_parts.append(extra)
    elif mode == "transfer":
        cmd_parts.append("transfer")
        if context: cmd_parts.extend(["--session", context])
        if extra: cmd_parts.append(extra)
    else:
        cmd_parts.append(mode)
        cmd_parts.append(".")

    command = " ".join(shlex.quote(p) for p in cmd_parts)
    result = await _executor.execute(command, timeout=CODEX_TIMEOUT)
    out = result.to_dict()
    out["codex_mode"] = mode
    out["native"] = True
    return out


def _parse_codex_mode(prompt: str):
    p = prompt.strip().lower()
    if not p.startswith("codex:"):
        return None, prompt
    rest = prompt[len("codex:"):].strip()
    parts = rest.split(None, 1)
    mode = parts[0] if parts else "review"
    extra = parts[1] if len(parts) > 1 else ""
    return mode, extra


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
                },
                "required": ["agent", "command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "spawn_subagent",
            "description": "Dynamically spawn a new sub-agent process for delegated work. Returns NATS subject. Supports ephemeral lifetime.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Unique name for the subagent (e.g. sub-debug-abc123)"},
                    "role": {"type": "string", "description": "Role/purpose (e.g. debugger, analyst, fixer)"},
                    "model": {"type": "string", "description": "Ollama model to use (default qwen2.5:3b)"},
                    "initial_task": {"type": "string", "description": "Optional first command to run on spawn"},
                    "timeout": {"type": "integer", "description": "Auto-kill after N seconds (0 = no timeout)"},
                    "ephemeral": {"type": "boolean", "description": "Auto-clean on task completion (default true)"},
                    "backlog_id": {"type": "string", "description": "Link to existing backlog item id for auto-assign + in_progress"},
                },
                "required": ["name", "role"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_subagents",
            "description": "List currently running dynamically spawned subagents.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "kill_subagent",
            "description": "Terminate a spawned subagent by name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Subagent name to kill"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_backlog_item",
            "description": "Create goal/project/task/subtask in live backlog. Links to agents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "description": "goal, project, task, or subtask"},
                    "title": {"type": "string", "description": "Short title"},
                    "description": {"type": "string"},
                    "parent_id": {"type": "string", "description": "Parent item id for hierarchy"},
                    "assignee": {"type": "string", "description": "Agent name assigned"},
                    "status": {"type": "string", "description": "todo, in_progress, done, blocked"},
                },
                "required": ["type", "title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_backlog_item",
            "description": "Update backlog item status, assignee, etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "status": {"type": "string"},
                    "assignee": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_backlog",
            "description": "List backlog items, filter by status/assignee/type.",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {"type": "string"},
                    "assignee": {"type": "string"},
                    "type": {"type": "string"},
                },
            },
        },
    },
    _tool_opencode_definition(),
    _tool_opendesign_definition(),
    {
        "type": "function",
        "function": {
            "name": "flamingo_deploy",
            "description": "Deploy Flamingo micro-agent to remote host for fleet management.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "Target host or IP"},
                    "agent_name": {"type": "string", "description": "Name for the mini agent"},
                },
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "codex",
            "description": "Codex CLI subagent. Use for codex:review, codex:adversarial-review, codex:rescue, codex:transfer. Pipes tree or serializes state. Identical to native Codex CLI.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "e.g. 'codex:review current implementation' or 'codex:rescue stuck task'"},
                    "context": {"type": "string", "description": "Additional serialized context or session"},
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "checkpoint_create",
            "description": "Create a filesystem checkpoint/snapshot for rollback safety. Like Git commit for agent workspace state.",
            "parameters": {
                "type": "object",
                "properties": {
                    "label": {"type": "string", "description": "Label describing this checkpoint"},
                    "paths": {"type": "array", "items": {"type": "string"}, "description": "Paths to snapshot (default: /opt/agnetic, /root)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "checkpoint_list",
            "description": "List all available checkpoints with labels and timestamps.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "checkpoint_restore",
            "description": "Restore filesystem state from a checkpoint. Reverts all tracked files to snapshot state.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Checkpoint ID to restore"},
                },
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "checkpoint_diff",
            "description": "Show what files changed between current state and a checkpoint.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Checkpoint ID to diff against"},
                },
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "context_load",
            "description": "Auto-discover and load context files (.hermes.md, AGENTS.md, CLAUDE.md, SOUL.md, .cursorrules) from the workspace. Injects project context into agent prompts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "label": {"type": "string", "description": "Optional label to prepend to context"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "hook_emit",
            "description": "Emit an event to the event hooks system. Triggers registered gateway and plugin hooks.",
            "parameters": {
                "type": "object",
                "properties": {
                    "event": {"type": "string", "description": "Event name (e.g. agent.command.completed, tool.after_execution, workflow.started)"},
                    "context": {"type": "object", "description": "Event context data"},
                },
                "required": ["event"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "credential_pool_status",
            "description": "Show status of all credential pools — available keys, usage counts, errors.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_navigate",
            "description": "Open a URL in the automated browser. Requires browser to be started.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to navigate to"},
                    "timeout": {"type": "integer", "description": "Navigation timeout in seconds (default 30)"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_screenshot",
            "description": "Take a screenshot of the current browser page. Returns base64-encoded PNG.",
            "parameters": {
                "type": "object",
                "properties": {
                    "full_page": {"type": "boolean", "description": "Capture full page (default false)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_get_content",
            "description": "Get the HTML and text content of the current browser page.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_click",
            "description": "Click an element on the current browser page by CSS selector.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "CSS selector for the element to click"},
                },
                "required": ["selector"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_fill",
            "description": "Fill a form field on the current browser page.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "CSS selector for the input field"},
                    "value": {"type": "string", "description": "Text to fill in"},
                },
                "required": ["selector", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_evaluate",
            "description": "Execute JavaScript in the browser page context.",
            "parameters": {
                "type": "object",
                "properties": {
                    "script": {"type": "string", "description": "JavaScript code to execute"},
                },
                "required": ["script"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "plugins_list",
            "description": "List all installed plugins with their version and status.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "plugins_enable",
            "description": "Enable a plugin by name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Plugin name to enable"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "plugins_disable",
            "description": "Disable a plugin by name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Plugin name to disable"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skills_search",
            "description": "Search the skills.sh marketplace for available skills. Returns matching skills with install counts and sources.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query (e.g. 'deploy', 'security', 'database')"},
                    "source": {"type": "string", "description": "Filter by source: 'all', 'anthropic', 'vercel', 'community', 'microsoft'"},
                    "limit": {"type": "integer", "description": "Max results (default 20)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skills_preview",
            "description": "Preview a skill's metadata and instructions before installing. Shows name, description, version, tags, allowed tools, and the first 2000 chars of instructions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_id": {"type": "string", "description": "Skill ID in format: owner/repo/skill-name (e.g. 'anthropics/skills/frontend-design')"},
                },
                "required": ["skill_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skills_test",
            "description": "Sandbox-test a skill for security and quality before install. Checks YAML validity, dangerous patterns, network/filesystem access, body size, and naming conventions. Returns a security score and recommendation (safe/warning/dangerous/block).",
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_id": {"type": "string", "description": "Skill ID in format: owner/repo/skill-name"},
                },
                "required": ["skill_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skills_install",
            "description": "Install a skill from skills.sh marketplace. Requires user approval flag after preview and test. Installs SKILL.md and optional scripts into /opt/agnetic/skills/<skill-name>/",
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_id": {"type": "string", "description": "Skill ID in format: owner/repo/skill-name"},
                    "approved": {"type": "boolean", "description": "User approval. Must be set to true after reviewing preview and test results."},
                },
                "required": ["skill_id", "approved"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skills_installed",
            "description": "List all installed skills from the skills marketplace with their security levels and install dates.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_email",
            "description": "Send an email via SMTP or Mailchain Web3 protocol. Supports plain text and HTML bodies, CC/BCC recipients.",
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "Recipient email address"},
                    "subject": {"type": "string", "description": "Email subject line"},
                    "body": {"type": "string", "description": "Plain text email body"},
                    "html_body": {"type": "string", "description": "HTML email body (optional)"},
                    "from": {"type": "string", "description": "Sender email address (optional, defaults to SMTP user or agent address)"},
                    "mode": {"type": "string", "enum": ["smtp", "mailchain"], "description": "Delivery method: 'smtp' (default) or 'mailchain' for Web3 email"},
                    "cc": {"type": "array", "items": {"type": "string"}, "description": "CC recipients (optional)"},
                    "bcc": {"type": "array", "items": {"type": "string"}, "description": "BCC recipients (optional)"},
                },
                "required": ["to", "subject", "body"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "email_list_inbox",
            "description": "List received emails. Only works with Mailchain Web3 protocol (SMTP is send-only).",
            "parameters": {
                "type": "object",
                "properties": {
                    "address": {"type": "string", "description": "Mailchain wallet address to fetch inbox for (optional, defaults to configured address)"},
                    "limit": {"type": "integer", "description": "Max messages to return (default 50)"},
                    "mode": {"type": "string", "enum": ["mailchain"], "description": "Only 'mailchain' mode supports inbox listing"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "email_register_address",
            "description": "Register an email address for an agent in the system. Associates an email address with an agent name for sending and receiving.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent": {"type": "string", "description": "Agent name (e.g. 'agnetic-core', 'agnetic-secops')"},
                    "address": {"type": "string", "description": "Email address to register"},
                    "smtp_enabled": {"type": "boolean", "description": "Enable SMTP sending (default true)"},
                    "mailchain_enabled": {"type": "boolean", "description": "Enable Mailchain Web3 (default false)"},
                    "aliases": {"type": "array", "items": {"type": "string"}, "description": "Alternative email addresses for this agent"},
                },
                "required": ["agent", "address"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "email_list_addresses",
            "description": "List all registered email addresses mapped to agents.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "email_remove_address",
            "description": "Remove an agent's email address registration.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent": {"type": "string", "description": "Agent name to remove"},
                },
                "required": ["agent"],
            },
        },
    },

    # ── Persistent memory tools (MEMORY.md / USER.md) ─────────────────

    {
        "type": "function",
        "function": {
            "name": "memory_note",
            "description": "Manage your MEMORY.md working notes. Use add to append a note, replace to overwrite a topic, remove to delete a topic.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["add", "replace", "remove"]},
                    "content": {"type": "string", "description": "Note content"},
                    "topic": {"type": "string", "description": "Topic heading to replace or remove"},
                },
                "required": ["action", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "user_profile",
            "description": "Manage USER.md — your model of the user's preferences, context, and past requests.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["add", "replace"]},
                    "content": {"type": "string", "description": "Preference or profile detail"},
                    "topic": {"type": "string", "description": "Topic heading"},
                },
                "required": ["action", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "archive_search",
            "description": "Search past sessions by keyword or agent.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search keyword"},
                    "agent": {"type": "string", "description": "Filter by agent (optional)"},
                    "limit": {"type": "integer", "description": "Max results (default 10)"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "temporal_graph",
            "description": "Query temporal state transitions for an entity.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string", "description": "Entity identifier"},
                    "since": {"type": "string", "description": "ISO timestamp or relative"},
                    "depth": {"type": "integer", "description": "Hops to traverse (default 1)"},
                },
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "temporal_chain",
            "description": "Trace complete history of an entity through all state transitions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string", "description": "Entity identifier"},
                    "since": {"type": "string", "description": "ISO timestamp or relative"},
                },
                "required": ["entity_id"],
            },
        },
    },
    {
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
    },
    {
        "type": "function",
        "function": {
            "name": "kg_query",
            "description": "Query the knowledge graph for entity relationships.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity": {"type": "string", "description": "Entity to query"},
                    "relation": {"type": "string", "description": "Optional relation filter"},
                    "depth": {"type": "integer", "description": "Hops (default 1)"},
                },
                "required": ["entity"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "kg_store",
            "description": "Store a knowledge triple in the knowledge graph.",
            "parameters": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string", "description": "Subject entity"},
                    "predicate": {"type": "string", "description": "Relationship"},
                    "object": {"type": "string", "description": "Object entity or value"},
                    "source": {"type": "string", "description": "Source document (optional)"},
                },
                "required": ["subject", "predicate", "object"],
            },
        },
    },
    {
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
    },
    {
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
    },
    {
        "type": "function",
        "function": {
            "name": "vault_sync",
            "description": "Sync HITL approval requests from hitl.db into the Obsidian vault as markdown notes.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
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
    },
    {
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
    },
    {
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
    },
    {
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
    },
    {
        "type": "function",
        "function": {
            "name": "vault_stats",
            "description": "Get HITL vault statistics (total, pending, approved, denied, expired counts).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "goal_create",
            "description": "Create a new strategic Goal.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Goal title"},
                    "description": {"type": "string", "description": "Goal description (optional)"},
                    "priority": {"type": "string", "enum": ["none", "urgent", "high", "medium", "low"], "description": "Priority (default medium)"},
                    "owner": {"type": "string", "description": "Agent responsible"},
                    "target_date": {"type": "string", "description": "Target date (optional)"},
                    "labels": {"type": "string", "description": "Comma-separated labels (optional)"},
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "goal_list",
            "description": "List all Goals, optionally filtered by status or owner.",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "description": "Filter by status (optional)"},
                    "owner": {"type": "string", "description": "Filter by owner (optional)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "goal_update",
            "description": "Update a Goal's status, priority, owner, or target date.",
            "parameters": {
                "type": "object",
                "properties": {
                    "goal_id": {"type": "string", "description": "Goal ID to update"},
                    "status": {"type": "string", "description": "New status: proposed, planned, active, completed, canceled"},
                    "priority": {"type": "string", "description": "New priority"},
                    "owner": {"type": "string", "description": "New owner"},
                    "target_date": {"type": "string", "description": "New target date"},
                },
                "required": ["goal_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mission_create",
            "description": "Create a Mission under a Goal.",
            "parameters": {
                "type": "object",
                "properties": {
                    "goal_id": {"type": "string", "description": "Parent Goal ID"},
                    "title": {"type": "string", "description": "Mission title"},
                    "description": {"type": "string", "description": "Optional description"},
                    "lead": {"type": "string", "description": "Agent leading this mission"},
                    "target_date": {"type": "string", "description": "Target date"},
                    "teams": {"type": "string", "description": "Comma-separated agent teams"},
                },
                "required": ["goal_id", "title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mission_list",
            "description": "List Missions, optionally filtered by Goal ID or status.",
            "parameters": {
                "type": "object",
                "properties": {
                    "goal_id": {"type": "string", "description": "Filter by Goal ID"},
                    "status": {"type": "string", "description": "Filter by status"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_create",
            "description": "Create a Task under a Mission.",
            "parameters": {
                "type": "object",
                "properties": {
                    "mission_id": {"type": "string", "description": "Parent Mission ID"},
                    "title": {"type": "string", "description": "Task title"},
                    "description": {"type": "string", "description": "Optional description"},
                    "priority": {"type": "string", "enum": ["none", "urgent", "high", "medium", "low"], "description": "Priority (default medium)"},
                    "assignee": {"type": "string", "description": "Agent assigned"},
                    "depends_on": {"type": "string", "description": "Comma-separated dependency task IDs"},
                },
                "required": ["mission_id", "title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_list",
            "description": "List Tasks, optionally filtered by Mission ID, status, or assignee.",
            "parameters": {
                "type": "object",
                "properties": {
                    "mission_id": {"type": "string", "description": "Filter by Mission ID"},
                    "status": {"type": "string", "description": "Filter by status"},
                    "assignee": {"type": "string", "description": "Filter by assignee"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_complete",
            "description": "Mark a Task as done. Updates Mission and Goal health automatically.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Task ID to mark complete"},
                },
                "required": ["task_id"],
            },
        },
    },

    # ── Schedule management tools ─────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "create_schedule",
            "description": "Create a scheduled task using natural language (e.g. 'every hour', 'daily at 9am'). Dispatches to an agent at the specified interval.",
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {"type": "string", "description": "What to do"},
                    "agent": {"type": "string", "description": "Agent to execute"},
                    "action": {"type": "string", "description": "Command to send"},
                    "schedule": {"type": "string", "description": "Cron expression or natural language"},
                },
                "required": ["description", "agent", "action", "schedule"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_schedules",
            "description": "List all scheduled tasks.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
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
    },
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
        elif name in ("opencode", "codex"):
            if name == "codex" and not arguments.get("prompt", "").startswith("codex:"):
                p = arguments.get("prompt") or arguments.get("command", "")
                arguments = dict(arguments)
                arguments["prompt"] = "codex:" + p
            result = await _tool_opencode(arguments)
        elif name == "opendesign":
            result = await _tool_opendesign(arguments)
        elif name == "flamingo_deploy":
            result = await _tool_flamingo_deploy(arguments, nats)
        elif name == "spawn_subagent":
            result = await _tool_spawn_subagent(arguments, nats)
        elif name == "list_subagents":
            result = await _tool_list_subagents(arguments)
        elif name == "kill_subagent":
            result = await _tool_kill_subagent(arguments)
        elif name == "create_backlog_item":
            result = await _tool_create_backlog_item(arguments)
        elif name == "update_backlog_item":
            result = await _tool_update_backlog_item(arguments)
        elif name == "list_backlog":
            result = await _tool_list_backlog(arguments)
        elif name in ("mcp_" + t for t in (get_mcp_tool_definitions() or [])):
            if call_mcp_tool:
                result = await call_mcp_tool(name, arguments)
            else:
                result = {"error": True, "message": "MCP not available"}
        elif name == "checkpoint_create":
            result = await _tool_checkpoint_create(arguments)
        elif name == "checkpoint_list":
            result = _tool_checkpoint_list(arguments)
        elif name == "checkpoint_restore":
            result = await _tool_checkpoint_restore(arguments)
        elif name == "checkpoint_diff":
            result = _tool_checkpoint_diff(arguments)
        elif name == "context_load":
            result = _tool_context_load(arguments)
        elif name == "hook_emit":
            result = await _tool_hook_emit(arguments)
        elif name == "credential_pool_status":
            result = _tool_credential_pool_status(arguments)
        elif name.startswith("browser_"):
            result = await _tool_browser(name, arguments)
        elif name == "plugins_list":
            result = _tool_plugins_list(arguments)
        elif name == "plugins_enable":
            result = _tool_plugins_enable(arguments)
        elif name == "plugins_disable":
            result = _tool_plugins_disable(arguments)
        elif name.startswith("plugin_"):
            if get_plugin_manager:
                pm = get_plugin_manager()
                result = await pm.call_tool(name, arguments)
            else:
                result = {"error": True, "message": "Plugin system not available"}
        elif name == "skills_search":
            result = await _tool_skills_search(arguments)
        elif name == "skills_preview":
            result = await _tool_skills_preview(arguments)
        elif name == "skills_test":
            result = await _tool_skills_test(arguments)
        elif name == "skills_install":
            result = await _tool_skills_install(arguments)
        elif name == "skills_installed":
            result = _tool_skills_installed(arguments)
        elif name == "memory_store":
            result = await _tool_memory_store(arguments)
        elif name == "memory_search":
            result = await _tool_memory_search(arguments)
        elif name == "memory_prospective_create":
            result = await _tool_memory_prospective_create(arguments)
        elif name == "memory_prospective_list":
            result = await _tool_memory_prospective_list(arguments)
        elif name == "memory_audit":
            result = await _tool_memory_audit(arguments)
        elif name == "send_email":
            result = await _tool_send_email(arguments)
        elif name == "email_list_inbox":
            result = await _tool_email_list_inbox(arguments)
        elif name == "email_register_address":
            result = await _tool_email_register_address(arguments)
        elif name == "email_list_addresses":
            result = _tool_email_list_addresses(arguments)
        elif name == "email_remove_address":
            result = await _tool_email_remove_address(arguments)
        elif name == "memory_note":
            result = _tool_memory_note_adv(arguments)
        elif name == "user_profile":
            result = _tool_user_profile_adv(arguments)
        elif name == "archive_search":
            result = _tool_archive_search_adv(arguments)
        elif name == "temporal_graph":
            result = _tool_temporal_graph_adv(arguments)
        elif name == "temporal_chain":
            result = _tool_temporal_chain_adv(arguments)
        elif name == "temporal_snapshot":
            result = _tool_temporal_snapshot_adv(arguments)
        elif name == "kg_query":
            result = _tool_kg_query_adv(arguments)
        elif name == "kg_store":
            result = _tool_kg_store_adv(arguments)
        elif name == "preference_note":
            result = _tool_preference_note_adv(arguments)
        elif name == "preference_query":
            result = _tool_preference_query_adv(arguments)
        elif name == "vault_sync":
            result = _tool_vault_sync_adv(arguments)
        elif name == "vault_list":
            result = _tool_vault_list_adv(arguments)
        elif name == "vault_note":
            result = _tool_vault_note_adv(arguments)
        elif name == "vault_approve":
            result = _tool_vault_approve_adv(arguments)
        elif name == "vault_deny":
            result = _tool_vault_deny_adv(arguments)
        elif name == "vault_stats":
            result = _tool_vault_stats_adv(arguments)
        elif name == "goal_create":
            result = _tool_goal_create_adv(arguments)
        elif name == "goal_list":
            result = _tool_goal_list_adv(arguments)
        elif name == "goal_update":
            result = _tool_goal_update_adv(arguments)
        elif name == "mission_create":
            result = _tool_mission_create_adv(arguments)
        elif name == "mission_list":
            result = _tool_mission_list_adv(arguments)
        elif name == "task_create":
            result = _tool_task_create_adv(arguments)
        elif name == "task_list":
            result = _tool_task_list_adv(arguments)
        elif name == "task_complete":
            result = _tool_task_complete_adv(arguments)
        elif name == "create_schedule":
            result = _tool_create_schedule_adv(arguments)
        elif name == "list_schedules":
            result = _tool_list_schedules_adv(arguments)
        elif name == "remove_schedule":
            result = _tool_remove_schedule_adv(arguments)
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
    extra_args = args.get("args", {})

    if not nats:
        return {"error": "NATS not connected — cannot delegate"}

    subject = f"agnetic.agent.{agent}.command.{command.replace(' ', '.')}"
    reply = f"agnetic.delegate.{datetime.now().timestamp()}"

    try:
        sub = await nats.subscribe(reply, max_msgs=1)
        await nats.publish(subject, json.dumps({
            "command": command,
            "args": extra_args,
            "reply_to": reply,
        }).encode())

        msg = await sub.next_msg(timeout=60)
        result = json.loads(msg.data.decode())
        return result
    except asyncio.TimeoutError:
        return {"error": f"Agent '{agent}' did not respond in 60s"}
    except Exception as e:
        return {"error": str(e)}


# ─── Dynamic Sub-Agent Spawning + Lifecycle ───────────────────────────
import uuid

class SubAgentManager:
    """Runtime manager for dynamically spawned sub-agents with lifecycle. Uses LanceDB for shared registry."""
    def __init__(self):
        self.agents: dict[str, dict] = {}  # local live proc tracking
        self._base_cmd = [sys.executable, str(Path(__file__).parent / "agent_daemon.py")]
        self.memory = MemoryManager() if MemoryManager else None
        self.MemType = MemoryType if MemoryType else None

    def _get_subject(self, name: str) -> str:
        return f"agnetic.agent.{name}.command.>"

    async def spawn(self, name: str, role: str, model: str = None, initial_task: str = None,
                    timeout: int = 0, ephemeral: bool = True, nats=None, backlog_id: str = None) -> dict:
        if name in self.agents:
            return {"error": f"Subagent {name} already exists"}

        # Create minimal runtime config dir
        sub_dir = Path("/tmp/agnetic-subagents")
        sub_dir.mkdir(parents=True, exist_ok=True)
        cfg_path = sub_dir / f"{name}.yaml"
        cfg = {
            "name": name,
            "role": role,
            "model": model or "qwen2.5:3b",
            "provider": "ollama",
            "ephemeral": ephemeral,
            "capabilities": [role],
            "skills": [],
        }
        if yaml:
            cfg_path.write_text(yaml.safe_dump(cfg))
        else:
            cfg_path.write_text(json.dumps(cfg))

        # Launch
        cmd = self._base_cmd + [name, "--role", role]
        if model:
            cmd += ["--model", model]
        if ephemeral:
            cmd.append("--ephemeral")

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            entry = {
                "name": name,
                "role": role,
                "model": model or "qwen2.5:3b",
                "pid": proc.pid,
                "subject": f"agnetic.agent.{name}.command.>",
                "started": datetime.now(timezone.utc).isoformat(),
                "timeout": timeout,
                "ephemeral": ephemeral,
                "status": "running",
                "current_task": initial_task or ""
            }
            self.agents[name] = {"proc": proc, **entry}

            # Persist to LanceDB shared registry
            if self.memory and self.MemType:
                reg_entry = {k: v for k, v in entry.items()}
                self.memory.store_sync("subagent_registry", self.MemType.PROCEDURAL, json.dumps(reg_entry), metadata={"type": "subagent", "name": name}, importance=0.7)

            # Link to backlog if provided: auto-assign + set in_progress
            if backlog_id and _backlog_manager:
                try:
                    _backlog_manager.update(backlog_id, assignee=name, status="in_progress")
                except Exception:
                    pass

            # Optional initial delegation
            if initial_task and nats:
                try:
                    await nats.publish(
                        f"agnetic.agent.{name}.command.{initial_task.replace(' ', '.')}",
                        json.dumps({"command": initial_task, "args": {}}).encode()
                    )
                except Exception:
                    pass

            # Timeout killer if set
            if timeout > 0:
                asyncio.create_task(self._timeout_kill(name, timeout))

            return {"name": name, "subject": entry["subject"], "pid": proc.pid, "status": "spawned"}
        except Exception as e:
            return {"error": str(e)}

    async def _timeout_kill(self, name: str, seconds: int):
        await asyncio.sleep(seconds)
        await self.kill(name)

    async def kill(self, name: str) -> dict:
        if name not in self.agents:
            return {"error": "not found"}
        entry = self.agents[name]
        try:
            if entry.get("proc"):
                entry["proc"].terminate()
                try:
                    await asyncio.wait_for(entry["proc"].wait(), timeout=5)
                except asyncio.TimeoutError:
                    entry["proc"].kill()
            entry["status"] = "killed"
            del self.agents[name]
            # Remove from shared registry
            if self.memory:
                results = self.memory.search_sync(f"subagent {name}", limit=1)
                for m in results:
                    if m.metadata.get("name") == name:
                        self.memory.forget(m.id)
                        break
            return {"name": name, "status": "killed"}
        except Exception as e:
            return {"error": str(e)}

    def list(self) -> list:
        # Prefer shared LanceDB registry for cross-agent visibility
        live_names = set(self.agents.keys())
        if self.memory:
            try:
                results = self.memory.search_sync("", limit=200)
                registry = []
                for m in results:
                    try:
                        data = json.loads(m.content)
                        if isinstance(data, dict) and data.get("name") and data.get("subject"):
                            live = self.agents.get(data["name"], {})
                            data["status"] = live.get("status", data.get("status", "unknown"))
                            data["current_task"] = live.get("current_task", data.get("current_task", ""))
                            registry.append({k: v for k, v in data.items() if k != "proc"})
                    except:
                        pass
                # Include any from live that aren't in registry
                for name, entry in self.agents.items():
                    if name not in {r.get("name") for r in registry}:
                        registry.append({k: v for k, v in entry.items() if k != "proc"})
                return registry
            except Exception as e:
                log.warning("SubAgent registry search failed: %s", e)
        # fallback local
        return [
            {k: v for k, v in entry.items() if k != "proc"}
            for entry in self.agents.values()
        ]

_subagent_manager = SubAgentManager()


async def _tool_spawn_subagent(args: dict, nats=None) -> dict:
    name = args.get("name") or f"sub-{uuid.uuid4().hex[:8]}"
    role = args.get("role", "helper")
    model = args.get("model")
    initial_task = args.get("initial_task")
    timeout = int(args.get("timeout", 0))
    ephemeral = args.get("ephemeral", True)

    # Governance check (high risk)
    if GovernanceManager:
        try:
            gov = GovernanceManager()
            decision = await gov.check_action("system", "spawn_subagent", {"name": name, "role": role})
            if not decision.get("approved", True):
                return {"error": f"Governance blocked spawn: {decision.get('reason')}"}
        except Exception:
            pass

    return await _subagent_manager.spawn(
        name, role, model=model, initial_task=initial_task,
        timeout=timeout, ephemeral=ephemeral, nats=nats,
        backlog_id=args.get("backlog_id")
    )


async def _tool_list_subagents(args: dict = None) -> dict:
    return {"subagents": _subagent_manager.list()}


async def _tool_kill_subagent(args: dict) -> dict:
    name = args.get("name")
    if not name:
        return {"error": "name required"}
    return await _subagent_manager.kill(name)


# ─── Live Backlog (Linear style: goals > projects > tasks > subtasks) ──
class BacklogManager:
    def __init__(self):
        self.memory = MemoryManager() if MemoryManager else None
        self.MemType = MemoryType if MemoryType else None

    def create(self, item_type, title, description="", parent_id=None, assignee=None, status="todo"):
        if not self.memory or not self.MemType:
            return {"error": "no shared memory"}
        item_id = uuid.uuid4().hex[:8]
        data = {
            "id": item_id,
            "type": item_type,
            "title": title,
            "description": description,
            "parent_id": parent_id,
            "assignee": assignee,
            "status": status,
            "created": datetime.now(timezone.utc).isoformat(),
            "updated": datetime.now(timezone.utc).isoformat()
        }
        self.memory.store_sync("backlog", self.MemType.PROCEDURAL, json.dumps(data),
                          metadata={"backlog_id": item_id, "type": item_type, "assignee": assignee or "", "status": status},
                          importance=0.8)
        return data

    def update(self, item_id, **updates):
        if not self.memory or not self.MemType:
            return {"error": "no shared memory"}
        results = self.memory.search_sync("", limit=200)
        for m in results:
            try:
                meta = m.metadata or {}
                bid = meta.get("backlog_id") or (json.loads(m.content) if isinstance(m.content, str) else {}).get("id")
                if bid == item_id:
                    data = json.loads(m.content) if isinstance(m.content, str) else m.content
                    data.update(updates)
                    data["updated"] = datetime.now(timezone.utc).isoformat()
                    self.memory.forget(m.id)
                    self.memory.store_sync("backlog", self.MemType.PROCEDURAL, json.dumps(data),
                                      metadata={"backlog_id": item_id, "type": data.get("type"), "assignee": data.get("assignee",""), "status": data.get("status")},
                                      importance=0.8)
                    return data
            except Exception:
                continue
        return {"error": "not found"}

    def list_items(self, status=None, assignee=None, item_type=None):
        if not self.memory:
            return []
        results = self.memory.search_sync("", limit=200)
        items = []
        for m in results:
            try:
                data = json.loads(m.content)
                if status and data.get("status") != status: continue
                if assignee and data.get("assignee") != assignee: continue
                if item_type and data.get("type") != item_type: continue
                items.append(data)
            except:
                pass
        # sort by type hierarchy then status
        order = {"goal": 0, "project": 1, "task": 2, "subtask": 3}
        items.sort(key=lambda x: (order.get(x.get("type",""), 99), x.get("status",""), x.get("updated","")))
        return items

_backlog_manager = BacklogManager()


async def _tool_create_backlog_item(args: dict) -> dict:
    return _backlog_manager.create(
        args.get("type", "task"),
        args.get("title", ""),
        description=args.get("description", ""),
        parent_id=args.get("parent_id"),
        assignee=args.get("assignee"),
        status=args.get("status", "todo")
    )


async def _tool_update_backlog_item(args: dict) -> dict:
    item_id = args.get("id")
    if not item_id:
        return {"error": "id required"}
    updates = {k: v for k, v in args.items() if k != "id"}
    return _backlog_manager.update(item_id, **updates)


async def _tool_list_backlog(args: dict = None) -> dict:
    args = args or {}
    return {"backlog": _backlog_manager.list_items(
        status=args.get("status"),
        assignee=args.get("assignee"),
        item_type=args.get("type")
    )}


async def _tool_flamingo_deploy(args: dict, nats=None) -> dict:
    target = args.get("target", "")
    agent_name = args.get("agent_name", "flamingo-mini")
    try:
        from services.endpoint_manager import EndpointManager
        mgr = EndpointManager()
        await mgr.connect_nats()
        res = await mgr.deploy_flamingo(target, agent_name)
        if nats:
            await nats.publish("agnetic.flamingo.fleet", json.dumps({"action": "deploy", "target": target, "res": res}).encode())
        return res
    except Exception as e:
        return {"error": str(e)}


# ─── New Tool Handlers (MCP, Checkpoint, Context, Hooks, Credentials, Browser, Plugins) ──


async def _tool_checkpoint_create(args: dict) -> dict:
    if not get_checkpoint_manager:
        return {"error": True, "message": "Checkpoint system not available"}
    mgr = get_checkpoint_manager()
    if emit_event:
        await emit_event("checkpoint.before_create", {"args": args})
    cp = mgr.create(
        label=args.get("label", ""),
        paths=args.get("paths", ["/opt/agnetic", "/root"]),
    )
    if emit_event:
        await emit_event("checkpoint.created", {"id": cp.get("id")})
    return cp


def _tool_checkpoint_list(args: dict) -> dict:
    if not get_checkpoint_manager:
        return {"error": True, "message": "Checkpoint system not available"}
    mgr = get_checkpoint_manager()
    checkpoints = mgr.list()
    return {"checkpoints": checkpoints, "count": len(checkpoints)}


async def _tool_checkpoint_restore(args: dict) -> dict:
    if not get_checkpoint_manager:
        return {"error": True, "message": "Checkpoint system not available"}
    mgr = get_checkpoint_manager()
    cid = args.get("id", "")
    if not cid:
        return {"error": True, "message": "checkpoint id required"}
    if emit_event:
        await emit_event("checkpoint.before_restore", {"id": cid})
    result = mgr.restore(cid)
    if emit_event:
        await emit_event("checkpoint.restored", {"id": cid, "result": result})
    return result


def _tool_checkpoint_diff(args: dict) -> dict:
    if not get_checkpoint_manager:
        return {"error": True, "message": "Checkpoint system not available"}
    mgr = get_checkpoint_manager()
    cid = args.get("id", "")
    if not cid:
        return {"error": True, "message": "checkpoint id required"}
    return mgr.diff(cid)


def _tool_context_load(args: dict) -> dict:
    ctx = load_context(label=args.get("label", ""))
    files = discover_context_files()
    return {"context": ctx, "files_found": files, "count": len(files)}


async def _tool_hook_emit(args: dict) -> dict:
    if not emit_event:
        return {"error": True, "message": "Event hooks not available"}
    event = args.get("event", "")
    context = args.get("context", {})
    if not event:
        return {"error": True, "message": "event name required"}
    results = await emit_event(event, context)
    return {"event": event, "hooks_fired": len(results), "results": results}


def _tool_credential_pool_status(args: dict) -> dict:
    if not get_credential_manager:
        return {"error": True, "message": "Credential pool not available"}
    mgr = get_credential_manager()
    return mgr.get_status()


async def _tool_browser(name: str, args: dict) -> dict:
    if not ensure_browser:
        return {"error": True, "message": "Browser automation not available"}
    bm = await ensure_browser()
    if not bm.is_available and name != "browser_start":
        return {"error": True, "message": "Browser not started. Call browser_start first."}

    if name == "browser_start":
        started = await bm.start(headless=args.get("headless", True))
        return {"status": "started" if started else "failed"}
    elif name == "browser_navigate":
        return await bm.navigate(args.get("url", ""), timeout=args.get("timeout"))
    elif name == "browser_screenshot":
        return await bm.screenshot(full_page=args.get("full_page", False))
    elif name == "browser_get_content":
        return await bm.get_content()
    elif name == "browser_click":
        return await bm.click(args.get("selector", ""))
    elif name == "browser_fill":
        return await bm.fill(args.get("selector", ""), args.get("value", ""))
    elif name == "browser_evaluate":
        return await bm.evaluate(args.get("script", ""))
    elif name == "browser_close":
        await bm.close()
        return {"status": "closed"}
    return {"error": True, "message": f"Unknown browser command: {name}"}


def _tool_plugins_list(args: dict) -> dict:
    if not get_plugin_manager:
        return {"error": True, "message": "Plugin system not available"}
    pm = get_plugin_manager()
    pm.discover()
    return {"plugins": pm.get_status(), "count": len(pm.get_status())}


def _tool_plugins_enable(args: dict) -> dict:
    if not get_plugin_manager:
        return {"error": True, "message": "Plugin system not available"}
    pm = get_plugin_manager()
    return pm.enable(args.get("name", ""))


def _tool_plugins_disable(args: dict) -> dict:
    if not get_plugin_manager:
        return {"error": True, "message": "Plugin system not available"}
    pm = get_plugin_manager()
    return pm.disable(args.get("name", ""))


# ─── Skills Hub Tools ───────────────────────────────────────────────


async def _tool_skills_search(args: dict) -> dict:
    if not search_skills_hub:
        return {"error": True, "message": "Skills hub not available"}
    query = args.get("query", "")
    source = args.get("source", "all")
    limit = args.get("limit", 20)
    results = await search_skills_hub(query=query, source=source, limit=limit)
    return {"skills": results, "count": len(results), "query": query}


async def _tool_skills_preview(args: dict) -> dict:
    if not preview_skill:
        return {"error": True, "message": "Skills hub not available"}
    skill_id = args.get("skill_id", "")
    if not skill_id:
        return {"error": True, "message": "skill_id required (format: owner/repo/skill-name)"}
    result = await preview_skill(skill_id)
    return result


async def _tool_skills_test(args: dict) -> dict:
    if not test_skill_sandboxed:
        return {"error": True, "message": "Skills hub not available"}
    skill_id = args.get("skill_id", "")
    if not skill_id:
        return {"error": True, "message": "skill_id required"}
    result = await test_skill_sandboxed(skill_id)
    return result


async def _tool_skills_install(args: dict) -> dict:
    if not install_skill:
        return {"error": True, "message": "Skills hub not available"}
    skill_id = args.get("skill_id", "")
    approved = args.get("approved", False)
    if not skill_id:
        return {"error": True, "message": "skill_id required"}
    return await install_skill(skill_id, approved=approved)


def _tool_skills_installed(args: dict) -> dict:
    if not list_installed_skills:
        return {"error": True, "message": "Skills hub not available"}
    skills = list_installed_skills()
    return {"installed": skills, "count": len(skills)}


# ── Email System Tools ─────────────────────────────────────────────────

async def _tool_send_email(args: dict) -> dict:
    """Send an email via SMTP or Mailchain."""
    if not get_email_service:
        return {"error": True, "message": "Email service not available"}
    es = get_email_service()
    to = args.get("to", "")
    subject = args.get("subject", "")
    body = args.get("body", "")
    html = args.get("html_body", "")
    from_addr = args.get("from", "")
    mode = args.get("mode", "smtp")
    cc = args.get("cc")
    bcc = args.get("bcc")
    if not to or not subject:
        return {"error": True, "message": "'to' and 'subject' are required"}
    result = await es.send_email(to, subject, body, from_address=from_addr, html_body=html, mode=mode, cc=cc, bcc=bcc)
    return {"id": result.id, "status": result.status, "to": to, "subject": subject, "error": result.error}


async def _tool_email_list_inbox(args: dict) -> dict:
    """List received emails (Mailchain only)."""
    if not get_email_service:
        return {"error": True, "message": "Email service not available"}
    es = get_email_service()
    address = args.get("address", "")
    limit = int(args.get("limit", 50))
    mode = args.get("mode", "mailchain")
    messages = await es.list_inbox(address=address, limit=limit, mode=mode)
    return {"count": len(messages), "messages": [m.to_dict() for m in messages], "mode": mode}


async def _tool_email_register_address(args: dict) -> dict:
    """Register an email address for an agent."""
    if not get_email_service:
        return {"error": True, "message": "Email service not available"}
    es = get_email_service()
    agent = args.get("agent", "")
    address = args.get("address", "")
    smtp = bool(args.get("smtp_enabled", True))
    mailchain = bool(args.get("mailchain_enabled", False))
    aliases = args.get("aliases")
    if not agent or not address:
        return {"error": True, "message": "'agent' and 'address' are required"}
    result = es.register_agent_address(agent, address, smtp_enabled=smtp, mailchain_enabled=mailchain, aliases=aliases)
    return {"agent": result.agent_name, "address": result.email_address, "status": "registered"}


def _tool_email_list_addresses(args: dict) -> dict:
    """List all registered agent email addresses."""
    if not get_email_service:
        return {"error": True, "message": "Email service not available"}
    es = get_email_service()
    addresses = es.list_addresses()
    return {"count": len(addresses), "addresses": [a.to_dict() for a in addresses]}


async def _tool_email_remove_address(args: dict) -> dict:
    """Remove an agent's email address registration."""
    if not get_email_service:
        return {"error": True, "message": "Email service not available"}
    es = get_email_service()
    agent = args.get("agent", "")
    if not agent:
        return {"error": True, "message": "'agent' is required"}
    ok = es.remove_address(agent)
    return {"agent": agent, "removed": ok}


# ── Memory System Tools ────────────────────────────────────────────────

async def _tool_memory_store(args: dict) -> dict:
    """Store a fact into semantic memory."""
    if not get_memory_manager:
        return {"error": True, "message": "Memory system not available"}
    mgr = get_memory_manager()
    agent = args.get("agent", "default")
    content = args.get("content", "")
    mem_type_str = args.get("type", "semantic")
    summary = args.get("summary", content[:120])
    importance = float(args.get("importance", 0.5))
    try:
        mem_type = MemoryType(mem_type_str)
    except ValueError:
        mem_type = MemoryType.SEMANTIC
    mem_id = await mgr.store(agent, mem_type, content, summary=summary, importance=importance)
    return {"id": mem_id, "agent": agent, "type": mem_type_str, "status": "stored"}


async def _tool_memory_search(args: dict) -> dict:
    """Vector search across memory types."""
    if not get_memory_manager:
        return {"error": True, "message": "Memory system not available"}
    mgr = get_memory_manager()
    query = args.get("query", "")
    limit = int(args.get("limit", 5))
    mem_type_str = args.get("type")
    mem_type = MemoryType(mem_type_str) if mem_type_str else None
    results = await mgr.search(query, limit=limit, mem_type=mem_type)
    return {
        "query": query,
        "count": len(results),
        "results": [m.to_dict() for m in results],
    }


async def _tool_memory_prospective_create(args: dict) -> dict:
    """Create a future intention (prospective memory)."""
    if not get_memory_manager:
        return {"error": True, "message": "Memory system not available"}
    mgr = get_memory_manager()
    pm = ProspectiveMemoryManager(mgr)
    agent = args.get("agent", "default")
    description = args.get("description", "")
    due_at = args.get("due_at")
    priority = float(args.get("priority", 0.5))
    if not description:
        return {"error": True, "message": "description required"}
    result = await pm.create_intention(agent, description, due_at=due_at, priority=priority)
    return result


async def _tool_memory_prospective_list(args: dict) -> dict:
    """List pending/deferred/overdue intentions."""
    if not get_memory_manager:
        return {"error": True, "message": "Memory system not available"}
    mgr = get_memory_manager()
    pm = ProspectiveMemoryManager(mgr)
    status = args.get("status", "pending")
    agent = args.get("agent")
    if status == "overdue":
        results = await pm.get_overdue(agent=agent)
    elif status == "upcoming":
        hours = int(args.get("horizon_hours", 24))
        results = await pm.get_upcoming(horizon_hours=hours)
    else:
        results = await pm.get_pending(agent=agent)
    return {
        "status": status,
        "count": len(results),
        "intentions": [m.to_dict() for m in results],
    }


async def _tool_memory_audit(args: dict) -> dict:
    """Audit all 7 memory types — what's implemented and current status."""
    if not MemoryType or not get_memory_manager:
        return {"error": True, "message": "Memory system not available"}
    mgr = get_memory_manager()
    audit = []
    for mt in MemoryType:
        label = MEMORY_DESCRIPTIONS.get(mt, "")
        try:
            sample = await mgr.search("", mem_type=mt, limit=1)
            count = len(await mgr.search("", mem_type=mt, limit=50))
            status = "active" if count > 0 else "ready"
        except Exception as e:
            sample = []
            count = 0
            status = f"error: {e}"
        audit.append({
            "type": mt.value,
            "description": label,
            "status": status,
            "stored_items": count,
        })
    return {"memory_types": audit, "total_types": len(audit)}


MEMORY_DIR = Path(os.path.dirname(os.path.abspath(__file__))).parent.parent / "memory"


def _tool_memory_note_adv(args: dict) -> dict:
    action = args.get("action", "add")
    content = args.get("content", "")
    topic = args.get("topic", "")
    agent = args.get("_agent", "default")
    p = MEMORY_DIR / agent / "MEMORY.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    md = p.read_text().strip() if p.exists() else ""
    lines = md.split("\n") if md else []
    if action == "add":
        lines.append(f"\n## {topic}\n{content}" if topic else content)
        p.write_text("\n".join(lines).strip())
        return {"status": "appended", "topic": topic or "general", "agent": agent}
    elif action == "replace":
        new_lines, in_sec, found = [], False, False
        for line in lines:
            if line.startswith(f"## {topic}"):
                in_sec, found = True, True
                new_lines.append(f"## {topic}\n{content}")
                continue
            if in_sec:
                if line.startswith("## "):
                    in_sec = False
                    new_lines.append(line)
                continue
            new_lines.append(line)
        if not found:
            new_lines.append(f"\n## {topic}\n{content}")
        p.write_text("\n".join(new_lines).strip())
        return {"status": "replaced", "topic": topic, "agent": agent}
    elif action == "remove":
        new_lines, in_sec = [], False
        for line in lines:
            if line.startswith(f"## {topic}"):
                in_sec = True
                continue
            if in_sec:
                if line.startswith("## "):
                    in_sec = False
                    new_lines.append(line)
                continue
            new_lines.append(line)
        p.write_text("\n".join(new_lines).strip())
        return {"status": "removed", "topic": topic, "agent": agent}
    return {"error": f"unknown action: {action}"}


def _tool_user_profile_adv(args: dict) -> dict:
    action = args.get("action", "add")
    content = args.get("content", "")
    topic = args.get("topic", "")
    agent = args.get("_agent", "default")
    p = MEMORY_DIR / agent / "USER.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    md = p.read_text().strip() if p.exists() else ""
    lines = md.split("\n") if md else []
    if action == "add":
        if topic:
            lines.append(f"\n## {topic}\n{content}")
        else:
            lines.append(f"- {content}")
        p.write_text("\n".join(lines).strip())
        return {"status": "added", "topic": topic or "general", "agent": agent}
    elif action == "replace":
        new_lines, in_sec, found = [], False, False
        for line in lines:
            if line.startswith(f"## {topic}"):
                in_sec, found = True, True
                new_lines.append(f"## {topic}\n{content}")
                continue
            if in_sec:
                if line.startswith("## "):
                    in_sec = False
                    new_lines.append(line)
                continue
            new_lines.append(line)
        if not found:
            new_lines.append(f"\n## {topic}\n{content}")
        p.write_text("\n".join(new_lines).strip())
        return {"status": "replaced", "topic": topic, "agent": agent}
    return {"error": f"unknown action: {action}"}


def _tool_archive_search_adv(args: dict) -> dict:
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


def _tool_temporal_graph_adv(args: dict) -> dict:
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


def _tool_temporal_chain_adv(args: dict) -> dict:
    entity_id = args.get("entity_id", "")
    since = args.get("since", "")
    if not entity_id:
        return {"error": "entity_id is required", "chain": {}}
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


def _tool_temporal_snapshot_adv(args: dict) -> dict:
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


def _tool_kg_query_adv(args: dict) -> dict:
    entity = args.get("entity", "")
    relation = args.get("relation", "")
    depth = int(args.get("depth", 1))
    try:
        mgr = get_memory_manager()
        query = entity
        if relation:
            query = f"{entity} {relation}"
        results = mgr.search_sync(query, mem_type=MemoryType.KNOWLEDGE_GRAPH, limit=20)
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


def _tool_kg_store_adv(args: dict) -> dict:
    subject = args.get("subject", "")
    predicate = args.get("predicate", "")
    obj = args.get("object", "")
    source = args.get("source", "agent")
    if not subject or not predicate or not obj:
        return {"error": "subject, predicate, and object are required", "status": "failed"}
    try:
        mgr = get_memory_manager()
        triple_str = f"{subject} | {predicate} | {obj}"
        mem_id = mgr.store_sync(
            agent=args.get("_agent", "default"),
            mem_type=MemoryType.KNOWLEDGE_GRAPH,
            content=triple_str,
            summary=f"{subject} → {predicate} → {obj}",
            metadata={"subject": subject, "predicate": predicate, "object": obj, "source": source},
            importance=0.8,
        )
        return {"status": "stored", "id": mem_id, "triple": f"{subject} → {predicate} → {obj}", "source": source}
    except Exception as e:
        log.warning("kg_store failed: %s", e)
        return {"status": "stored", "triple": f"{subject} → {predicate} → {obj}", "source": source, "note": f"Falling back to local record: {e}"}


def _tool_preference_note_adv(args: dict) -> dict:
    key = args.get("key", "")
    value = args.get("value", "")
    context = args.get("context", "")
    if not key or not value:
        return {"error": "key and value are required", "status": "failed"}
    agent = args.get("_agent", "default")
    try:
        mgr = get_memory_manager()
        pref_str = f"{key} = {value}"
        if context:
            pref_str += f" [{context}]"
        mem_id = mgr.store_sync(
            agent=agent,
            mem_type=MemoryType.PREFERENCE,
            content=pref_str,
            summary=f"preference: {key} = {value}",
            metadata={"key": key, "value": value, "context": context, "source": "conversation"},
            importance=0.6,
        )
        return {"status": "stored", "id": mem_id, "key": key, "value": value}
    except Exception as e:
        log.warning("preference_note failed: %s", e)
        return {"status": "noted", "key": key, "value": value, "note": str(e)}


def _tool_vault_sync_adv(args: dict) -> dict:
    try:
        from services.hitl_vault import HITLVault
        vault = HITLVault()
        result = vault.sync()
        return {"status": "synced", **result}
    except Exception as e:
        log.warning("vault_sync failed: %s", e)
        return {"error": str(e)}


def _tool_vault_list_adv(args: dict) -> dict:
    try:
        from services.hitl_vault import HITLVault
        vault = HITLVault()
        entries = vault.list(status=args.get("status"))
        return {"count": len(entries), "entries": entries}
    except Exception as e:
        log.warning("vault_list failed: %s", e)
        return {"error": str(e), "entries": []}


def _tool_vault_note_adv(args: dict) -> dict:
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


def _tool_vault_approve_adv(args: dict) -> dict:
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


def _tool_vault_deny_adv(args: dict) -> dict:
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


def _tool_vault_stats_adv(args: dict) -> dict:
    try:
        from services.hitl_vault import HITLVault
        vault = HITLVault()
        return vault.stats()
    except Exception as e:
        log.warning("vault_stats failed: %s", e)
        return {"error": str(e)}


# ── Goals tool implementations ─────────────────────────────────────

def _tool_goal_create_adv(args: dict) -> dict:
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


def _tool_goal_list_adv(args: dict) -> dict:
    try:
        from services.goals import get_db
        db = get_db()
        goals = db.goal_list(status=args.get("status") or None, owner=args.get("owner") or None)
        return {"count": len(goals), "goals": [g.to_dict() for g in goals]}
    except Exception as e:
        log.warning("goal_list failed: %s", e)
        return {"error": str(e), "goals": []}


def _tool_goal_update_adv(args: dict) -> dict:
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


def _tool_mission_create_adv(args: dict) -> dict:
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


def _tool_mission_list_adv(args: dict) -> dict:
    try:
        from services.goals import get_db
        db = get_db()
        missions = db.mission_list(goal_id=args.get("goal_id") or None, status=args.get("status") or None)
        return {"count": len(missions), "missions": [m.to_dict() for m in missions]}
    except Exception as e:
        log.warning("mission_list failed: %s", e)
        return {"error": str(e), "missions": []}


def _tool_task_create_adv(args: dict) -> dict:
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


def _tool_task_list_adv(args: dict) -> dict:
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


def _tool_task_complete_adv(args: dict) -> dict:
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


def _tool_preference_query_adv(args: dict) -> dict:
    key = args.get("key", "")
    if not key:
        return {"error": "key is required", "preferences": []}
    try:
        mgr = get_memory_manager()
        results = mgr.search_sync(key, mem_type=MemoryType.PREFERENCE, limit=20)
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


def _parse_natural_cron(text: str) -> str:
    t = text.lower().strip()
    mappings = {
        "every minute": "* * * * *", "every hour": "0 * * * *",
        "every 2 hours": "0 */2 * * *", "every 3 hours": "0 */3 * * *",
        "every 6 hours": "0 */6 * * *", "every 12 hours": "0 */12 * * *",
        "every day": "0 0 * * *", "daily": "0 0 * * *",
        "every weekday": "0 0 * * 1-5", "every week": "0 0 * * 0",
        "weekly": "0 0 * * 0", "every month": "0 0 1 * *", "monthly": "0 0 1 * *",
    }
    if t in mappings:
        return mappings[t]
    import re
    m = re.search(r"every (\d+) minutes?", t)
    if m: return f"*/{m.group(1)} * * * *"
    m = re.search(r"every (\d+) hours?", t)
    if m: return f"0 */{m.group(1)} * * *"
    m = re.search(r"every (\d+) days?", t)
    if m: return f"0 0 */{m.group(1)} * *"
    m = re.search(r"(?:every )?day at (\d+)(?::(\d+))?\s*([ap]m)?|at (\d+)(?::(\d+))?\s*([ap]m)?", t)
    if m:
        hour = int(m.group(1) or m.group(4))
        minute = int(m.group(2) or m.group(5) or 0)
        ampm = (m.group(3) or m.group(6) or "").lower()
        if ampm == "pm" and hour < 12: hour += 12
        if ampm == "am" and hour == 12: hour = 0
        return f"{minute} {hour} * * *"
    return t


def _tool_create_schedule_adv(args: dict) -> dict:
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


def _tool_list_schedules_adv(args: dict) -> dict:
    try:
        from services.webhooks import schedule_list
        rows = schedule_list()
        schedules = [{"id": r["id"], "cron": r["cron_expr"], "agent": r["agent"],
                      "action": r["action"], "description": r.get("description", ""),
                      "enabled": r.get("enabled", 1), "next_run": r.get("next_run", "")}
                     for r in rows]
        return {"count": len(schedules), "schedules": schedules}
    except Exception as e:
        return {"error": str(e), "schedules": []}


def _tool_remove_schedule_adv(args: dict) -> dict:
    sched_id = args.get("schedule_id", "")
    if not sched_id:
        return {"error": "schedule_id is required"}
    try:
        from services.webhooks import schedule_remove
        removed = schedule_remove(sched_id)
        return {"status": "removed" if removed else "not_found", "id": sched_id}
    except Exception as e:
        return {"error": str(e)}


def _check_path(path: str, operation: str = "read") -> bool:
    """Validate path against allowed directories."""
    try:
        resolved = Path(path).resolve()
        paths = ALLOWED_READ_PATHS if operation == "read" else ALLOWED_WRITE_PATHS
        return any(str(resolved).startswith(allowed) for allowed in paths)
    except Exception:
        return False
