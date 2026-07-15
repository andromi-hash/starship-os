"""Fleet role / red-blue policy enforcement for tool calls.

Loaded by tools.execute_tool and agent_daemon when STARSHIP_FLEET_TEAM
or STARSHIP_FLEET_ROLES is set (or /etc/starship/fleet-node.yaml).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

try:
    import yaml
except ImportError:
    yaml = None

# Red-team: diagnostics only — no OpenCode, no write/shell by default
RED_TEAM_ALLOWED = frozenset({
    "read_file",
    "list_dir",
    "search_files",
    "http_get",
    "delegate_to_agent",
})
RED_TEAM_DENIED = frozenset({
    "opencode",
    "opendesign",
    "write_file",
    "shell",
    "http_post",
})

BLUE_TEAM_DENIED = frozenset({
    "opencode",  # blue may use diagnostics; OpenCode unrestricted still blocked on range
})

# During active exercise, extra isolation
EXERCISE_CROSS_PLANT_DENY = True


def _load_node_override() -> dict:
    paths = [
        Path("/etc/starship/fleet-node.yaml"),
        Path(os.getenv("STARSHIP_ROOT", "/opt/starship")) / "etc" / "fleet-node.yaml",
    ]
    if yaml is None:
        return {}
    for p in paths:
        if p.exists():
            try:
                return yaml.safe_load(p.read_text()) or {}
            except Exception:
                pass
    return {}


def current_context() -> dict:
    """Resolve fleet team/roles for this process."""
    ov = _load_node_override()
    node = ov.get("node", ov)
    team = os.getenv("STARSHIP_FLEET_TEAM") or node.get("team") or "ops"
    roles_env = os.getenv("STARSHIP_FLEET_ROLES", "")
    if roles_env:
        roles = [r.strip() for r in roles_env.split(",") if r.strip()]
    else:
        roles = list(node.get("roles") or [])
    plant = os.getenv("STARSHIP_FLEET_PLANT") or node.get("plant") or "plant-alpha"
    return {"team": team, "roles": roles, "plant": plant}


def exercise_active() -> bool:
    for p in (
        Path("/var/lib/starship/fleet-state.json"),
        Path("/tmp/starship-fleet/fleet-state.json"),
    ):
        if p.exists():
            try:
                import json
                data = json.loads(p.read_text())
                return bool(data.get("exercise", {}).get("active"))
            except Exception:
                pass
    return False


def check_tool(name: str, ctx: Optional[dict] = None) -> Optional[str]:
    """Return denial reason string, or None if allowed."""
    ctx = ctx or current_context()
    team = (ctx.get("team") or "ops").lower()
    roles = [r.lower() for r in (ctx.get("roles") or [])]
    plant = ctx.get("plant") or ""

    is_red = team == "red" or "red-team" in roles
    is_blue = team == "blue" or "blue-team" in roles

    if is_red:
        if name in RED_TEAM_DENIED or name not in RED_TEAM_ALLOWED:
            return (
                f"policy: red-team denied tool '{name}' "
                f"(allowed: {sorted(RED_TEAM_ALLOWED)}; never unrestricted OpenCode)"
            )

    if is_blue and name in BLUE_TEAM_DENIED and exercise_active():
        return f"policy: blue-team denied tool '{name}' during active exercise"

    # Red-team cannot leave range plant during exercise
    if is_red and exercise_active() and plant not in ("plant-range", "range"):
        if name in ("delegate_to_agent", "http_post", "shell"):
            return "policy: red-team cross-plant traffic denied during exercise"

    return None


def filter_toolset(tool_names: list[str], ctx: Optional[dict] = None) -> list[str]:
    """Filter a list of tool names by fleet policy."""
    return [t for t in tool_names if check_tool(t, ctx) is None]


def enforce_or_raise(name: str, ctx: Optional[dict] = None) -> None:
    reason = check_tool(name, ctx)
    if reason:
        raise PermissionError(reason)
