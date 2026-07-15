#!/usr/bin/env python3
"""
Starship OS Multi-Node Cluster Service

NATS-based clustering and node management for running agents across
multiple machines. Handles node registration, heartbeats, discovery,
task routing, and load balancing.

Usage:
    python3 cluster.py status            # show cluster overview
    python3 cluster.py register          # register this node
    python3 cluster.py heartbeat         # send a single heartbeat
    python3 cluster.py nodes             # list known nodes
    python3 cluster.py monitor           # watch cluster in real-time
    python3 cluster.py route <task_json> # route a task to best node
    python3 cluster.py daemon            # run as background service
"""

import sys
import os
import json
import time
import signal
import asyncio
import socket
import platform
import logging
import logging.handlers
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Optional
from dataclasses import dataclass, field, asdict

try:
    import yaml
except ImportError:
    yaml = None

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
PROJECT_ROOT = SCRIPT_DIR.parent
CONFIG_PATH = PROJECT_ROOT / "services" / "cluster_config.yaml"
SYSTEM_CONFIG_PATH = Path("/etc/agnetic/cluster.yaml")
_log_dir = Path("/var/log/agnetic")
if not os.access(_log_dir, os.W_OK):
    _log_dir = Path("/tmp/agnetic-data/logs")
_log_dir.mkdir(parents=True, exist_ok=True)
LOG_DIR = _log_dir
LOG_FILE = LOG_DIR / "cluster.log"

NATS_URL = os.getenv("NATS_URL", "nats://127.0.0.1:4222")

SUBJECT_NODE_REGISTER = "agnetic.cluster.register"
SUBJECT_NODE_DEREGISTER = "agnetic.cluster.deregister"
SUBJECT_HEARTBEAT = "agnetic.cluster.heartbeat.{node_id}"
SUBJECT_HEARTBEAT_ALL = "agnetic.cluster.heartbeat.*"
SUBJECT_STATUS = "agnetic.cluster.status"
SUBJECT_STATUS_REQUEST = "agnetic.cluster.status.request"
SUBJECT_TASK_DELEGATE = "agnetic.cluster.task.delegate"
SUBJECT_TASK_RESULT = "agnetic.cluster.task.result.{task_id}"
SUBJECT_TASK_RESULT_ALL = "agnetic.cluster.task.result.*"
SUBJECT_NODE_ALERT = "agnetic.cluster.alert"
SUBJECT_DISCOVERY = "agnetic.cluster.discovery"
SUBJECT_DISCOVERY_REQUEST = "agnetic.cluster.discovery.request"

DEFAULT_HEARTBEAT_INTERVAL = 30
DEFAULT_OFFLINE_THRESHOLD = 3
DEFAULT_TASK_TIMEOUT = 120

NODE_STATUSES = ("online", "offline", "busy", "maintenance")
ROUTING_STRATEGIES = ("capability-based", "round-robin", "least-loaded")

# ---------------------------------------------------------------------------
# Structured Logger (watchdog.py pattern)
# ---------------------------------------------------------------------------


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "service": getattr(record, "service", "cluster"),
            "event": getattr(record, "event", record.getMessage()),
        }
        details = getattr(record, "details", None)
        if details:
            entry["details"] = details
        return json.dumps(entry, default=str)


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("cluster")
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
        pass
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


log = setup_logging()


def _log(event: str, service: str = "cluster", level: str = "info", details: dict | None = None):
    extra: dict[str, Any] = {"service": service, "event": event}
    if details:
        extra["details"] = details
    getattr(log, level, log.info)(event, extra=extra)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def load_config() -> dict:
    """Load cluster config from YAML files, falling back to defaults."""
    default = {
        "cluster": {
            "name": "agnetic-mesh",
            "heartbeat_interval": DEFAULT_HEARTBEAT_INTERVAL,
            "offline_threshold": DEFAULT_OFFLINE_THRESHOLD,
        },
        "node": {
            "roles": ["proxy", "romi", "ergo"],
            "capabilities": {
                "gpu": "auto-detect",
                "cpu_cores": "auto",
                "memory_mb": "auto",
            },
        },
        "routing": {
            "strategy": "capability-based",
            "prefer_local": True,
        },
    }

    for path in (SYSTEM_CONFIG_PATH, CONFIG_PATH):
        if path.exists() and yaml is not None:
            try:
                with open(path) as f:
                    cfg = yaml.safe_load(f) or {}
                _log("config_loaded", details={"path": str(path)})
                return _deep_merge(default, cfg)
            except Exception as exc:
                _log("config_load_failed", level="warning", details={"path": str(path), "error": str(exc)})

    return default


