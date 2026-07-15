# Starship OS

A native AI operating system for complex system control. AI agents are first-class system services.

## Architecture

```
User/Opencode → Hermes Orchestrator
                 ├── Proxy (tech diagnostics, troubleshooting)
                 ├── Romi (user interface, natural language)
                 ├── Ergo (automation, scheduled tasks)
                 └── StarAgent (Rust, system metric collection)
                        ↕ NATS/JetStream bus
```

## Agents

| Agent | Role | Model |
|---|---|---|
| **Proxy** | Tech diagnostics, system queries | `qwen2.5:7b` (ollama) |
| **Romi** | User-facing interface, NL interaction | `qwen2.5:7b` (ollama) |
| **Ergo** | Automation, scheduled workflows | `jeffgreen311/eve-v2-unleashed-qwen3.5-8b-liberated-4k-4b-merged` (ollama) |
| **StarAgent** | Cross-platform system monitor (Rust) | N/A (binary) |

## Communication

- **NATS/JetStream** bus at `starship.agent.{proxy,romi,ergo}.{command,event,status}`
- **Telemetry** at `starship.telemetry.{cpu,mem,disk,net}`
- Dual-publish also on legacy `agnetic.*` (Alpha 2.0 compat)
- All agents communicate via NATS subjects; no direct coupling

## Commands

- `ollama list` — verify local models
- `ollama run qwen2.5:7b` — test primary model
- `nats-server --version` — verify NATS
- `go version` / `rustc --version` — verify toolchains
- `hermes` — Hermes Agent CLI (configured for Ollama)

## Conventions

- Configs: YAML in `agents/`
- Skills: Markdown in `skills/<name>/SKILL.md`
- Docs: Markdown in `docs/`
- CLI: Go/Cobra in `starshipctl/`
- Metrics agent: Rust in `agent/`
- NATS config: `nats/`

## State

- Hermes Agent v0.18.2, Ollama v0.31.1
- Models: qwen2.5:7b (4.7 GB), Eve-V2 (3.4 GB)
- NATS v2.10.7, Go 1.22.2, Rust 1.96.1
- No cloud dependencies; all local
