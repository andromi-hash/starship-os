# Security Architecture

Starship OS implements defense-in-depth security across all layers.

## Sandboxed Tool Execution

Agents execute tools through a sandboxed `CommandExecutor`:

| Rule | Description |
|------|-------------|
| Blocked commands | `rm -rf /`, `mkfs`, `dd if=`, `shutdown`, `reboot` |
| Privileged commands | `sudo`, `su`, `chmod 777`, `chown`, `passwd`, `useradd` |
| Path allowlists | Read: `/home`, `/tmp`, `/opt/agnetic`, `/etc/agnetic` |
| Path allowlists | Write: `/tmp`, `/opt/agnetic`, `/var/log/agnetic` |
| Max output | 50KB per tool call |
| Max file read | 1MB |
| Command timeout | 30 seconds default |

## NATS Authentication

Per-agent tokens with subject-level permissions:

```bash
# Generate tokens
python3 agents/security.py generate-tokens

# Generate config with auth
python3 agents/security.py generate-nats-conf > /etc/agnetic/nats/agent-bus.conf
```

### Permission Matrix

| Agent | Can Publish | Can Subscribe |
|-------|------------|---------------|
| proxy | `agnetic.agent.proxy.>`, `agnetic.telemetry.>` | `agnetic.agent.proxy.command.>`, `agnetic.telemetry.>` |
| romi | `agnetic.agent.romi.>`, `agnetic.telemetry.>` | `agnetic.agent.romi.command.>`, `agnetic.telemetry.>` |
| ergo | `agnetic.agent.ergo.>`, `agnetic.workflow.>`, `agnetic.telemetry.>` | `agnetic.agent.ergo.command.>`, `agnetic.workflow.>`, `agnetic.telemetry.>` |
| staragent | `agnetic.telemetry.>` | `system.>` |
| dashboard | `agnetic.agent.*.command.>`, `agnetic.workflow.>` | `agnetic.agent.>.status`, `agnetic.agent.>.event.>`, `agnetic.telemetry.>` |

## AppArmor Profiles

Three profiles enforce MAC restrictions:

### agnetic-agent
- Denies writes to `/home`, `/root`, `/etc` (except `/etc/agnetic`)
- Allows `/opt/agnetic/**`, `/tmp/**`, `/var/log/agnetic/**`
- Denies raw sockets, mount, ptrace, kernel module loading
- Allows TCP to localhost (NATS, Ollama)

### ollama
- Allows `/usr/bin/ollama`, `/usr/lib/ollama/**`
- GPU access via `/dev/nvidia*`, `/dev/dri/*`
- Denies writes to `/home`, `/etc` (except `/etc/ollama`)

### nats
- Allows `/usr/local/bin/nats-server`, `/etc/agnetic/nats/**`
- Allows `/var/lib/agnetic/nats/**`, `/var/log/agnetic/**`
- Denies writes to `/home`, `/root`, `/etc` (except `/etc/agnetic/nats`)

```bash
# Install profiles
sudo bash scripts/install-apparmor.sh
```

## Encrypted Configuration

AES-256-GCM encryption with PBKDF2 key derivation:

```python
from agents.security import SecretsManager

sm = SecretsManager(password="master-password")
sm.set("openai-api-key", "sk-...")
value = sm.get("openai-api-key")
```

## Secret Redaction

All tool output is automatically redacted before being sent to LLMs:

```
password=***REDACTED***
ghp_***REDACTED***
sk-***REDACTED***
```

## Systemd Hardening

All systemd units include:

```ini
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
RestrictNamespaces=true
```

## Recommendations

1. **Enable NATS auth** — Set `auth_enabled: true` in config and use generated tokens
2. **Install AppArmor** — Run `sudo bash scripts/install-apparmor.sh`
3. **Use encrypted config** — Store API keys with `SecretsManager`
4. **Run as non-root** — Systemd units use `User=agnetic`
5. **Limit network access** — AppArmor profiles restrict to localhost only
