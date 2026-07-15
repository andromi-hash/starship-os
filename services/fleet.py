#!/usr/bin/env python3
"""
Starship OS Fleet Manager

Topology: fleet → plants → nodes (roles + red/blue teams)
Ops manager: aggregate status, plant listing, exercise mode.

Usage:
  python3 services/fleet.py status
  python3 services/fleet.py plants
  python3 services/fleet.py nodes
  python3 services/fleet.py register
  python3 services/fleet.py daemon
  python3 services/fleet.py exercise start|stop|status
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

try:
    import yaml
except ImportError:
    yaml = None

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT / "agents"))

try:
    from nats_subjects import dual, dual_publish, PRIMARY
except ImportError:
    PRIMARY = "starship"

    def dual(subject: str):
        rest = subject.split(".", 1)[-1] if "." in subject else subject
        if subject.startswith("starship.") or subject.startswith("agnetic."):
            rest = subject.split(".", 1)[1]
        return [f"starship.{rest}", f"agnetic.{rest}"]

    async def dual_publish(nc, subject, payload):
        for s in dual(subject):
            await nc.publish(s, payload)

def _nats_url() -> str:
    """Build NATS URL; inject user/pass or token from env when set."""
    try:
        from nats_connect import build_nats_url
        return build_nats_url()
    except ImportError:
        pass
    url = os.getenv("NATS_URL", "nats://127.0.0.1:4222")
    user = os.getenv("NATS_USER", "").strip()
    password = os.getenv("NATS_PASSWORD", "").strip()
    token = os.getenv("STARSHIP_NATS_TOKEN", "").strip()
    if "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    if "@" in rest:
        return url
    if user and password:
        return f"{scheme}://{user}:{password}@{rest}"
    if token:
        return f"{scheme}://:{token}@{rest}"
    return url


NATS_URL = _nats_url()
CONFIG_PATHS = [
    Path("/etc/starship/fleet.yaml"),
    Path("/etc/starship/fleet-node.yaml"),
    PROJECT_ROOT / "config" / "fleet.yaml",
]
AUTH_MAP = PROJECT_ROOT / "nats" / "fleet-auth.yaml"
STATE_DIR = Path(os.getenv("STARSHIP_STATE", "/var/lib/starship"))
if not os.access(STATE_DIR, os.W_OK):
    STATE_DIR = Path("/tmp/starship-fleet")
STATE_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = STATE_DIR / "fleet-state.json"

SUBJECT_REGISTER = f"{PRIMARY}.fleet.register"
SUBJECT_HEARTBEAT = f"{PRIMARY}.fleet.heartbeat"
SUBJECT_STATUS = f"{PRIMARY}.fleet.status"
SUBJECT_OPS = f"{PRIMARY}.fleet.ops.status"
SUBJECT_EXERCISE = f"{PRIMARY}.fleet.exercise"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_fleet_config() -> dict:
    cfg: dict = {}
    if yaml is None:
        return _default_config()
    for path in CONFIG_PATHS:
        if path.exists():
            try:
                data = yaml.safe_load(path.read_text()) or {}
                cfg = _deep_merge(cfg, data)
            except Exception as exc:
                print(f"warn: failed to load {path}: {exc}", file=sys.stderr)
    if not cfg:
        cfg = _default_config()
    return cfg


def _default_config() -> dict:
    return {
        "fleet": {"name": "starship-fleet", "ops_manager": {"enabled": True}},
        "plants": {
            "plant-alpha": {
                "name": "Alpha Plant",
                "profile": "server",
                "roles_allowed": ["proxy", "romi", "ergo", "ops"],
            }
        },
        "node": {"plant": "plant-alpha", "roles": ["proxy"], "team": "ops"},
        "red_blue": {"enabled": True, "default_plant": "plant-range"},
    }


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


@dataclass
class FleetNode:
    node_id: str
    hostname: str
    plant: str
    roles: list[str]
    team: str
    profile: str
    status: str = "online"
    last_seen: str = field(default_factory=_utcnow)
    capabilities: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def local_node_id() -> str:
    return os.getenv("STARSHIP_NODE_ID", socket.gethostname())


def detect_caps() -> dict:
    caps: dict[str, Any] = {
        "hostname": socket.gethostname(),
        "arch": os.uname().machine if hasattr(os, "uname") else "unknown",
    }
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    caps["memory_mb"] = int(line.split()[1]) // 1024
                    break
    except Exception:
        pass
    try:
        import subprocess

        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if r.returncode == 0 and r.stdout.strip():
            parts = [p.strip() for p in r.stdout.strip().split(",")]
            caps["gpu"] = {"type": "nvidia", "name": parts[0], "vram_mb": int(parts[1]) if len(parts) > 1 else 0}
    except Exception:
        caps["gpu"] = None
    return caps


def build_local_node(cfg: dict) -> FleetNode:
    node_cfg = cfg.get("node", {})
    profile = node_cfg.get("profile", "auto")
    if profile == "auto":
        pfile = Path("/etc/starship/profile.yaml")
        if pfile.exists():
            for line in pfile.read_text().splitlines():
                if line.startswith("profile:"):
                    profile = line.split(":", 1)[1].strip()
                    break
        else:
            profile = "server"
    return FleetNode(
        node_id=local_node_id(),
        hostname=socket.gethostname(),
        plant=node_cfg.get("plant", "plant-alpha"),
        roles=list(node_cfg.get("roles", ["proxy"])),
        team=node_cfg.get("team", "ops"),
        profile=profile,
        capabilities=detect_caps(),
    )


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"nodes": {}, "exercise": {"active": False, "plant": None}, "updated": _utcnow()}


def save_state(state: dict) -> None:
    state["updated"] = _utcnow()
    STATE_FILE.write_text(json.dumps(state, indent=2))


def cmd_status(cfg: dict) -> int:
    state = load_state()
    node = build_local_node(cfg)
    fleet_name = cfg.get("fleet", {}).get("name", "starship-fleet")
    plants = cfg.get("plants", {})
    print(f"Fleet:   {fleet_name}")
    print(f"Local:   {node.node_id}  plant={node.plant}  team={node.team}  roles={','.join(node.roles)}")
    print(f"Profile: {node.profile}")
    print(f"Plants:  {len(plants)}")
    for pid, p in plants.items():
        print(f"  - {pid}: {p.get('name', pid)} [{p.get('profile', '?')}]")
    nodes = state.get("nodes", {})
    print(f"Known nodes: {len(nodes)}")
    for nid, n in nodes.items():
        print(f"  - {nid}: plant={n.get('plant')} team={n.get('team')} status={n.get('status')}")
    ex = state.get("exercise", {})
    print(f"Exercise: {'ACTIVE' if ex.get('active') else 'idle'}" + (f" plant={ex.get('plant')}" if ex.get("active") else ""))
    print(f"State:   {STATE_FILE}")
    mode = os.getenv("STARSHIP_NATS_MODE", "")
    if os.getenv("NATS_USER") or mode == "accounts":
        auth = f"accounts/{os.getenv('STARSHIP_NATS_ROLE', os.getenv('NATS_USER', '?'))}"
    elif os.getenv("STARSHIP_NATS_TOKEN", "").strip():
        auth = "token"
    elif os.getenv("STARSHIP_NATS_NKEY_SEED") or os.getenv("STARSHIP_NATS_NKEY_SEED_FILE"):
        auth = "nkey"
    else:
        auth = "none (dev)"
    try:
        from nats_connect import safe_url
        nats_disp = safe_url(NATS_URL)
    except ImportError:
        nats_disp = NATS_URL.split("@")[-1] if "@" in NATS_URL else NATS_URL
    print(f"NATS:    {nats_disp}  auth={auth}")
    acl = cfg.get("acl") or {}
    print(f"ACL:     default={acl.get('default', 'same_plant_only')} edges={len(acl.get('allow') or {})}")
    return 0


def cmd_plants(cfg: dict) -> int:
    for pid, p in cfg.get("plants", {}).items():
        roles = ", ".join(p.get("roles_allowed", []))
        iso = " [isolated]" if p.get("isolation") else ""
        print(f"{pid}: {p.get('name', pid)}{iso}")
        print(f"  profile={p.get('profile')} region={p.get('region', '-')} roles=[{roles}]")
        if p.get("description"):
            print(f"  {p['description']}")
    return 0


def cmd_nodes() -> int:
    state = load_state()
    nodes = state.get("nodes", {})
    if not nodes:
        print("(no registered nodes — run: fleet.py register)")
        return 0
    for nid, n in nodes.items():
        print(json.dumps(n, indent=2))
    return 0


def cmd_register(cfg: dict) -> int:
    node = build_local_node(cfg)
    plant = node.plant
    plants = cfg.get("plants", {})
    if plant not in plants:
        print(f"error: unknown plant '{plant}'", file=sys.stderr)
        return 1
    allowed = set(plants[plant].get("roles_allowed", []))
    for role in node.roles:
        if allowed and role not in allowed:
            print(f"warn: role '{role}' not in plant {plant} allowlist {sorted(allowed)}", file=sys.stderr)

    state = load_state()
    state.setdefault("nodes", {})[node.node_id] = node.to_dict()
    save_state(state)
    print(f"Registered {node.node_id} → plant={plant} team={node.team} roles={node.roles}")

    try:
        asyncio.run(_nats_register(node))
    except Exception as exc:
        print(f"nats: skipped ({exc})")
    return 0


async def _nats_register(node: FleetNode) -> None:
    try:
        from nats_connect import connect as nats_connect, safe_url
    except ImportError:
        from nats import connect as nats_connect

        def safe_url(u=None):
            return u or NATS_URL

    nc = await nats_connect(_nats_url())
    payload = json.dumps(node.to_dict()).encode()
    await dual_publish(nc, SUBJECT_REGISTER, payload)
    await dual_publish(nc, SUBJECT_STATUS, payload)
    await nc.flush()
    await nc.close()
    print(f"nats: dual-published register on {dual(SUBJECT_REGISTER)} via {safe_url()}")


def cmd_exercise(cfg: dict, action: str) -> int:
    rb = cfg.get("red_blue", {})
    if not rb.get("enabled", True) and action == "start":
        print("red/blue exercises disabled in config")
        return 1
    state = load_state()
    plant = rb.get("default_plant", "plant-range")
    if action == "start":
        state["exercise"] = {"active": True, "plant": plant, "started": _utcnow()}
        save_state(state)
        print(f"Exercise STARTED on plant={plant}")
        for rule in rb.get("rules", []):
            print(f"  rule: {rule}")
    elif action == "stop":
        state["exercise"] = {"active": False, "plant": None, "stopped": _utcnow()}
        save_state(state)
        print("Exercise STOPPED")
    else:
        ex = state.get("exercise", {})
        print(json.dumps(ex, indent=2))
    try:
        asyncio.run(_nats_exercise(state.get("exercise", {})))
    except Exception as exc:
        print(f"nats: skipped ({exc})")
    return 0


async def _nats_exercise(exercise: dict) -> None:
    try:
        from nats_connect import connect as nats_connect
    except ImportError:
        from nats import connect as nats_connect

    nc = await nats_connect(_nats_url())
    await dual_publish(nc, SUBJECT_EXERCISE, json.dumps(exercise).encode())
    await nc.flush()
    await nc.close()


async def daemon_loop(cfg: dict) -> None:
    try:
        from nats_connect import connect as nats_connect, safe_url
    except ImportError:
        from nats import connect as nats_connect

        def safe_url(u=None):
            u = u or _nats_url()
            return u.split("@")[-1] if "@" in u else u

    node = build_local_node(cfg)
    state = load_state()
    state.setdefault("nodes", {})[node.node_id] = node.to_dict()
    save_state(state)

    url = _nats_url()
    nc = await nats_connect(url)
    mode = os.getenv("STARSHIP_NATS_MODE", "")
    if os.getenv("NATS_USER") or mode == "accounts":
        auth = f"accounts/{os.getenv('STARSHIP_NATS_ROLE', os.getenv('NATS_USER', 'user'))}"
    elif os.getenv("STARSHIP_NATS_TOKEN", "").strip():
        auth = "token"
    elif os.getenv("STARSHIP_NATS_NKEY_SEED") or os.getenv("STARSHIP_NATS_NKEY_SEED_FILE"):
        auth = "nkey"
    else:
        auth = "none"
    print(f"fleet daemon: {node.node_id} plant={node.plant} nats={safe_url(url)} auth={auth}")

    async def on_register(msg):
        try:
            data = json.loads(msg.data.decode())
            nid = data.get("node_id")
            if nid:
                st = load_state()
                st.setdefault("nodes", {})[nid] = data
                save_state(st)
                print(f"peer registered: {nid}")
        except Exception:
            pass

    async def on_heartbeat(msg):
        try:
            data = json.loads(msg.data.decode())
            nid = data.get("node_id")
            if nid:
                st = load_state()
                nodes = st.setdefault("nodes", {})
                if nid in nodes:
                    nodes[nid]["last_seen"] = data.get("last_seen", _utcnow())
                    nodes[nid]["status"] = data.get("status", "online")
                else:
                    nodes[nid] = data
                save_state(st)
        except Exception:
            pass

    for subj in dual(SUBJECT_REGISTER):
        await nc.subscribe(subj, cb=on_register)
    for subj in dual(SUBJECT_HEARTBEAT):
        await nc.subscribe(subj, cb=on_heartbeat)

    # Ops manager status broadcast
    ops = cfg.get("fleet", {}).get("ops_manager", {})
    interval = int(os.getenv("STARSHIP_FLEET_HB", "30"))

    while True:
        node.last_seen = _utcnow()
        node.status = "online"
        payload = json.dumps(node.to_dict()).encode()
        await dual_publish(nc, SUBJECT_HEARTBEAT, payload)
        if ops.get("enabled", True):
            summary = {
                "ops_manager": True,
                "node_id": node.node_id,
                "fleet": cfg.get("fleet", {}).get("name"),
                "plants": list(cfg.get("plants", {}).keys()),
                "nodes": list(load_state().get("nodes", {}).keys()),
                "exercise": load_state().get("exercise", {}),
                "timestamp": _utcnow(),
            }
            await dual_publish(nc, SUBJECT_OPS, json.dumps(summary).encode())
        await asyncio.sleep(interval)


def main() -> int:
    parser = argparse.ArgumentParser(description="Starship OS Fleet Manager")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("status", help="Fleet overview")
    sub.add_parser("plants", help="List plants")
    sub.add_parser("nodes", help="List known nodes")
    sub.add_parser("register", help="Register this node")
    sub.add_parser("daemon", help="Run fleet heartbeat/ops daemon")
    ex = sub.add_parser("exercise", help="Red/blue exercise control")
    ex.add_argument("action", choices=["start", "stop", "status"])

    args = parser.parse_args()
    if not args.cmd:
        parser.print_help()
        return 2

    cfg = load_fleet_config()

    if args.cmd == "status":
        return cmd_status(cfg)
    if args.cmd == "plants":
        return cmd_plants(cfg)
    if args.cmd == "nodes":
        return cmd_nodes()
    if args.cmd == "register":
        return cmd_register(cfg)
    if args.cmd == "exercise":
        return cmd_exercise(cfg, args.action)
    if args.cmd == "daemon":
        try:
            asyncio.run(daemon_loop(cfg))
        except KeyboardInterrupt:
            print("stopped")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
