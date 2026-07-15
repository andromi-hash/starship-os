# C11 agent runtime

See **ADR 0001**: `docs/adr/0001-c11-agent-runtime.md`

## Layout

| Path | Purpose |
|------|---------|
| `sandbox_spike/` | Authorized spike: `sandbox_run` fork+exec + deny list |
| (future) `starshipd/` | Agent loop + NATS |
| (future) `policyexec/` | seccomp + policy gate |
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
# optional: export STARSHIP_SANDBOX_RUN=/path/to/sandbox_run
```

Python `CommandExecutor` then routes shell tools through `agents/sandbox_native.py`.

Python agent_daemon remains the control plane for Alpha 2.1.
