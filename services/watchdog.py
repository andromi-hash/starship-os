#!/usr/bin/env python3
"""
Starship OS Watchdog Service

Health check and auto-restart watchdog for all Starship OS services.

Usage:
    python3 watchdog.py           # Run as daemon
    python3 watchdog.py status    # Print service status
    python3 watchdog.py check     # One-shot health check
"""

import sys
import os
import json
import time
import signal
import asyncio
import subprocess
import socket
import logging
import logging.handlers
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONFIG_PATH = Path("/etc/agnetic/watchdog.yaml")
_log_dir = Path("/var/log/agnetic")
if not os.access(_log_dir, os.W_OK):
    _log_dir = Path("/tmp/agnetic-data/logs")
_log_dir.mkdir(parents=True, exist_ok=True)
LOG_DIR = _log_dir
LOG_FILE = LOG_DIR / "watchdog.log"
NATS_URL = os.getenv("NATS_URL", "nats://127.0.0.1:4222")
NATS_STATUS_SUBJECT = "agnetic.watchdog.status"

BACKOFF_SEQUENCE = [0, 10, 30, 60]
BACKOFF_RESET_SECONDS = 300  # 5 minutes

DEFAULT_SERVICES = {
    "nats": {
        "check": ["process", "port:4222"],
        "command": "nats-server -c /etc/agnetic/nats/agent-bus.conf",
        "process_name": "nats-server",
    },
    "staragent": {
        "check": ["process"],
        "command": "staragent",
        "process_name": "staragent",
    },
    "proxy": {
        "check": ["process"],
        "command": "python3 agents/agent_daemon.py proxy",
        "process_name": "agent_daemon.py proxy",
    },
    "romi": {
        "check": ["process"],
        "command": "python3 agents/agent_daemon.py romi",
        "process_name": "agent_daemon.py romi",
    },
    "ergo": {
        "check": ["process"],
        "command": "python3 agents/agent_daemon.py ergo",
        "process_name": "agent_daemon.py ergo",
    },
    "dashboard": {
        "check": ["port:8788", "http:http://127.0.0.1:8788/api/health"],
        "command": "python3 dashboard/server.py",
        "process_name": "dashboard",
    },
    "status-bridge": {
        "check": ["process"],
        "command": "python3 tray/agnetic-status.py",
        "process_name": "agnetic-status.py",
    },
    "message-history": {
        "check": ["process"],
        "command": "python3 scripts/message_history.py",
        "process_name": "message_history.py",
    },
}

DEFAULT_CHECK_INTERVAL = 30

# ---------------------------------------------------------------------------
# Structured Logger
# ---------------------------------------------------------------------------


class JSONFormatter(logging.Formatter):
    """Emit log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "service": getattr(record, "service", "watchdog"),
            "event": getattr(record, "event", record.getMessage()),
        }
        details = getattr(record, "details", None)
        if details:
            entry["details"] = details
        return json.dumps(entry, default=str)


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("watchdog")
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
        # Non-root or missing directory — fall through to stderr only
        pass

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    return logger


log = setup_logging()


def _log(event: str, service: str = "watchdog", level: str = "info", details: dict | None = None):
    extra: dict[str, Any] = {"service": service, "event": event}
    if details:
        extra["details"] = details
    getattr(log, level, log.info)(event, extra=extra)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def load_config() -> dict:
    if CONFIG_PATH.exists() and yaml is not None:
        try:
            with open(CONFIG_PATH) as f:
                cfg = yaml.safe_load(f) or {}
            _log("config_loaded", details={"path": str(CONFIG_PATH)})
            merged = dict(DEFAULT_SERVICES)
            for name, svc_cfg in cfg.get("services", {}).items():
                merged[name] = {**DEFAULT_SERVICES.get(name, {}), **svc_cfg}
            return {
                "check_interval": cfg.get("check_interval", DEFAULT_CHECK_INTERVAL),
                "services": merged,
            }
        except Exception as exc:
            _log("config_load_failed", level="warning", details={"error": str(exc)})

    return {
        "check_interval": DEFAULT_CHECK_INTERVAL,
        "services": dict(DEFAULT_SERVICES),
    }


# ---------------------------------------------------------------------------
# Health Check Methods
# ---------------------------------------------------------------------------


def check_process(process_name: str) -> bool:
    """Return True if a process matching *process_name* is alive."""
    try:
        out = subprocess.run(
            ["pgrep", "-f", process_name],
            capture_output=True, timeout=10,
        )
        return out.returncode == 0
    except FileNotFoundError:
        # pgrep not available — fall back to /proc scan
        return _fallback_process_check(process_name)
    except subprocess.TimeoutExpired:
        return False


def _fallback_process_check(name: str) -> bool:
    """Minimal /proc-based fallback when pgrep is absent."""
    try:
        for pid_dir in Path("/proc").iterdir():
            if not pid_dir.name.isdigit():
                continue
            cmdline_file = pid_dir / "cmdline"
            if cmdline_file.exists():
                try:
                    cmdline = cmdline_file.read_text(errors="ignore")
                    if name in cmdline:
                        return True
                except (PermissionError, OSError):
                    continue
    except FileNotFoundError:
        pass
    return False


def check_port(port: int) -> bool:
    """Return True if *port* is listening on localhost."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5):
            return True
    except (ConnectionRefusedError, OSError, TimeoutError):
        return False


