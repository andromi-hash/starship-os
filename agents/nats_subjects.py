"""NATS subject helpers — dual-publish starship.* + agnetic.* (Alpha 2.1).

Primary prefix: starship
Legacy prefix:  agnetic (compat with Alpha 2.0 mesh clients)
"""

from __future__ import annotations

import os
from typing import Iterable, List

PRIMARY = os.getenv("STARSHIP_NATS_PREFIX", "starship")
LEGACY = os.getenv("STARSHIP_NATS_LEGACY_PREFIX", "agnetic")
PREFIXES: tuple[str, ...] = (PRIMARY, LEGACY)


def rest_of(subject: str) -> str:
    """Strip known prefix from a subject, return remainder."""
    for p in PREFIXES:
        if subject == p:
            return ""
        if subject.startswith(p + "."):
            return subject[len(p) + 1 :]
    return subject


def dual(subject: str) -> List[str]:
    """Return [starship.*, agnetic.*] for any subject under either prefix."""
    rest = rest_of(subject)
    if not rest:
        return list(PREFIXES)
    return [f"{p}.{rest}" for p in PREFIXES]


def primary(subject: str) -> str:
    """Canonical starship.* form of a subject."""
    rest = rest_of(subject)
    return f"{PRIMARY}.{rest}" if rest else PRIMARY


def agent_command(name: str, command: str = ">") -> str:
    return f"{PRIMARY}.agent.{name}.command.{command}"


def agent_status(name: str) -> str:
    return f"{PRIMARY}.agent.{name}.status"


def agent_event(name: str, event: str = ">") -> str:
    return f"{PRIMARY}.agent.{name}.event.{event}"


def telemetry(metric: str = ">") -> str:
    if metric in ("", ">", "*"):
        return f"{PRIMARY}.telemetry.>"
    return f"{PRIMARY}.telemetry.{metric}"


def workflow(name: str = ">") -> str:
    return f"{PRIMARY}.workflow.{name}"


def skill(name: str = ">") -> str:
    return f"{PRIMARY}.skill.{name}"


async def dual_publish(nc, subject: str, payload: bytes) -> None:
    """Publish payload to both starship.* and agnetic.* subjects."""
    for s in dual(subject):
        await nc.publish(s, payload)


async def dual_subscribe(nc, subject: str, cb=None):
    """Subscribe to both prefixes. Returns list of subscriptions."""
    subs = []
    for s in dual(subject):
        if cb is not None:
            subs.append(await nc.subscribe(s, cb=cb))
        else:
            subs.append(await nc.subscribe(s))
    return subs