def _deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


# ---------------------------------------------------------------------------
# System Capability Detection
# ---------------------------------------------------------------------------


def detect_capabilities() -> dict:
    """Auto-detect system capabilities."""
    import multiprocessing

    caps: dict[str, Any] = {
        "cpu_cores": multiprocessing.cpu_count(),
        "memory_mb": _get_memory_mb(),
        "gpu": _detect_gpu(),
        "disk_free_gb": _get_disk_free_gb(),
        "hostname": socket.gethostname(),
        "ip": _get_local_ip(),
        "platform": platform.system(),
        "arch": platform.machine(),
    }
    return caps


def _get_memory_mb() -> int:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // 1024
    except Exception:
        pass
    return 0


def _detect_gpu() -> dict | None:
    try:
        import subprocess
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            parts = out.stdout.strip().split(", ")
            return {
                "type": "nvidia",
                "name": parts[0],
                "memory_total_mb": int(parts[1]),
                "memory_free_mb": int(parts[2]),
            }
    except Exception:
        pass
    return None


def _get_disk_free_gb() -> float:
    try:
        st = os.statvfs("/")
        return (st.f_bavail * st.f_frsize) / (1024 ** 3)
    except Exception:
        return 0.0


def _get_local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


# ---------------------------------------------------------------------------
# Node Registry
# ---------------------------------------------------------------------------


@dataclass
class NodeInfo:
    node_id: str
    hostname: str
    ip: str
    roles: list[str]
    capabilities: dict
    status: str = "online"
    registered_at: str = ""
    last_heartbeat: str = ""
    missed_heartbeats: int = 0
    active_tasks: int = 0
    max_tasks: int = 10

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "NodeInfo":
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)

    @property
    def is_available(self) -> bool:
        return self.status == "online" and self.active_tasks < self.max_tasks

    @property
    def load_ratio(self) -> float:
        if self.max_tasks <= 0:
            return 1.0
        return self.active_tasks / self.max_tasks

    def matches_requirements(self, required: dict) -> bool:
        """Check if this node satisfies the given capability requirements."""
        for key, value in required.items():
            if key == "gpu":
                if value and not self.capabilities.get("gpu"):
                    return False
            elif key == "min_cpu_cores":
                if self.capabilities.get("cpu_cores", 0) < value:
                    return False
            elif key == "min_memory_mb":
                if self.capabilities.get("memory_mb", 0) < value:
                    return False
            elif key == "roles":
                if isinstance(value, str):
                    value = [value]
                if not any(r in self.roles for r in value):
                    return False
        return True


