"""Optional C11 policyexec bridge — shared policy JSON with Python.

Enable: STARSHIP_POLICY_NATIVE=1
Binary: STARSHIP_POLICYEXEC or PATH / /opt/starship/bin/policyexec
Policy: STARSHIP_POLICY or /etc/starship/policy.json or config/policy.default.json
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional


def policyexec_binary() -> Optional[str]:
    env = os.getenv("STARSHIP_POLICYEXEC", "").strip()
    if env and Path(env).is_file() and os.access(env, os.X_OK):
        return env
    which = shutil.which("policyexec")
    if which:
        return which
    roots = [
        Path(os.getenv("STARSHIP_ROOT", "/opt/starship")) / "bin" / "policyexec",
        Path(__file__).resolve().parent.parent / "src" / "c" / "policyexec" / "policyexec",
    ]
    for p in roots:
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return None


def native_enabled() -> bool:
    flag = os.getenv("STARSHIP_POLICY_NATIVE", "").strip().lower()
    if flag not in ("1", "true", "yes", "on"):
        return False
    return policyexec_binary() is not None


def policy_path() -> Optional[str]:
    e = os.getenv("STARSHIP_POLICY", "").strip()
    if e and Path(e).is_file():
        return e
    for p in (
        Path("/etc/starship/policy.json"),
        Path(__file__).resolve().parent.parent / "config" / "policy.default.json",
    ):
        if p.is_file():
            return str(p)
    return None


def _run(args: list[str]) -> tuple[int, dict]:
    binary = policyexec_binary()
    if not binary:
        raise FileNotFoundError("policyexec not found")
    cmd = [binary]
    pol = policy_path()
    if pol:
        cmd += ["--policy", pol]
    role = os.getenv("STARSHIP_FLEET_ROLES", "").split(",")[0].strip()
    if not role:
        role = os.getenv("STARSHIP_FLEET_TEAM", "").strip()
    if role:
        cmd += ["--role", role]
    cmd += args
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    data: dict = {}
    out = (proc.stdout or "").strip()
    if out.startswith("{"):
        try:
            data = json.loads(out.splitlines()[0])
        except json.JSONDecodeError:
            data = {"raw": out}
    data["exit_code"] = proc.returncode
    data["stderr"] = (proc.stderr or "").strip()
    return proc.returncode, data


def check_tool(name: str) -> Optional[str]:
    """Return denial reason or None if allowed."""
    code, data = _run(["check-tool", name])
    if code == 0:
        return None
    return data.get("reason") or f"policyexec denied tool '{name}'"


def check_command(command: str) -> Optional[str]:
    """Return denial reason or None if allowed. Uses first token as program."""
    prog = command.strip().split()[0] if command.strip() else ""
    if not prog:
        return "empty command"
    code, data = _run(["check-command", prog])
    if code == 0:
        return None
    return data.get("reason") or f"policyexec denied command '{prog}'"
