# Multi-Node Agent System

Running Starship OS agents across multiple machines connected via NATS.

## Architecture

```
┌─────────────────────┐    ┌─────────────────────┐    ┌─────────────────────┐
│   Machine A         │    │   Machine B         │    │   Machine C         │
│   (proxy node)      │    │   (ergo node)       │    │   (romi + GPU)      │
│                     │    │                     │    │                     │
│  ┌───────────────┐  │    │  ┌───────────────┐  │    │  ┌───────────────┐  │
│  │ agent_daemon  │  │    │  │ agent_daemon  │  │    │  │ agent_daemon  │  │
│  │ (proxy)       │  │    │  │ (ergo)        │  │    │  │ (romi)        │  │
│  └───────┬───────┘  │    │  └───────┬───────┘  │    │  └───────┬───────┘  │
│          │          │    │          │          │    │          │          │
│  ┌───────┴───────┐  │    │  ┌───────┴───────┐  │    │  ┌───────┴───────┐  │
│  │ cluster.py    │  │    │  │ cluster.py    │  │    │  │ cluster.py    │  │
│  │ (node mgr)    │  │    │  │ (node mgr)    │  │    │  │ (node mgr)    │  │
│  └───────┬───────┘  │    │  └───────┬───────┘  │    │  └───────┬───────┘  │
└──────────┼──────────┘    └──────────┼──────────┘    └──────────┼──────────┘
           │                          │                          │
           └──────────────┬───────────┴──────────────────────────┘
                          │
                 ┌────────┴────────┐
                 │  NATS Server    │
                 │  (any machine)  │
                 │  JetStream bus  │
                 └────────┬────────┘
                          │
                 ┌────────┴────────┐
                 │  agnetic.mesh   │
                 │  subject tree   │
                 │                 │
                 │  cluster.*      │
                 │  agent.*        │
                 │  telemetry.*    │
                 └─────────────────┘
```

Each node runs `cluster.py daemon` which:
- Registers itself on startup
- Sends periodic heartbeats
- Routes incoming task requests to the best available node
- Monitors peer health and alerts on failures

### NATS Subject Tree

```
agnetic.cluster.register          — node registration announcements
agnetic.cluster.deregister       — node departure announcements
agnetic.cluster.heartbeat.<id>   — per-node heartbeats
agnetic.cluster.status           — aggregated cluster status
agnetic.cluster.status.request   — on-demand status queries
agnetic.cluster.task.delegate    — task routing requests
agnetic.cluster.task.result.*    — task routing responses
agnetic.cluster.alert            — failure/offline alerts
agnetic.cluster.discovery        — service discovery broadcasts
agnetic.cluster.discovery.request — on-demand discovery queries
```

## Setup Guide

### Prerequisites

- NATS server running (single node or cluster)
- Python 3.10+ with `nats-py` and `pyyaml`
- Starship OS installed on each machine

### Single Machine (Default)

```bash
# Start NATS
nats-server -c nats/agent-bus.conf &

# Start cluster manager
python3 services/cluster.py daemon &
```

### Adding a Second Machine

**1. Set up NATS on the first machine to accept remote connections.**

Edit `/etc/agnetic/nats/agent-bus.conf` on Machine A:

```
port: 4222
host: "0.0.0.0"          # listen on all interfaces

jetstream {
  store_dir: "/tmp/agnetic-nats"
  max_memory_store: 256MB
  max_file_store: 1GB
}

max_payload: 8MB
max_connections: 100
```

Restart NATS:
```bash
sudo systemctl restart agnetic-nats
```

**2. Point Machine B to Machine A's NATS.**

```bash
export NATS_URL="nats://<machine-a-ip>:4222"

# Register and start cluster daemon
python3 services/cluster.py register
python3 services/cluster.py daemon &
```

**3. Verify both nodes see each other.**

```bash
# On either machine
python3 services/cluster.py nodes
```

Expected output:
```
  Node ID                  IP               Status       Roles                    GPU    Load
  ------------------------------------------------------------------------------------------------
  machine-a                192.168.1.10     online       proxy,romi,ergo          no     0%
  machine-b                192.168.1.11     online       proxy,romi,ergo          yes    0%
```

### Adding a GPU Node

On a machine with an NVIDIA GPU:

```bash
export NATS_URL="nats://<nats-server-ip>:4222"
python3 services/cluster.py register
python3 services/cluster.py daemon &
```

GPU capabilities are auto-detected via `nvidia-smi`. The node will advertise its GPU type and VRAM, and `capability-based` routing will automatically send GPU-required tasks to it.

### Firewall Rules

Open port 4222 (NATS) between cluster machines:

```bash
# ufw
sudo ufw allow from 192.168.1.0/24 to any port 4222

# firewalld
sudo firewall-cmd --add-rich-rule='rule family="ipv4" source address="192.168.1.0/24" port port="4222" protocol="tcp" accept'
```

