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
        "description": "Multi-agent task delegation",
        "tools": ["delegate_to_agent"],
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
    "full": {
        "description": "All available tools",
        "includes": ["core", "network", "delegation", "coding", "design"],
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
        elif name == "opencode":
            result = await _tool_opencode(arguments)
        elif name == "opendesign":
            result = await _tool_opendesign(arguments)
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


def _check_path(path: str, operation: str = "read") -> bool:
    """Validate path against allowed directories."""
    try:
        resolved = Path(path).resolve()
        paths = ALLOWED_READ_PATHS if operation == "read" else ALLOWED_WRITE_PATHS
        return any(str(resolved).startswith(allowed) for allowed in paths)
    except Exception:
        return False
