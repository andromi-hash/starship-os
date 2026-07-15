"""Fleet role / red-blue / cross-plant policy enforcement for tool calls.

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

# Tools that can leave the local plant (subject to ACL)
CROSS_PLANT_TOOLS = frozenset({
    "delegate_to_agent",
    "http_post",
    "http_get",
    "shell",
})

_fleet_cfg_cache: Optional[dict] = None


def _load_yaml(path: Path) -> dict:
    if yaml is None or not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text()) or {}
    except Exception:
        return {}


def _load_node_override() -> dict:
    paths = [
        Path("/etc/starship/fleet-node.yaml"),
        Path(os.getenv("STARSHIP_ROOT", "/opt/starship")) / "etc" / "fleet-node.yaml",
    ]
    for p in paths:
        data = _load_yaml(p)
        if data:
            return data
    return {}


def load_fleet_config() -> dict:
    """Load fleet topology + ACL (cached)."""
    global _fleet_cfg_cache
    if _fleet_cfg_cache is not None:
        return _fleet_cfg_cache
    paths = [
        Path("/etc/starship/fleet.yaml"),
        Path(os.getenv("STARSHIP_ROOT", "/opt/starship")) / "config" / "fleet.yaml",
        Path(__file__).resolve().parent.parent / "config" / "fleet.yaml",
    ]
    cfg: dict = {}
    for p in paths:
        data = _load_yaml(p)
        if data:
            cfg = data
            break
    _fleet_cfg_cache = cfg
    return cfg


def clear_cache() -> None:
    """Test helper — drop cached fleet config."""
    global _fleet_cfg_cache
    _fleet_cfg_cache = None


def current_context() -> dict:
    """Resolve fleet team/roles/plant for this process."""
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


def plant_isolated(plant_id: str) -> bool:
    plants = load_fleet_config().get("plants") or {}
    p = plants.get(plant_id) or {}
    return bool(p.get("isolation"))


def check_cross_plant(
    source_plant: str,
    target_plant: Optional[str],
    ctx: Optional[dict] = None,
) -> Optional[str]:
    """Return denial reason if source→target plant traffic is blocked.

    Rules (first match wins):
    1. Same plant (or no target) → allow
    2. Red-team during exercise → deny all cross-plant
    3. Source plant isolation: true → deny outbound
    4. Target plant isolation: true → deny inbound from other plants
    5. ACL allow matrix in fleet.yaml (if present)
    6. Default: deny cross-plant (fail closed)
    """
    if not target_plant or target_plant == source_plant:
        return None

    ctx = ctx or current_context()
    team = (ctx.get("team") or "ops").lower()
    roles = [r.lower() for r in (ctx.get("roles") or [])]
    is_red = team == "red" or "red-team" in roles

    if is_red and exercise_active():
        return (
            f"policy: red-team cross-plant denied during exercise "
            f"({source_plant} → {target_plant})"
        )

    if plant_isolated(source_plant):
        return (
            f"policy: plant '{source_plant}' is isolated — "
            f"outbound to '{target_plant}' denied"
        )

    if plant_isolated(target_plant):
        return (
            f"policy: plant '{target_plant}' is isolated — "
            f"inbound from '{source_plant}' denied"
        )

    cfg = load_fleet_config()
    acl = cfg.get("acl") or {}
    allow = acl.get("allow") or {}

    # Explicit allow list for source plant
    if source_plant in allow:
        permitted = allow[source_plant]
        if target_plant in (permitted or []):
            return None
        return (
            f"policy: cross-plant ACL denied {source_plant} → {target_plant} "
            f"(allowed: {sorted(permitted or [])})"
        )

    # Global default
    default = (acl.get("default") or "deny").lower()
    if default in ("allow", "permit", "open"):
        return None
    if default in ("same_plant_only", "deny", "closed"):
        return (
            f"policy: cross-plant denied {source_plant} → {target_plant} "
            f"(default={default})"
        )

    return f"policy: cross-plant denied {source_plant} → {target_plant}"


def check_tool(
    name: str,
    ctx: Optional[dict] = None,
    *,
    target_plant: Optional[str] = None,
    arguments: Optional[dict] = None,
) -> Optional[str]:
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

    # Resolve target plant from args when not explicit
    args = arguments or {}
    if target_plant is None:
        target_plant = args.get("plant") or args.get("target_plant")
        if not target_plant and isinstance(args.get("args"), dict):
            target_plant = args["args"].get("plant") or args["args"].get("target_plant")

    # Cross-plant ACL for tools that can leave the plant
    if name in CROSS_PLANT_TOOLS and target_plant:
        denial = check_cross_plant(plant, target_plant, ctx)
        if denial:
            return denial

    # Red-team cannot use outbound tools from non-range during exercise
    # even without an explicit target (fail closed on traffic tools)
    if is_red and exercise_active() and plant not in ("plant-range", "range"):
        if name in ("delegate_to_agent", "http_post", "shell"):
            return "policy: red-team cross-plant traffic denied during exercise"

    # Isolated plants: block outbound tools without same-plant target
    if plant_isolated(plant) and name in ("delegate_to_agent", "http_post"):
        if target_plant and target_plant != plant:
            return check_cross_plant(plant, target_plant, ctx)
        # delegate without plant tag still allowed within range mesh

    return None


def filter_toolset(tool_names: list[str], ctx: Optional[dict] = None) -> list[str]:
    """Filter a list of tool names by fleet policy."""
    return [t for t in tool_names if check_tool(t, ctx) is None]


def enforce_or_raise(name: str, ctx: Optional[dict] = None, **kwargs) -> None:
    reason = check_tool(name, ctx, **kwargs)
    if reason:
        raise PermissionError(reason)
