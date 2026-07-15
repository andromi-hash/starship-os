"""Starship OS Self-Healing System — Autonomous recovery for agents and services.

Monitors agent health, detects stalls/failures, and triggers recovery actions.
Pattern: Kubernetes-style liveness + readiness probes adapted for agent mesh."""

import json
import logging
import os
import signal
import subprocess
import time
import asyncio
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger("agnetic-healer")

HEALER_STATE = Path("/var/lib/agnetic/healer_state.json")


@dataclass
class HealthStatus:
    agent: str
    status: str
    last_seen: str
    response_time_ms: float
    memory_usage_mb: float
    error_count: int


@dataclass
class RecoveryAction:
    action: str
    target: str
    reason: str
    timestamp: str
    success: bool
    result: str


class SelfHealer:
    """Autonomous system health monitor with recovery capabilities."""

    def __init__(self):
        self._agents: dict[str, HealthStatus] = {}
        self._recovery_history: list[RecoveryAction] = []
        self._running = False
        self._load_state()

    def _load_state(self):
        if HEALER_STATE.exists():
            try:
                data = json.loads(HEALER_STATE.read_text())
                for agent, status in data.get("agents", {}).items():
                    if isinstance(status, dict):
                        self._agents[agent] = HealthStatus(**status)
                for action in data.get("history", []):
                    if isinstance(action, dict):
                        self._recovery_history.append(RecoveryAction(**action))
                log.info("Healer loaded state: %d agents, %d past recoveries",
                         len(self._agents), len(self._recovery_history))
            except Exception as e:
                log.warning("Failed to load healer state: %s", e)

    def _save_state(self):
        try:
            HEALER_STATE.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "agents": {name: asdict(status) for name, status in self._agents.items()},
                "history": [asdict(a) for a in self._recovery_history[-100:]],
                "updated": datetime.now(timezone.utc).isoformat(),
            }
            HEALER_STATE.write_text(json.dumps(data, indent=2))
        except Exception as e:
            log.warning("Failed to save healer state: %s", e)

    def report_health(self, agent: str, status: str, response_time_ms: float,
                      memory_usage_mb: float = 0) -> HealthStatus:
        old = self._agents.get(agent)
        error_count = (old.error_count + 1) if old and status in ("stalled", "error") else 0
        hs = HealthStatus(
            agent=agent,
            status=status,
            last_seen=datetime.now(timezone.utc).isoformat(),
            response_time_ms=response_time_ms,
            memory_usage_mb=memory_usage_mb,
            error_count=error_count,
        )
        self._agents[agent] = hs
        self._save_state()

        if error_count >= 3:
            asyncio.create_task(self.recover(agent, f"{error_count} consecutive errors"))

        return hs

    async def recover(self, target: str, reason: str) -> RecoveryAction:
        action = RecoveryAction(
            action="restart",
            target=target,
            reason=reason,
            timestamp=datetime.now(timezone.utc).isoformat(),
            success=False,
            result="",
        )
        log.info("Healing: %s — %s", target, reason)

        if target in self._agents:
            del self._agents[target]

        try:
            if os.geteuid() == 0:
                result = subprocess.run(
                    ["systemctl", "restart", target],
                    capture_output=True, text=True, timeout=30
                )
                action.success = result.returncode == 0
                action.result = result.stdout.strip() or result.stderr.strip() or "restarted"
            else:
                action.success = True
                action.result = "manual restart needed (not root)"
                warn(f"Cannot restart {target}: not running as root")
        except subprocess.TimeoutExpired:
            action.result = "restart timed out after 30s"
        except FileNotFoundError:
            action.result = f"systemctl not available; kill + restart {target} manually"
            action.success = True

        self._recovery_history.append(action)
        self._save_state()
        return action

    def get_agent_status(self, agent: str) -> Optional[HealthStatus]:
        return self._agents.get(agent)

    def list_agents(self) -> list[HealthStatus]:
        return list(self._agents.values())

    def get_recovery_history(self, limit: int = 20) -> list[RecoveryAction]:
        return self._recovery_history[-limit:]

    async def check_all(self):
        stale_timeout = 300
        now = time.time()
        for agent, hs in list(self._agents.items()):
            try:
                last = datetime.fromisoformat(hs.last_seen).timestamp()
                if now - last > stale_timeout:
                    await self.recover(agent, f"not seen in {now - last:.0f}s")
            except Exception:
                pass

    def detect_stall(self, agent: str, timeout_seconds: int = 120) -> bool:
        hs = self._agents.get(agent)
        if not hs:
            return False
        try:
            last = datetime.fromisoformat(hs.last_seen).timestamp()
            return (time.time() - last) > timeout_seconds
        except Exception:
            return False

    def summary(self) -> dict:
        agents_alive = sum(1 for a in self._agents.values() if a.status == "alive")
        agents_stalled = sum(1 for a in self._agents.values() if a.status in ("stalled", "error"))
        return {
            "total_agents": len(self._agents),
            "alive": agents_alive,
            "stalled_or_error": agents_stalled,
            "recoveries_performed": len(self._recovery_history),
            "last_recovery": self._recovery_history[-1].timestamp if self._recovery_history else None,
        }


_healer = SelfHealer()


def get_healer() -> SelfHealer:
    return _healer


def check_and_report(agent: str, status: str = "alive", response_time_ms: float = 0) -> HealthStatus:
    return _healer.report_health(agent, status, response_time_ms)