class NodeRegistry:
    """In-memory registry of known cluster nodes."""

    def __init__(self, offline_threshold: int = DEFAULT_OFFLINE_THRESHOLD):
        self._nodes: dict[str, NodeInfo] = {}
        self._offline_threshold = offline_threshold
        self._round_robin_idx = 0

    def upsert(self, node: NodeInfo):
        existing = self._nodes.get(node.node_id)
        if existing:
            node.missed_heartbeats = 0
            node.status = "online" if node.status != "maintenance" else "maintenance"
            node.last_heartbeat = datetime.now(timezone.utc).isoformat()
            node.active_tasks = existing.active_tasks
        else:
            node.registered_at = datetime.now(timezone.utc).isoformat()
            node.last_heartbeat = node.registered_at
        self._nodes[node.node_id] = node

    def remove(self, node_id: str):
        self._nodes.pop(node_id, None)

    def get(self, node_id: str) -> NodeInfo | None:
        return self._nodes.get(node_id)

    def all_nodes(self) -> list[NodeInfo]:
        return list(self._nodes.values())

    def online_nodes(self) -> list[NodeInfo]:
        return [n for n in self._nodes.values() if n.status == "online"]

    def available_nodes(self) -> list[NodeInfo]:
        return [n for n in self._nodes.values() if n.is_available]

    def nodes_with_role(self, role: str) -> list[NodeInfo]:
        return [n for n in self._nodes.values() if role in n.roles and n.is_available]

    def nodes_with_gpu(self) -> list[NodeInfo]:
        return [n for n in self._nodes.values() if n.capabilities.get("gpu") and n.is_available]

    def mark_missed_heartbeat(self, node_id: str):
        node = self._nodes.get(node_id)
        if not node:
            return
        node.missed_heartbeats += 1
        if node.missed_heartbeats >= self._offline_threshold:
            node.status = "offline"
            _log("node_offline", level="warning", details={"node_id": node_id, "missed": node.missed_heartbeats})

    def get_offline_nodes(self) -> list[NodeInfo]:
        return [n for n in self._nodes.values() if n.status == "offline"]

    def cluster_summary(self) -> dict:
        nodes = list(self._nodes.values())
        return {
            "total_nodes": len(nodes),
            "online": sum(1 for n in nodes if n.status == "online"),
            "offline": sum(1 for n in nodes if n.status == "offline"),
            "busy": sum(1 for n in nodes if n.status == "busy"),
            "maintenance": sum(1 for n in nodes if n.status == "maintenance"),
            "total_cpu_cores": sum(n.capabilities.get("cpu_cores", 0) for n in nodes),
            "total_memory_mb": sum(n.capabilities.get("memory_mb", 0) for n in nodes),
            "gpu_nodes": sum(1 for n in nodes if n.capabilities.get("gpu")),
            "active_tasks": sum(n.active_tasks for n in nodes),
            "nodes": [n.to_dict() for n in nodes],
        }


# ---------------------------------------------------------------------------
# Task Router
# ---------------------------------------------------------------------------


@dataclass
class TaskRequest:
    task_id: str
    command: str
    args: dict = field(default_factory=dict)
    requirements: dict = field(default_factory=dict)
    preferred_node: str = ""
    timeout: int = DEFAULT_TASK_TIMEOUT
    created_by: str = ""
    created_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "TaskRequest":
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)


class TaskRouter:
    """Routes tasks to the best available node based on strategy."""

    def __init__(self, registry: NodeRegistry, strategy: str = "capability-based", prefer_local: bool = True):
        self._registry = registry
        self._strategy = strategy
        self._prefer_local = prefer_local
        self._local_node_id = socket.gethostname()

    def route(self, task: TaskRequest) -> NodeInfo | None:
        """Select the best node for a task. Returns None if no node available."""
        if task.preferred_node:
            node = self._registry.get(task.preferred_node)
            if node and node.is_available:
                return node

        candidates = self._registry.available_nodes()
        if not candidates:
            return None

        if task.requirements:
            candidates = [n for n in candidates if n.matches_requirements(task.requirements)]
            if not candidates:
                return None

        if self._strategy == "capability-based":
            return self._route_capability(candidates, task)
        elif self._strategy == "round-robin":
            return self._route_round_robin(candidates)
        elif self._strategy == "least-loaded":
            return self._route_least_loaded(candidates)
        else:
            return self._route_capability(candidates, task)

    def _route_capability(self, candidates: list[NodeInfo], task: TaskRequest) -> NodeInfo:
        """Score nodes by capability match and pick the best."""
        scored = []
        for node in candidates:
            score = 0.0

            if self._prefer_local and node.node_id == self._local_node_id:
                score += 10.0

            gpu_req = task.requirements.get("gpu", False)
            if gpu_req and node.capabilities.get("gpu"):
                score += 5.0

            cpu = node.capabilities.get("cpu_cores", 0)
            mem = node.capabilities.get("memory_mb", 0)
            score += min(cpu / 4.0, 3.0)
            score += min(mem / 4096.0, 3.0)

            score -= node.load_ratio * 4.0

            scored.append((score, node))

        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[0][1]

    def _route_round_robin(self, candidates: list[NodeInfo]) -> NodeInfo:
        idx = self._registry._round_robin_idx % len(candidates)
        self._registry._round_robin_idx += 1
        return candidates[idx]

    def _route_least_loaded(self, candidates: list[NodeInfo]) -> NodeInfo:
        return min(candidates, key=lambda n: n.load_ratio)


