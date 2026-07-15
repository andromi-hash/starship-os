# Orchestrate Failover

Enterprise-grade failover orchestration for Starship OS cluster.

## Capabilities
- Detect node/agent failures via telemetry and NATS heartbeats
- Coordinate graceful failover: promote standby, reroute NATS subjects
- Restart failed services with systemd or direct
- Verify post-failover health (CPU/RAM/disk, agent status)
- Report summarized decision to memory

## Usage
- "orchestrate failover for proxy node"
- "trigger cluster failover on ergo failure"
- Integrated with StarAgent telemetry and cluster service.

## Dependencies
- NATS/JetStream
- systemd units for agents
- LanceDB memory for decisions
- Terminal + sudo where needed (sandboxed)