def check_http(url: str, timeout: int = 10) -> bool:
    """Return True if *url* responds with HTTP 2xx."""
    try:
        out = subprocess.run(
            ["curl", "-sf", "--max-time", str(timeout), url],
            capture_output=True, timeout=timeout + 5,
        )
        return out.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def check_nats(nats_url: str = NATS_URL, timeout: float = 5) -> bool:
    """Return True if we can connect to NATS and get a PONG."""
    try:
        import nats as nats_mod

        async def _ping():
            nc = await asyncio.wait_for(nats_mod.connect(nats_url), timeout=timeout)
            ok = nc.is_connected
            await nc.close()
            return ok

        return asyncio.get_event_loop().run_until_complete(_ping())
    except Exception:
        return False


CHECK_DISPATCH: dict[str, Any] = {
    "process": check_process,
    "port": check_port,
    "http": check_http,
    "nats": check_nats,
}


def run_check(check_spec: str, svc_name: str, svc_cfg: dict) -> tuple[str, bool]:
    """Parse a check specifier string and execute the right method.

    Returns (check_label, is_healthy).
    """
    if check_spec == "process":
        proc_name = svc_cfg.get("process_name", svc_name)
        return "process", check_process(proc_name)

    if check_spec.startswith("port:"):
        port = int(check_spec.split(":", 1)[1])
        return check_spec, check_port(port)

    if check_spec.startswith("http:"):
        url = check_spec.split(":", 1)[1]
        return check_spec, check_http(url)

    if check_spec == "nats":
        return "nats", check_nats()

    _log("unknown_check", service=svc_name, level="warning", details={"check": check_spec})
    return check_spec, False


# ---------------------------------------------------------------------------
# Service Restart
# ---------------------------------------------------------------------------