# ---------------------------------------------------------------------------
# NATS Cluster Manager
# ---------------------------------------------------------------------------


class ClusterManager:
    """Main cluster manager — handles NATS pub/sub for node coordination."""

    def __init__(self, config: dict):
        self._config = config
        cluster_cfg = config.get("cluster", {})
        node_cfg = config.get("node", {})
        routing_cfg = config.get("routing", {})

        self._cluster_name = cluster_cfg.get("name", "agnetic-mesh")
        self._heartbeat_interval = cluster_cfg.get("heartbeat_interval", DEFAULT_HEARTBEAT_INTERVAL)
        self._offline_threshold = cluster_cfg.get("offline_threshold", DEFAULT_OFFLINE_THRESHOLD)

        caps = detect_capabilities()
        roles = node_cfg.get("roles", ["proxy", "romi", "ergo"])

        self._node = NodeInfo(
            node_id=caps["hostname"],
            hostname=caps["hostname"],
            ip=caps["ip"],
            roles=roles,
            capabilities=caps,
        )

        self._registry = NodeRegistry(offline_threshold=self._offline_threshold)
        self._registry.upsert(self._node)

        self._router = TaskRouter(
            self._registry,
            strategy=routing_cfg.get("strategy", "capability-based"),
            prefer_local=routing_cfg.get("prefer_local", True),
        )

        self._nc = None
        self._js = None
        self._running = True
        self._subscriptions: list = []

    @property
    def node(self) -> NodeInfo:
        return self._node

    @property
    def registry(self) -> NodeRegistry:
        return self._registry

    @property
    def router(self) -> TaskRouter:
        return self._router

    async def connect(self):
        try:
            import nats as nats_mod
            self._nc = await nats_mod.connect(NATS_URL)
            self._js = self._nc.jetstream()
            _log("nats_connected", details={"url": NATS_URL})
        except Exception as exc:
            _log("nats_connect_failed", level="error", details={"error": str(exc)})
            raise

    async def close(self):
        for sub in self._subscriptions:
            try:
                await sub.unsubscribe()
            except Exception:
                pass
        self._subscriptions.clear()
        if self._nc:
            try:
                await self._nc.close()
            except Exception:
                pass

    async def publish(self, subject: str, payload: dict):
        if not self._nc:
            return
        try:
            data = json.dumps(payload, default=str).encode()
            await self._js.publish(subject, data)
        except Exception as exc:
            _log("publish_failed", level="warning", details={"subject": subject, "error": str(exc)})

    async def request(self, subject: str, payload: dict, timeout: float = 5.0) -> dict | None:
        if not self._nc:
            return None
        try:
            data = json.dumps(payload, default=str).encode()
            msg = await self._nc.request(subject, data, timeout=timeout)
            return json.loads(msg.data.decode())
        except Exception:
            return None

    # -- Node Registration --------------------------------------------------

    async def register_node(self):
        """Announce this node to the cluster."""
        self._node.status = "online"
        self._node.registered_at = datetime.now(timezone.utc).isoformat()
        self._node.last_heartbeat = self._node.registered_at

        payload = self._node.to_dict()
        await self.publish(SUBJECT_NODE_REGISTER, payload)
        _log("node_registered", details={"node_id": self._node.node_id, "roles": self._node.roles})

    async def deregister_node(self):
        """Announce this node is leaving the cluster."""
        self._node.status = "offline"
        await self.publish(SUBJECT_NODE_DEREGISTER, {"node_id": self._node.node_id})
        _log("node_deregistered", details={"node_id": self._node.node_id})

    # -- Heartbeat ----------------------------------------------------------

    async def send_heartbeat(self):
        """Send a heartbeat for this node."""
        self._node.last_heartbeat = datetime.now(timezone.utc).isoformat()
        payload = {
            "node_id": self._node.node_id,
            "hostname": self._node.hostname,
            "ip": self._node.ip,
            "status": self._node.status,
            "active_tasks": self._node.active_tasks,
            "capabilities": self._node.capabilities,
            "timestamp": self._node.last_heartbeat,
        }
        subject = SUBJECT_HEARTBEAT.format(node_id=self._node.node_id)
        await self.publish(subject, payload)

    # -- Subscriptions ------------------------------------------------------

    async def setup_subscriptions(self):
        """Subscribe to all cluster NATS subjects."""
        await self._sub_register()
        await self._sub_deregister()
        await self._sub_heartbeats()
        await self._sub_status_request()
        await self._sub_task_delegate()
        await self._sub_discovery_request()

    async def _sub_register(self):
        async def handler(msg):
            try:
                data = json.loads(msg.data.decode())
                node = NodeInfo.from_dict(data)
                if node.node_id != self._node.node_id:
                    self._registry.upsert(node)
                    _log("peer_registered", details={"node_id": node.node_id, "ip": node.ip})
            except Exception as exc:
                _log("register_handler_error", level="warning", details={"error": str(exc)})

        sub = await self._nc.subscribe(SUBJECT_NODE_REGISTER, cb=handler)
        self._subscriptions.append(sub)

    async def _sub_deregister(self):
        async def handler(msg):
            try:
                data = json.loads(msg.data.decode())
                node_id = data.get("node_id", "")
                if node_id and node_id != self._node.node_id:
                    self._registry.remove(node_id)
                    _log("peer_deregistered", details={"node_id": node_id})
            except Exception as exc:
                _log("deregister_handler_error", level="warning", details={"error": str(exc)})

        sub = await self._nc.subscribe(SUBJECT_NODE_DEREGISTER, cb=handler)
        self._subscriptions.append(sub)

    async def _sub_heartbeats(self):
        async def handler(msg):
            try:
                data = json.loads(msg.data.decode())
                node_id = data.get("node_id", "")
                if node_id and node_id != self._node.node_id:
                    node = self._registry.get(node_id)
                    if node:
                        node.last_heartbeat = data.get("timestamp", "")
                        node.missed_heartbeats = 0
                        node.status = data.get("status", "online")
                        node.active_tasks = data.get("active_tasks", 0)
                        node.capabilities = data.get("capabilities", node.capabilities)
                    else:
                        node = NodeInfo.from_dict(data)
                        self._registry.upsert(node)
                        _log("discovered_peer", details={"node_id": node_id})
            except Exception as exc:
                _log("heartbeat_handler_error", level="warning", details={"error": str(exc)})

        sub = await self._nc.subscribe(SUBJECT_HEARTBEAT_ALL, cb=handler)
        self._subscriptions.append(sub)

    async def _sub_status_request(self):
        async def handler(msg):
            try:
                summary = self._registry.cluster_summary()
                summary["cluster_name"] = self._cluster_name
                summary["local_node"] = self._node.node_id
                if msg.reply:
                    await self._nc.publish(msg.reply, json.dumps(summary, default=str).encode())
            except Exception as exc:
                _log("status_handler_error", level="warning", details={"error": str(exc)})

        sub = await self._nc.subscribe(SUBJECT_STATUS_REQUEST, cb=handler)
        self._subscriptions.append(sub)

    async def _sub_task_delegate(self):
        async def handler(msg):
            try:
                data = json.loads(msg.data.decode())
                task = TaskRequest.from_dict(data)
                node = self._router.route(task)
                if node:
                    result_payload = {
                        "task_id": task.task_id,
                        "assigned_node": node.node_id,
                        "status": "routed",
                    }
                    node.active_tasks += 1
                else:
                    result_payload = {
                        "task_id": task.task_id,
                        "assigned_node": None,
                        "status": "no_available_node",
                    }
                if msg.reply:
                    await self._nc.publish(msg.reply, json.dumps(result_payload, default=str).encode())
                _log("task_routed", details={"task_id": task.task_id, "target": result_payload.get("assigned_node")})
            except Exception as exc:
                _log("task_handler_error", level="warning", details={"error": str(exc)})

        sub = await self._nc.subscribe(SUBJECT_TASK_DELEGATE, cb=handler)
        self._subscriptions.append(sub)

    async def _sub_discovery_request(self):
        async def handler(msg):
            try:
                summary = self._registry.cluster_summary()
                summary["cluster_name"] = self._cluster_name
                if msg.reply:
                    await self._nc.publish(msg.reply, json.dumps(summary, default=str).encode())
            except Exception as exc:
                _log("discovery_handler_error", level="warning", details={"error": str(exc)})

        sub = await self._nc.subscribe(SUBJECT_DISCOVERY_REQUEST, cb=handler)
        self._subscriptions.append(sub)

    # -- Background Loops ---------------------------------------------------

    async def _heartbeat_loop(self):
        while self._running:
            try:
                await self.send_heartbeat()
            except Exception as exc:
                _log("heartbeat_error", level="warning", details={"error": str(exc)})
            await asyncio.sleep(self._heartbeat_interval)

    async def _missed_heartbeat_checker(self):
        """Check for missed heartbeats from peers."""
        while self._running:
            await asyncio.sleep(self._heartbeat_interval)
            try:
                now = time.time()
                for node in self._registry.all_nodes():
                    if node.node_id == self._node.node_id:
                        continue
                    if node.last_heartbeat:
                        try:
                            last = datetime.fromisoformat(node.last_heartbeat).timestamp()
                        except Exception:
                            continue
                        if (now - last) > (self._heartbeat_interval * self._offline_threshold * 1.5):
                            self._registry.mark_missed_heartbeat(node.node_id)
                            if node.status == "offline":
                                await self.publish(SUBJECT_NODE_ALERT, {
                                    "type": "node_offline",
                                    "node_id": node.node_id,
                                    "timestamp": datetime.now(timezone.utc).isoformat(),
                                })
            except Exception as exc:
                _log("heartbeat_check_error", level="warning", details={"error": str(exc)})

    async def _cluster_status_publisher(self):
        """Periodically publish aggregated cluster status."""
        while self._running:
            await asyncio.sleep(self._heartbeat_interval)
            try:
                summary = self._registry.cluster_summary()
                summary["cluster_name"] = self._cluster_name
                summary["timestamp"] = datetime.now(timezone.utc).isoformat()
                await self.publish(SUBJECT_STATUS, summary)
            except Exception as exc:
                _log("status_publish_error", level="warning", details={"error": str(exc)})

    # -- Run ----------------------------------------------------------------

    async def run(self):
        _log("cluster_starting", details={
            "node_id": self._node.node_id,
            "cluster": self._cluster_name,
            "roles": self._node.roles,
        })
        await self.connect()
        await self.register_node()
        await self.setup_subscriptions()

        await asyncio.gather(
            self._heartbeat_loop(),
            self._missed_heartbeat_checker(),
            self._cluster_status_publisher(),
        )

    def stop(self):
        self._running = False