## Configuration Reference

`services/cluster_config.yaml`:

```yaml
cluster:
  name: agnetic-mesh            # cluster identifier
  heartbeat_interval: 30        # seconds between heartbeats
  offline_threshold: 3          # missed heartbeats before offline

node:
  roles: [proxy, romi, ergo]    # agent roles this node supports
  capabilities:
    gpu: auto-detect            # auto | nvidia | amd | none
    cpu_cores: auto             # auto-detect or integer
    memory_mb: auto             # auto-detect or integer

routing:
  strategy: capability-based    # capability-based | round-robin | least-loaded
  prefer_local: true            # prefer local node when scores tie
```

### Routing Strategies

| Strategy | Behavior |
|---|---|
| `capability-based` | Scores nodes by GPU match, CPU, memory, and current load. Picks highest score. |
| `round-robin` | Cycles through available nodes sequentially. |
| `least-loaded` | Picks the node with the lowest active_tasks/max_tasks ratio. |

### Override Capabilities

For non-standard hardware or to manually set node capacity:

```yaml
node:
  capabilities:
    gpu:
      type: nvidia
      name: "RTX 4090"
      memory_total_mb: 24576
    cpu_cores: 16
    memory_mb: 65536
```

## Task Routing Examples

### Route via Agent Tool

An agent can delegate work to a specific node or let the cluster pick:

```python
# In an agent's tool call (via delegate_to_agent):
{
    "tool": "delegate_to_agent",
    "args": {
        "agent": "proxy",
        "command": "system.diagnostics.full",
        "cluster": {
            "preferred_node": "gpu-node-1",
            "requirements": {"gpu": true, "min_memory_mb": 8192}
        }
    }
}
```

### Route via Python

```python
import asyncio
from services.cluster import delegate_task_to_node, discover_cluster

async def main():
    import nats
    nc = await nats.connect("nats://127.0.0.1:4222")

    # Discover cluster
    cluster = await discover_cluster(nc)
    print(f"Cluster has {cluster['total_nodes']} nodes")

    # Delegate a task
    result = await delegate_task_to_node(
        nc,
        task_id="task-001",
        command="inference.run",
        args={"model": "qwen2.5:7b", "prompt": "Hello"},
        requirements={"gpu": True},
    )
    print(f"Routed to: {result.get('assigned_node')}")

    await nc.close()

asyncio.run(main())
```

### Route via CLI

```bash
# One-shot task routing
python3 services/cluster.py route '{"command": "inference.run", "requirements": {"gpu": true}}'
```

## Monitoring

### Real-Time Cluster Watch

```bash
python3 services/cluster.py monitor
```

Output:
```
  [2026-07-12T10:30:01Z] 3/3 nodes online, 2 active tasks
  [2026-07-12T10:30:31Z] 3/3 nodes online, 1 active tasks
  [ALERT] node_offline: machine-c
  [2026-07-12T10:31:01Z] 2/3 nodes online, 1 active tasks
```

### NATS Monitoring

The NATS HTTP monitor is available at `http://<nats-host>:8222/` when configured.

### Cluster Status Query

```bash
python3 services/cluster.py status
```

## Troubleshooting

### Nodes Can't See Each Other

1. Verify NATS is listening on `0.0.0.0:4222` (not just `127.0.0.1`)
2. Check firewall allows port 4222 between machines
3. Test connectivity: `nats-server --test` or `nc -vz <host> 4222`
4. Ensure all nodes use the same `NATS_URL`

### Nodes Going Offline Immediately

- Check `heartbeat_interval` — if too low, network latency may cause missed heartbeats
- Increase `offline_threshold` for unreliable networks
- Verify time sync between machines (`timedatectl status`)

### GPU Not Detected

- Ensure `nvidia-smi` is installed and works: `nvidia-smi --query-gpu=name --format=csv`
- The cluster uses `nvidia-smi` for GPU detection; AMD GPUs are not yet auto-detected
- Override manually in `cluster_config.yaml` under `node.capabilities.gpu`

### Task Routing Returns "no_available_node"

- All nodes may be at max capacity (`active_tasks >= max_tasks`)
- Check requirements match at least one node's capabilities
- Run `python3 services/cluster.py nodes` to verify node status

### High Latency Between Nodes

- Place NATS server on the same LAN as agent nodes
- Reduce `max_payload` if message sizes are small
- Consider NATS clustering (`nats-server --cluster`) for geographic distribution

## Integration with Agent Daemon

To enable multi-node task delegation in an agent daemon, add the cluster to its NATS subscriptions:

```python
# In agent_daemon.py, after connecting to NATS:
from services.cluster import ClusterManager, load_config

cluster_config = load_config()
cluster = ClusterManager(cluster_config)
await cluster.connect()
await cluster.register_node()
await cluster.setup_subscriptions()
```

The `delegate_to_agent` tool in `agents/tools.py` will automatically use the cluster manager for routing when connected.
