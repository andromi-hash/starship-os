# C11 agent runtime

See **ADR 0001**: `docs/adr/0001-c11-agent-runtime.md`

## Layout

| Path | Purpose |
|------|---------|
| `sandbox_spike/` | `sandbox_run` — fork+exec, seccomp, namespaces |
| `policyexec/` | Shared policy JSON gate (tools + commands) |
| (future) `starshipd/` | Agent loop + NATS |
| (future) `heald/` | Self-healing watchdog |

## Build spike

```bash
make -C src/c/sandbox_spike
make -C src/c/sandbox_spike test
make bench          # ADR 0001 p50 timing vs Python
```

## Optional native tool path

```bash
export STARSHIP_SANDBOX_NATIVE=1
export STARSHIP_POLICY_NATIVE=1
# optional: STARSHIP_POLICY=config/policy.default.json
make -C src/c/policyexec all test
```

- `sandbox_run` — isolation (seccomp/NS)
- `policyexec` — allow/deny tools & commands from shared JSON
- Python: `agents/sandbox_native.py`, `agents/policy_native.py`

Python agent_daemon remains the control plane for Alpha 2.1.
