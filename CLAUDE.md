# Starship OS v2.2.0

A native AI operating system for complex system control. AI agents are first-class system services.

## Architecture

```
User/Opencode → Agent Team
                 ├── Romi (PA & Creative Strategist)
                 ├── Ergo (Diplomatic Strategist & CEO)
                 ├── Proxy (Red/Blue Security & Engineering)
                 └── StarAgent (Rust, system metric collection)
                        ↕ NATS/JetStream bus
                              ↕ Starship Dashboard (web C2)
```

## Agents

| Agent | Role | Model | Soul |
|---|---|---|---|
| **Romi** | Personal Assistant & Creative Strategist | `romi:latest` (Eve-V2 base, Ollama) | `Modelfile.romi` — warm, proactive, creative |
| **Ergo** | Diplomatic Strategist & CEO | `ergo:latest` (qwen2.5:7b base, Ollama) | `Modelfile.ergo` — orchestrator, planner |
| **Proxy** | Red/Blue Security & Engineering | `proxy:latest` (qwen35-claude-coder:9b base, Ollama) | `Modelfile.proxy` — security engineer |
| **StarAgent** | Cross-platform system monitor (Rust) | N/A (binary) | — |

Skills distributed from Hermes Agent catalog: Romi (creative, productivity, media), Ergo (planning, finance, blockchain, MCP), Proxy (coding agents, ML ops, security, devops). Health checker service monitors agent processes and model connectivity every 30s with auto-recovery. See `souls/*/SOUL.md` for full agent personalities.

## Dashboard Tabs

| Tab | Status | Backend |
|---|---|---|
| Overview | Live | `/api/dashboard` — agents, telemetry, GPU, Ollama |
| Crew Manifest | Live | `/api/agents` — agent YAML configs + pgrep |
| Fleet Map | Live | `/api/fleet` — fleet YAML + NATS exercise |
| Officer Check-In | Live | `/api/chat/stream` — Ollama SSE + tools |
| Connect | Live | `/api/agents`, `/api/agent/installer-info` — Simplex bridge |
| Incidents | Live | `/api/incidents` — down agents, stale nodes, resource pressure |
| Shield | Live | `/api/shield/stats` — NATS telemetry aggregator |
| Policy | Live | `/api/policy` — osquery pack configs (41 queries) |
| Memory | Live | `/api/memory` — 3D knowledge graph (Three.js) |
| Skills | Live | `/api/skills` — registry, security scores, proxy vetting |
| Telemetry Log | Live | `/api/telemetry/recent` — per-node telemetry snapshots |

## StarAgent (Rust)

- Cross-platform osquery telemetry agent (Linux x86_64, aarch64; Windows x86_64)
- Ships with osquery config: `starshipd.conf` (12 queries), security + compliance packs
- Self-service agent installer from dashboard Shield tab
- NATS token auth, 10s telemetry publish interval

## Communication

- NATS/JetStream bus
- Telemetry: `starship.telemetry.{hostname}.{table}`
- Commands: `starship.agent.{agent}.command.{command}`
- Dual-publish on legacy `agnetic.*` (Alpha 2.0 compat)

## Health Checker

- `scripts/agent-health-checker.py` — Persistent service (systemd or cron)
- Checks every 30s: agent process liveness, Ollama model availability, OpenRouter reachability
- Auto-recovery: restarts down agents, pulls missing models
- Status: `/var/lib/starship/health-status.json` or `/tmp/starship-health.json`
- Dashboard integrates into `/api/health` and `/api/incidents`

## Commands

- `ollama list` — verify local models
- `nats-server --version` — verify NATS
- `rustc --version` — verify Rust toolchain

## Conventions

- Configs: YAML in `agents/`
- Docs: Markdown in `docs/`
- CLI: Go/Cobra in `starshipctl/`
- Metrics agent: Rust in `agent/`
- NATS config: `nats/`
- Dashboard: Python + vanilla JS in `dashboard/`

## Version

2.1.0 → 2.2.0 (Phase 1: StarAgent + osquery telemetry)