# ---------------------------------------------------------------------------
# Task Delegation (used by agents via delegate_to_agent tool)
# ---------------------------------------------------------------------------


async def delegate_task_to_node(
    nats_conn,
    task_id: str,
    command: str,
    args: dict = None,
    requirements: dict = None,
    preferred_node: str = "",
    timeout: int = DEFAULT_TASK_TIMEOUT,
) -> dict:
    """Delegate a task to a cluster node via NATS.

    This is the function agents call from their delegate_to_agent tool
    when they want to route work to a specific or best-fit node.
    """
    task = TaskRequest(
        task_id=task_id,
        command=command,
        args=args or {},
        requirements=requirements or {},
        preferred_node=preferred_node,
        timeout=timeout,
        created_at=datetime.now(timezone.utc).isoformat(),
    )

    try:
        data = json.dumps(task.to_dict(), default=str).encode()
        msg = await nats_conn.request(SUBJECT_TASK_DELEGATE, data, timeout=10.0)
        return json.loads(msg.data.decode())
    except asyncio.TimeoutError:
        return {"error": "Cluster did not respond to task routing request"}
    except Exception as e:
        return {"error": str(e)}


async def discover_cluster(nats_conn, timeout: float = 5.0) -> dict:
    """Request cluster status from any available cluster manager."""
    try:
        data = json.dumps({"request": "status"}).encode()
        msg = await nats_conn.request(SUBJECT_DISCOVERY_REQUEST, data, timeout=timeout)
        return json.loads(msg.data.decode())
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_node_table(nodes: list[NodeInfo]):
    if not nodes:
        print("  (no nodes known)")
        return
    print(f"  {'Node ID':<25} {'IP':<16} {'Status':<14} {'Roles':<25} {'GPU':<6} {'Load':<6}")
    print("  " + "-" * 96)
    for n in nodes:
        gpu = "yes" if n.capabilities.get("gpu") else "no"
        load = f"{n.load_ratio:.0%}"
        roles = ",".join(n.roles) if n.roles else "-"
        print(f"  {n.node_id:<25} {n.ip:<16} {n.status:<14} {roles:<25} {gpu:<6} {load:<6}")


