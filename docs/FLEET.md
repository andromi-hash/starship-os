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
| `nats/fleet-bus.conf` | Shared token (trusted LAN) |
| `nats/fleet-accounts.conf.tmpl` | Multi-tenant accounts template |
| `scripts/gen-nats-accounts.sh` | Materialize accounts + nkeys + client envs |
| `nats/fleet-auth.yaml` | Role → account / subject map |
| `agents/nats_connect.py` | Client helper (user/pass / token / nkey) |
| `/etc/starship/nats/active.conf` | Symlink to active server conf |
| `/etc/starship/nats.env` | Client credentials for fleet daemon |

### Modes

| Mode | When | Auth |
|------|------|------|
| `agent` | edge/server default | none |
| `token` | `STARSHIP_NATS_MODE=token` or fleet-bus only | shared `STARSHIP_NATS_TOKEN` |
| **`accounts`** | **ops firstboot default** | per-role user/pass + optional nkeys |

```bash
# Generate multi-tenant accounts (ops)
bash scripts/gen-nats-accounts.sh --out /etc/starship/nats
nats-server -c /etc/starship/nats/fleet-accounts.conf
set -a; source /etc/starship/nats/creds/ops.env; set +a
python3 services/fleet.py daemon
```

Accounts: `STARSHIP_OPS` · `STARSHIP_EDGE` · `STARSHIP_RANGE` (red/blue) · `STARSHIP_TELEM` · `SYS`  
Nkeys: optional (`nk` from `go install github.com/nats-io/nkeys/nk@latest`) → `creds/*.nk`  
Heartbeats dual-publish `starship.fleet.heartbeat` + `agnetic.fleet.heartbeat`.

### Optional TLS

```bash
bash scripts/gen-nats-tls.sh --out /etc/starship/nats/tls --host ops.example
# firstboot: STARSHIP_NATS_TLS=1 STARSHIP_PROFILE=ops sudo bash scripts/starship-firstboot.sh
```

Appends `tls { ... }` to fleet-accounts conf; clients use `STARSHIP_NATS_CA` + `tls://` via `nats_connect.py`.

## Firstboot

`scripts/starship-firstboot.sh`:

| Profile | NATS mode | Auth |
|---------|-----------|------|
| edge | agent-bus | none |
| server | agent-bus | none |
| **ops** | **accounts** | multi-tenant (`gen-nats-accounts.sh`) |

Overrides:
- `STARSHIP_NATS_ACCOUNTS=1` — force accounts on any profile  
- `STARSHIP_NATS_MODE=token` + `STARSHIP_FLEET_BUS=1` — shared token fleet-bus  
- `STARSHIP_NATS_ROUTES=...` — cluster routes (token mode)

```bash
STARSHIP_PROFILE=ops sudo bash scripts/starship-firstboot.sh
```

## Dashboard

- Plant map: `GET /api/fleet` · `GET /api/fleet/plants`
- Exercise: `POST /api/fleet/exercise` `{"action":"start"|"stop"}`
- Register: `POST /api/fleet/register`
- UI panel **Fleet Map** + Exercise Start/Stop buttons (port 8788)