def restart_service(svc_name: str, svc_cfg: dict) -> bool:
    """Attempt to restart a service via its command. Returns True on success."""
    cmd = svc_cfg.get("command")
    if not cmd:
        _log("no_restart_command", service=svc_name, level="warning")
        return False

    _log("restarting", service=svc_name, details={"command": cmd})
    try:
        subprocess.Popen(
            cmd,
            shell=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except Exception as exc:
        _log("restart_failed", service=svc_name, level="error", details={"error": str(exc)})
        return False


# ---------------------------------------------------------------------------
# Service State
# ---------------------------------------------------------------------------


class ServiceState:
    """Tracks health history and backoff for a single service."""

    __slots__ = ("name", "healthy", "consecutive_failures", "last_healthy_ts", "last_restart_ts")

    def __init__(self, name: str):
        self.name = name
        self.healthy = False
        self.consecutive_failures = 0
        self.last_healthy_ts: float = 0.0
        self.last_restart_ts: float = 0.0

    def record_healthy(self):
        self.healthy = True
        self.consecutive_failures = 0
        self.last_healthy_ts = time.time()

    def record_failure(self):
        self.healthy = False
        self.consecutive_failures += 1

    @property
    def backoff_seconds(self) -> float:
        if self.consecutive_failures <= 0:
            return 0
        idx = min(self.consecutive_failures - 1, len(BACKOFF_SEQUENCE) - 1)
        return float(BACKOFF_SEQUENCE[idx])

    @property
    def should_restart_now(self) -> bool:
        if self.consecutive_failures == 0:
            return False
        if self.last_restart_ts == 0:
            return True
        elapsed = time.time() - self.last_restart_ts
        return elapsed >= self.backoff_seconds

    def mark_restarted(self):
        self.last_restart_ts = time.time()


# ---------------------------------------------------------------------------
# NATS Status Publisher
# ---------------------------------------------------------------------------


class NATSPublisher:
    """Publishes watchdog status to NATS (fire-and-forget)."""

    def __init__(self, nats_url: str = NATS_URL):
        self._nats_url = nats_url
        self._nc = None
        self._js = None
        self._connected = False

    async def connect(self):
        try:
            import nats as nats_mod
            self._nc = await nats_mod.connect(self._nats_url)
            self._js = self._nc.jetstream()
            self._connected = True
            _log("nats_connected", details={"url": self._nats_url})
        except Exception as exc:
            _log("nats_connect_failed", level="warning", details={"error": str(exc)})
            self._connected = False

    async def publish(self, payload: dict):
        if not self._connected or self._nc is None:
            return
        try:
            data = json.dumps(payload, default=str).encode()
            await self._js.publish(NATS_STATUS_SUBJECT, data)
        except Exception as exc:
            _log("nats_publish_failed", level="warning", details={"error": str(exc)})
            # Attempt reconnect on next cycle
            self._connected = False

    async def close(self):
        if self._nc:
            try:
                await self._nc.close()
            except Exception:
                pass
        self._connected = False


# ---------------------------------------------------------------------------
# Core Watchdog Loop
# ---------------------------------------------------------------------------


class Watchdog:
    def __init__(self, config: dict):
        self._interval = config["check_interval"]
        self._services = config["services"]
        self._states: dict[str, ServiceState] = {
            name: ServiceState(name) for name in self._services
        }
        self._publisher = NATSPublisher()
        self._running = True

    # -- health checks ------------------------------------------------------

    def _check_all(self) -> dict[str, dict]:
        results: dict[str, dict] = {}
        for svc_name, svc_cfg in self._services.items():
            checks = svc_cfg.get("check", ["process"])
            details_list: list[dict] = []
            all_ok = True
            for check_spec in checks:
                label, ok = run_check(check_spec, svc_name, svc_cfg)
                details_list.append({"check": label, "healthy": ok})
                if not ok:
                    all_ok = False
            results[svc_name] = {
                "healthy": all_ok,
                "checks": details_list,
            }
        return results

    # -- restart decisions ---------------------------------------------------

    def _maybe_restart(self, results: dict[str, dict]):
        for svc_name, result in results.items():
            state = self._states[svc_name]
            if result["healthy"]:
                state.record_healthy()
                continue

            state.record_failure()

            # Respect backoff reset: if it was healthy long enough, reset failures
            if state.last_healthy_ts and (time.time() - state.last_healthy_ts) >= BACKOFF_RESET_SECONDS:
                state.consecutive_failures = 1  # start fresh with immediate restart

            if state.should_restart_now:
                svc_cfg = self._services[svc_name]
                ok = restart_service(svc_name, svc_cfg)
                if ok:
                    state.mark_restarted()
                    _log("restart_triggered", service=svc_name, details={
                        "attempt": state.consecutive_failures,
                        "backoff": state.backoff_seconds,
                    })

    # -- publish status ------------------------------------------------------

    def _build_status_payload(self, results: dict[str, dict]) -> dict:
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "services": {
                name: {
                    "healthy": results[name]["healthy"],
                    "consecutive_failures": self._states[name].consecutive_failures,
                    "backoff_seconds": self._states[name].backoff_seconds,
                    "checks": results[name]["checks"],
                }
                for name in results
            },
        }

    async def _publish_status(self, results: dict[str, dict]):
        payload = self._build_status_payload(results)
        await self._publisher.publish(payload)

    # -- run loop ------------------------------------------------------------

    async def run(self):
        _log("watchdog_starting", details={"interval": self._interval, "services": list(self._services)})
        await self._publisher.connect()

        while self._running:
            try:
                results = self._check_all()
                self._maybe_restart(results)
                await self._publish_status(results)

                for svc_name, result in results.items():
                    level = "info" if result["healthy"] else "warning"
                    _log("health_check", service=svc_name, level=level, details=result)

            except Exception as exc:
                _log("check_cycle_error", level="error", details={"error": str(exc)})

            await asyncio.sleep(self._interval)

        await self._publisher.close()
        _log("watchdog_stopped")

    def stop(self):
        self._running = False