async def cmd_status():
    """Show cluster overview by querying the cluster via NATS."""
    import nats as nats_mod
    try:
        nc = await asyncio.wait_for(nats_mod.connect(NATS_URL), timeout=5)
        data = json.dumps({"request": "status"}).encode()
        msg = await nc.request(SUBJECT_STATUS_REQUEST, data, timeout=5.0)
        result = json.loads(msg.data.decode())
        await nc.close()
    except Exception:
        # Fallback: show local node info
        config = load_config()
        caps = detect_capabilities()
        result = {
            "cluster_name": config["cluster"]["name"],
            "total_nodes": 1,
            "online": 1,
            "offline": 0,
            "gpu_nodes": 1 if caps.get("gpu") else 0,
            "total_cpu_cores": caps.get("cpu_cores", 0),
            "total_memory_mb": caps.get("memory_mb", 0),
            "nodes": [{
                "node_id": caps["hostname"],
                "ip": caps["ip"],
                "status": "online",
                "roles": config["node"]["roles"],
                "capabilities": caps,
                "active_tasks": 0,
            }],
        }

    print(f"\n  Cluster: {result.get('cluster_name', 'unknown')}")
    print(f"  Nodes:   {result.get('total_nodes', 0)} total, "
          f"{result.get('online', 0)} online, "
          f"{result.get('offline', 0)} offline")
    print(f"  CPU:     {result.get('total_cpu_cores', 0)} cores total")
    print(f"  Memory:  {result.get('total_memory_mb', 0)} MB total")
    print(f"  GPU:     {result.get('gpu_nodes', 0)} nodes with GPU\n")

    nodes_raw = result.get("nodes", [])
    nodes = [NodeInfo.from_dict(n) for n in nodes_raw]
    _print_node_table(nodes)
    print()


