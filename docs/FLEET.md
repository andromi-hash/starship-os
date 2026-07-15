# Starship OS — Fleet, Plants, Ops Manager, Red/Blue

**Status:** Alpha 2.1 scaffold  
**Config:** `config/fleet.yaml`  
**Service:** `services/fleet.py`  
**CLI:** `starshipctl fleet …`

## Model

```
Fleet
 ├── Ops Manager (aggregates status on starship.fleet.ops.*)
 ├── Plant Alpha     (production mesh)
 ├── Plant Edge      (thin nodes)
 └── Plant Range     (red/blue exercise, isolated)
       ├── red-team roles
       └── blue-team roles
```

| Concept | Meaning |
|---------|---------|
| **Fleet** | Named multi-plant deployment |
| **Plant** | Site/zone with profile + allowed roles |
| **Ops manager** | Node that publishes fleet summary heartbeats |
| **Red team** | Offensive exercise role (restricted tools) |
| **Blue team** | Defensive exercise role |

Cluster mesh (`services/cluster.py`) remains the low-level node/task router.  
Fleet is the **topology + exercise** control plane on top.

## NATS subjects (dual-publish)

| Subject | Purpose |
|---------|---------|
| `starship.fleet.register` | Node registration |
| `starship.fleet.heartbeat` | Node heartbeat |
| `starship.fleet.status` | Node status snapshot |
| `starship.fleet.ops.status` | Ops manager aggregate |
| `starship.fleet.exercise` | Exercise start/stop events |

Legacy `agnetic.fleet.*` is dual-published for Alpha 2.0 clients.

## CLI

```bash
starshipctl fleet status
starshipctl fleet plants
starshipctl fleet register
starshipctl fleet nodes
starshipctl fleet exercise start
starshipctl fleet exercise stop
starshipctl fleet exercise status

# or directly
python3 services/fleet.py daemon
```

## Node override

`/etc/starship/fleet-node.yaml`:

```yaml
node:
  plant: plant-edge
  roles: [proxy, plant-controller]
  team: ops
  profile: edge
```

## Red/blue policy notes

- Exercises default to `plant-range` (`isolation: true`).
- Red-team never gets unrestricted OpenCode (enforced in `agents/fleet_policy.py` + toolsets `red_team` / `security_audit`).
- Red-team allowed tools: `read_file`, `list_dir`, `search_files`, `http_get`, `delegate_to_agent`.
- Set identity via env: `STARSHIP_FLEET_TEAM=red` `STARSHIP_FLEET_ROLES=red-team` or `/etc/starship/fleet-node.yaml`.

## Cross-plant ACL

Config in `config/fleet.yaml` → `acl`:

```yaml
acl:
  default: same_plant_only   # same_plant_only | deny | allow
  allow:
    plant-alpha: [plant-edge]
    plant-edge: [plant-alpha]
    plant-range: []
```

Enforced by `fleet_policy.check_cross_plant` / `check_tool(..., target_plant=...)`:

1. Same plant → allow  
2. Red-team during exercise → deny all cross-plant  
3. Source or target `isolation: true` → deny  
4. Explicit `acl.allow[source]` list  
5. Default fail-closed (`same_plant_only` / `deny`)

`delegate_to_agent` accepts `plant` / `target_plant` for ACL checks.

## Multi-node NATS auth

| File | Purpose |
|------|---------|
| `nats/agent-bus.conf` | Dev/server/edge — auth disabled, localhost |
| `nats/fleet-bus.conf` | Ops multi-node — token placeholder + dual-prefix |
| `nats/fleet-auth.yaml` | Role → subject allow map |
| `/etc/starship/nats/active.conf` | Symlink to agent-bus or fleet-bus.active |
| `/etc/starship/nats.env` | `STARSHIP_NATS_TOKEN` + `NATS_URL` for clients |

```bash
# Manual multi-node fleet bus
export STARSHIP_NATS_TOKEN=$(openssl rand -hex 24)
# firstboot materializes this automatically on ops profile
nats-server -c /etc/starship/nats/active.conf
export NATS_URL=nats://127.0.0.1:4222
python3 services/fleet.py daemon
```

Fleet daemon injects token into URL when `STARSHIP_NATS_TOKEN` is set.  
Heartbeats dual-publish `starship.fleet.heartbeat` + `agnetic.fleet.heartbeat`.

## Firstboot (ops profile → fleet-bus)

`scripts/starship-firstboot.sh`:

| Profile | NATS mode | Auth |
|---------|-----------|------|
| edge | agent-bus | none |
| server | agent-bus | none (override: `STARSHIP_FLEET_BUS=1`) |
| **ops** | **fleet-bus** | token in `/etc/starship/nats-token` + `nats.env` |

Ops firstboot also:
1. Writes `/etc/starship/fleet-node.yaml` + copies `fleet.yaml`
2. Generates token (or uses `STARSHIP_NATS_TOKEN`)
3. Materializes `fleet-bus.active.conf` (replaces `__STARSHIP_NATS_TOKEN__`)
4. Points `active.conf` → fleet-bus; enables `agnetic-nats` + `starship-fleet`
5. Optional cluster: `STARSHIP_NATS_ROUTES='nats-route://peer:6222'` in `firstboot.env`

```bash
# Force fleet-bus on server/edge
STARSHIP_FLEET_BUS=1 sudo bash scripts/starship-firstboot.sh
# Ops autoinstall
STARSHIP_PROFILE=ops sudo bash scripts/starship-firstboot.sh
```

## Dashboard

- Plant map: `GET /api/fleet` · `GET /api/fleet/plants`
- Exercise: `POST /api/fleet/exercise` `{"action":"start"|"stop"}`
- Register: `POST /api/fleet/register`
- UI panel **Fleet Map** + Exercise Start/Stop buttons (port 8788)

## Next

- NATS accounts/nkeys for untrusted multi-tenant