# ---------------------------------------------------------------------------
# CLI: status
# ---------------------------------------------------------------------------


def cmd_status(config: dict):
    """One-shot check: print table of service health."""
    results: dict[str, dict] = {}
    for svc_name, svc_cfg in config["services"].items():
        checks = svc_cfg.get("check", ["process"])
        details_list: list[dict] = []
        all_ok = True
        for check_spec in checks:
            label, ok = run_check(check_spec, svc_name, svc_cfg)
            details_list.append({"check": label, "healthy": ok})
            if not ok:
                all_ok = False
        results[svc_name] = {"healthy": all_ok, "checks": details_list}

    # Print human-readable table
    print(f"\n{'Service':<20} {'Status':<10} {'Checks'}")
    print("-" * 60)
    for svc_name, result in results.items():
        status = "OK" if result["healthy"] else "FAIL"
        checks_str = ", ".join(
            f"{c['check']}={'ok' if c['healthy'] else 'fail'}" for c in result["checks"]
        )
        print(f"{svc_name:<20} {status:<10} {checks_str}")
    print()

    # Also output JSON to stdout for machine consumption
    print(json.dumps(results, indent=2, default=str))

    all_healthy = all(r["healthy"] for r in results.values())
    sys.exit(0 if all_healthy else 1)


# ---------------------------------------------------------------------------
# CLI: check  (one-shot, JSON only)
# ---------------------------------------------------------------------------


def cmd_check(config: dict):
    results: dict[str, dict] = {}
    for svc_name, svc_cfg in config["services"].items():
        checks = svc_cfg.get("check", ["process"])
        details_list: list[dict] = []
        all_ok = True
        for check_spec in checks:
            label, ok = run_check(check_spec, svc_name, svc_cfg)
            details_list.append({"check": label, "healthy": ok})
            if not ok:
                all_ok = False
        results[svc_name] = {"healthy": all_ok, "checks": details_list}

    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "services": {
            name: {
                "healthy": r["healthy"],
                "checks": r["checks"],
            }
            for name, r in results.items()
        },
    }
    print(json.dumps(payload, indent=2, default=str))
    all_healthy = all(r["healthy"] for r in results.values())
    sys.exit(0 if all_healthy else 1)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    config = load_config()
    mode = sys.argv[1] if len(sys.argv) > 1 else "daemon"

    if mode == "status":
        cmd_status(config)
        return

    if mode == "check":
        cmd_check(config)
        return

    if mode not in ("daemon", "--daemon", "-d"):
        print(f"Usage: {sys.argv[0]} [daemon|status|check]")
        sys.exit(1)

    watchdog = Watchdog(config)

    def _shutdown(signum, _frame):
        _log("signal_received", details={"signal": signum})
        watchdog.stop()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        asyncio.run(watchdog.run())
    except KeyboardInterrupt:
        _log("keyboard_interrupt")


if __name__ == "__main__":
    main()
