"""Optional C11 sandbox_run bridge (ADR 0001).

Enabled when STARSHIP_SANDBOX_NATIVE=1 and sandbox_run is on PATH or at
src/c/sandbox_spike/sandbox_run / /opt/starship/bin/sandbox_run.

Uses subprocess (not ctypes) for the spike binary; a .so bridge can replace
this later without changing call sites.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_WALL_RE = re.compile(r"wall_ms=([0-9.]+)")


@dataclass
class NativeResult:
    exit_code: int
    stdout: str
    stderr: str
    wall_ms: Optional[float] = None
    timed_out: bool = False
    denied: bool = False
    binary: str = ""


def sandbox_binary() -> Optional[str]:
    env = os.getenv("STARSHIP_SANDBOX_RUN", "").strip()
    if env and Path(env).is_file() and os.access(env, os.X_OK):
        return env
    which = shutil.which("sandbox_run")
    if which:
        return which
    roots = [
        Path(os.getenv("STARSHIP_ROOT", "/opt/starship")) / "bin" / "sandbox_run",
        Path(__file__).resolve().parent.parent / "src" / "c" / "sandbox_spike" / "sandbox_run",
    ]
    for p in roots:
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return None


def native_enabled() -> bool:
    flag = os.getenv("STARSHIP_SANDBOX_NATIVE", "").strip().lower()
    if flag not in ("1", "true", "yes", "on"):
        return False
    return sandbox_binary() is not None


def run_native(
    argv: list[str],
    *,
    timeout: int = 30,
    cwd: Optional[str] = None,
) -> NativeResult:
    """Run argv under sandbox_run. argv[0] should be absolute path when possible."""
    binary = sandbox_binary()
    if not binary:
        raise FileNotFoundError("sandbox_run not found")
    if not argv:
        raise ValueError("empty argv")

    cmd = [binary, "--timeout", str(int(timeout)), "--", *argv]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout + 2,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired as e:
        return NativeResult(
            exit_code=124,
            stdout=(e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or ""),
            stderr=(e.stderr or b"").decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or ""),
            timed_out=True,
            binary=binary,
        )

    stderr = proc.stderr or ""
    wall = None
    m = _WALL_RE.search(stderr)
    if m:
        wall = float(m.group(1))
        # strip timing line from stderr presented to tools
        stderr = _WALL_RE.sub("", stderr).replace("sandbox: wall_ms=\n", "").strip()

    denied = proc.returncode == 126 or "sandbox: denied" in (proc.stderr or "")
    return NativeResult(
        exit_code=proc.returncode,
        stdout=proc.stdout or "",
        stderr=stderr,
        wall_ms=wall,
        timed_out=proc.returncode == 124,
        denied=denied,
        binary=binary,
    )


def run_shell_native(command: str, *, timeout: int = 30) -> NativeResult:
    """Best-effort shell command via /bin/sh -c under sandbox_run."""
    return run_native(["/bin/sh", "-c", command], timeout=timeout)