async def cmd_register():
    """Register this node on the cluster."""
    config = load_config()
    manager = ClusterManager(config)
    await manager.connect()
    await manager.register_node()
    print(f"  Registered node: {manager.node.node_id} ({manager.node.ip})")
    print(f"  Roles: {', '.join(manager.node.roles)}")
    caps = manager.node.capabilities
    gpu_str = f"{caps['gpu']['name']} ({caps['gpu']['memory_total_mb']} MB)" if caps.get("gpu") else "none"
    print(f"  GPU: {gpu_str}")
    print(f"  CPU: {caps.get('cpu_cores', 0)} cores, Memory: {caps.get('memory_mb', 0)} MB")
    await manager.close()


async def cmd_heartbeat():
    """Send a single heartbeat."""
    config = load_config()
    manager = ClusterManager(config)
    await manager.connect()
    await manager.send_heartbeat()
    print(f"  Heartbeat sent from {manager.node.node_id}")
    await manager.close()


async def cmd_nodes():
    """List all known nodes by querying the cluster."""
    import nats as nats_mod
    try:
        nc = await asyncio.wait_for(nats_mod.connect(NATS_URL), timeout=5)
        data = json.dumps({"request": "discovery"}).encode()
        msg = await nc.request(SUBJECT_DISCOVERY_REQUEST, data, timeout=5.0)
        result = json.loads(msg.data.decode())
        await nc.close()
    except Exception:
        config = load_config()
        caps = detect_capabilities()
        result = {"nodes": [{
            "node_id": caps["hostname"],
            "ip": caps["ip"],
            "status": "online",
            "roles": config["node"]["roles"],
            "capabilities": caps,
            "active_tasks": 0,
            "last_heartbeat": datetime.now(timezone.utc).isoformat(),
        }]}

    nodes = [NodeInfo.from_dict(n) for n in result.get("nodes", [])]
    print()
    _print_node_table(nodes)
    print()


