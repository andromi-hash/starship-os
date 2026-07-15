# Security Architecture — Starship OS 2.1

Defense-in-depth across tools, policy, message bus, packaging, and OS confinement.  
Install roots: **`/opt/starship`**, **`/etc/starship`**, **`/var/lib/starship`**  
(legacy `/opt/agnetic` symlinks for Alpha 2.0 compatibility).

## Threat model (Alpha / Beta)

| Asset | Risk | Mitigations |
|-------|------|-------------|
| Shell / tool execution | Agent RCE, data wipe | Sandbox blocklists, C11 seccomp, path allowlists |
| Untrusted red-team agents | Lateral movement | Fleet ACL, tool allowlists, isolated plant-range |
| NATS bus | Spoofed commands | Accounts/nkeys, token auth, optional TLS |
| Secrets in logs/LLM context | Credential leak | Redaction patterns, gitignore, SecretsManager |
| Abliterated local models | Weaker refusal | Mandatory policy + sandbox + Droid Shield |

## Sandboxed tool execution

Agents run tools through `CommandExecutor` (`agents/tools.py`):

| Rule | Description |
|------|-------------|
| Blocked commands | `mount`, `mkfs`, `dd`, `shutdown`, `reboot`, destructive patterns |
| Privileged commands | `sudo`, `su`, `chmod 777`, `chown`, `passwd`, `useradd` |
| Path allowlists | Prefer `/opt/starship`, `/etc/starship`, `/tmp`, `/var/log/starship` |
| Max output | 50KB per tool call |
| Timeout | 30s default |

### Optional C11 isolation

```bash
export STARSHIP_SANDBOX_NATIVE=1   # sandbox_run (seccomp + NEWNS/NEWPID)
export STARSHIP_POLICY_NATIVE=1    # policyexec shared JSON gate
export STARSHIP_POLICY=/etc/starship/policy.json
```

| Binary | Role |
|--------|------|
| `sandbox_run` | fork+exec, seccomp-bpf allowlist, best-effort namespaces |
| `policyexec` | `check-tool` / `check-command` against `policy.default.json` |

Shared policy contract: `config/policy.default.json` → packaged as `/etc/starship/policy.json`.

## Fleet red / blue policy

`agents/fleet_policy.py` + `config/fleet.yaml` ACL:

- **Red-team:** tools limited (`read_file`, `list_dir`, `search_files`, `http_get`, `delegate_to_agent`); never unrestricted OpenCode
- **Cross-plant:** fail-closed; `plant-range` isolation during exercises
- Identity: `/etc/starship/fleet-node.yaml` or `STARSHIP_FLEET_TEAM` / `STARSHIP_FLEET_ROLES`

## NATS authentication

| Mode | When | How |
|------|------|-----|
| **agent-bus** | edge/server dev | No auth, localhost (`nats/agent-bus.conf`) |
| **token** | trusted LAN | `STARSHIP_NATS_TOKEN` + `fleet-bus.conf` |
| **accounts** | ops firstboot default | Multi-tenant `STARSHIP_OPS` / `EDGE` / `RANGE` / `TELEM` |
| **TLS** | optional | `STARSHIP_NATS_TLS=1` + `scripts/gen-nats-tls.sh` |

```bash
# Generate multi-tenant accounts + optional nkeys
bash scripts/gen-nats-accounts.sh --out /etc/starship/nats
# Clients: source /etc/starship/nats.env  (or creds/ops.env)
```

Dual-publish subjects: `starship.*` (primary) + `agnetic.*` (legacy).  
Python helper: `agents/nats_connect.py` (user/pass, token, nkey, TLS).

### Subject permission sketch (accounts mode)

| Role | Account | Publish (examples) |
|------|---------|-------------------|
| ops | STARSHIP_OPS | `starship.>`, `agnetic.>` |
| edge | STARSHIP_EDGE | fleet heartbeat/register, proxy, telemetry |
| red | STARSHIP_RANGE | proxy + fleet heartbeat only |
| telem | STARSHIP_TELEM | `starship.telemetry.>` only |

## AppArmor

Profiles under `security/apparmor/` (install: `sudo bash scripts/install-apparmor.sh`):

| Profile | Scope |
|---------|-------|
| agent | Denies raw sockets, mount, ptrace; limits writes outside starship trees |
| ollama | GPU + model dirs; restricted FS |
| nats | Conf + JetStream store + logs only |

Paths should be updated to `/etc/starship` / `/opt/starship` when loading on 2.1 hosts.

## Encrypted configuration & secrets

```python
from agents.security import SecretsManager
sm = SecretsManager(password="master-password")
sm.set("api-key", "…")
```

- AES-256-GCM + PBKDF2
- Never commit: `*.key`, `*.pem`, `.env`, `credentials/`, `nats/creds/`, `nats/tls/`

## Secret redaction

Tool output redacts before LLM context:

```
password=***REDACTED***
ghp_***REDACTED***
sk-***REDACTED***
```

## Systemd hardening

Units under `systemd/` use:

```ini
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
# plus ProtectKernel*, RestrictSUIDSGID where applicable
EnvironmentFile=-/etc/starship/nats.env
User=agnetic   # nats user for message bus
```

## Packaging trust boundary

```bash
make deb
sudo dpkg -i dist/starship-os_*.deb
```

- Layout: `/opt/starship`, `/etc/starship` (validated in `scripts/build-deb.sh`)
- postinst creates users `agnetic` / `nats`, venv, enables units
- Firstboot (ops): multi-tenant NATS accounts + optional native sandbox

## Recommendations

1. **Ops / multi-node:** accounts mode + TLS; never share red-team credentials with ops
2. **Enable native gates:** `STARSHIP_SANDBOX_NATIVE=1` and `STARSHIP_POLICY_NATIVE=1`
3. **Install AppArmor** on bare metal
4. **Run agents as non-root** (`User=agnetic`)
5. **Rotate** NATS tokens/passwords after firstboot; store only under `/etc/starship/nats/creds` (mode 600)
6. **Abliterated models:** treat as untrusted reasoners — policy + sandbox mandatory

## Reporting

See root [`SECURITY.md`](../SECURITY.md) for supported versions and vulnerability reporting.