async def cmd_monitor():
    """Watch the cluster in real-time."""
    import nats as nats_mod
    try:
        nc = await asyncio.wait_for(nats_mod.connect(NATS_URL), timeout=5)
    except Exception as exc:
        print(f"  Cannot connect to NATS: {exc}")
        return

    print("  Monitoring cluster... (Ctrl+C to stop)\n")

    async def on_status(msg):
        try:
            data = json.loads(msg.data.decode())
            ts = data.get("timestamp", "")
            online = data.get("online", 0)
            total = data.get("total_nodes", 0)
            tasks = data.get("active_tasks", 0)
            print(f"  [{ts}] {online}/{total} nodes online, {tasks} active tasks")
        except Exception:
            pass

    async def on_alert(msg):
        try:
            data = json.loads(msg.data.decode())
            print(f"  [ALERT] {data.get('type', 'unknown')}: {data.get('node_id', '?')}")
        except Exception:
            pass

    sub_status = await nc.subscribe(SUBJECT_STATUS, cb=on_status)
    sub_alert = await nc.subscribe(SUBJECT_NODE_ALERT, cb=on_alert)

    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        await sub_status.unsubscribe()
        await sub_alert.unsubscribe()
        await nc.close()


async def cmd_daemon():
    """Run cluster manager as a background service."""
    config = load_config()
    manager = ClusterManager(config)

    def _shutdown(signum, _frame):
        _log("signal_received", details={"signal": signum})
        manager.stop()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        await manager.run()
    except KeyboardInterrupt:
        pass
    finally:
        await manager.deregister_node()
        await manager.close()
        _log("cluster_stopped")


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <command>")
        print("Commands: status, register, heartbeat, nodes, monitor, daemon")
        sys.exit(1)

    command = sys.argv[1]

    if command == "status":
        asyncio.run(cmd_status())
    elif command == "register":
        asyncio.run(cmd_register())
    elif command == "heartbeat":
        asyncio.run(cmd_heartbeat())
    elif command == "nodes":
        asyncio.run(cmd_nodes())
    elif command == "monitor":
        asyncio.run(cmd_monitor())
    elif command == "daemon":
        asyncio.run(cmd_daemon())
    else:
        print(f"Unknown command: {command}")
        print("Commands: status, register, heartbeat, nodes, monitor, daemon")
        sys.exit(1)


if __name__ == "__main__":
    main()
