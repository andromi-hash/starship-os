# Starship OS — Architecture & Dependency Map

> **Author**: Opencode / Agnetic Engineering
> **Version**: 1.0 (2026-07-09)
> **Purpose**: Complete reference for developers building on, extending, or deploying Starship OS.
> **Repository**: `https://github.com/andromi-hash/starship-os`

---

## Table of Contents

1. [What & Why: Architectural Decisions](#1-what--why-architectural-decisions)
2. [Full File Inventory](#2-full-file-inventory)
3. [Component Map](#3-component-map)
4. [Dependency Graph](#4-dependency-graph)
5. [NATS Subject Map](#5-nats-subject-map)
6. [Development Guide](#6-development-guide)
7. [Configuration Reference](#7-configuration-reference)
8. [Deployment Topology](#8-deployment-topology)
9. [Appendices](#9-appendices)

---

## 1. What & Why: Architectural Decisions

### 1.1 Why NATS for Inter-Agent Communication?

| Decision | Rationale |
|---|---|
| **NATS as message bus** | Lightweight, persistent (JetStream), pub/sub + request-reply. Agents are fully decoupled — no agent needs to know another agent's address or port. |
| **JetStream for history** | NATS JetStream provides exactly-once delivery, message replay, and configurable retention. Used for both agent communication history and telemetry archiving. |
| **Wildcard subjects** | Subject pattern `agnetic.agent.*.command.>` lets any agent subscribe to a single wildcard, making it trivial to add new agents without touching existing infrastructure. |

**Why not** REST/gRPC? — Agent-to-agent communication is event-driven and many-to-many. REST requires hardcoded endpoints; gRPC requires schema compilation. NATS subjects are runtime-discoverable and language-agnostic.

### 1.2 Why a File-Based Status Bridge (`/tmp/agnetic-status.json`)?

```
NATS ──→ agnetic-status.py ──→ /tmp/agnetic-status.json ──→ All 5 UI layers
```

| Reason | Detail |
|---|---|
| **Zero UI dependency on NATS** | GTK4 bridge, Cinnamon desklet, Conky, system tray — none need `nats-py`. They just read a JSON file. |
| **Pollable, not push** | UIs poll at their own refresh rate (2s–5s). This prevents overwhelming slow renderers (Cinnamon desklet JS, Conky Lua). |
| **Debuggable** | You can `cat /tmp/agnetic-status.json` at any time or pipe it to `jq` for debugging. |
| **Crash resilience** | If NATS goes down, the last written status file persists. UIs show stale data rather than blank screens. |

**Trade-off**: ~100ms stale data (2s poll worst case). Acceptable for system monitoring.

### 1.3 Why Ollama (Local LLM)?

| Concern | Resolution |
|---|---|
| **Privacy** | All inference runs locally. No data ever leaves the machine. |
| **Offline operation** | Zero cloud dependency. The entire system functions without internet. |
| **Model flexibility** | Each agent gets a different model/Modelfile. Proxy runs `qwen2.5:7b` (fast, small), Ergo runs `eve-v2-unleashed` (larger context, strategic). |
| **Cost** | Free inference at ~30 tokens/s on RTX 4050. |

### 1.4 Why Agent Specialization (Proxy / Romi / Ergo)?

Instead of one monolithic model, each agent has:

- **A distinct model** with its own Modelfile (context size, temperature, system prompt)
- **A distinct persona** defined in `souls/<name>/SOUL.md`
- **A distinct skill set** linked in `config.yaml`

| Agent | Model | Persona | Role |
|---|---|---|---|
| **Proxy** | `qwen2.5:7b` (32K ctx) | Security engineer + diagnostics | Red/blue team, system queries, troubleshooting |
| **Romi** | `qwen2.5:7b` (32K ctx, custom Modelfile) | Warm PA (Andromi) | User interface, NL interaction, code review |
| **Ergo** | `eve-v2-unleashed` (65K ctx) | Diplomatic strategist | Automation, scheduling, multi-agent orchestration |

**Why not one big model?** — Specialization lets us tune context window, temperature, and system prompt per role. Ergo gets 65K context for long-horizon planning; Proxy gets a lower temperature for factual diagnostics. Failures in one agent don't cascade.

### 1.5 Why Three-Persona Soul System?

Each agent has a `souls/<name>/SOUL.md` that defines its personality as a blend of fictional archetypes:

- **Proxy**: "Jarvis wit + Harper engineering + Tyr pragmatism + Beka realism"
- **Romi**: "Andromi — warm proactive PA, blends strategy + execution"
- **Ergo**: "Trance Gemini intuition + Dylan Hunt command + Andromeda precision"

This provides a *consistent behavioral framework* that the LLM uses to format responses, prioritize tasks, and interact with users and other agents.

### 1.6 Why Go for CLI, Rust for StarAgent, Python for Everything Else?

| Component | Language | Rationale |
|---|---|---|
| **CLI** (`cli/`) | Go | Single static binary, fast startup, excellent NATS client library (`nats.go`). Cobra provides CLI framework out of the box. |
| **StarAgent** (`agent/`) | Rust | Minimal footprint (~5MB binary), `sysinfo` crate for cross-platform metrics, `async-nats` for NATS. No runtime overhead. |
| **Agents, Dashboard, Scheduler** | Python | Speed of development. All need HTTP (Ollama API, aiohttp) + JSON processing + async I/O. Python's ecosystem is ideal for glue code. |

---

## 2. Full File Inventory

Every file in the repository with its purpose.

### 2.1 Root Level

| File | Purpose |
|---|---|
| `Makefile` | Top-level automation: `make build`, `make run-all`, `make install` |
| `CLAUDE.md` | Opencode/Cline context file — architecture summary, commands, conventions |
| `DESIGN.md` | Complete design language spec — color tokens, typography, UI component specs |
| `.gitignore` | Ignores binaries, build artifacts, logs, PID files |
| `bridge_console.py` | GTK4 native bridge console app — reads `status.json`, live-updates |
| `agnetic_tray.py` | GTK3 StatusIcon tray indicator — colored dot, agent menu, quick-launch |

### 2.2 `dashboard/` — Web UI Layer

| File | Purpose |
|---|---|
| `server.py` | aiohttp server (`:8899`). REST endpoints bridging NATS ↔ HTTP. Serves `index.html`. |
| `index.html` | Single-page dashboard (~750 lines). 3-column glass-panel UI. Polls `/api/status` every 3s. |

### 2.3 `agents/` — Agent Runtime

| File | Purpose |
|---|---|
| `agent_daemon.py` | Core daemon. Loads YAML config, subscribes `command.>`, calls Ollama, publishes responses. |
| `config.yaml` | Master configuration: agent definitions, NATS settings, dashboard settings, cron schedules. |
| `run_agent.sh` | Shell launcher: `start` / `stop` / `status` via PID files. |
| `scheduler.py` | Cron scheduler. Reads schedules from `config.yaml`, triggers `agnetic.workflow.<name>`. |
| `workflows.py` | Multi-agent workflow engine. Registered workflows: `security-audit`, `deploy`, `system-health`. |
| `proxy.yaml` | Proxy agent definition (model, role, skills, NATS subjects). |
| `romi.yaml` | Romi agent definition. |
| `ergo.yaml` | Ergo agent definition. |
| `orchestrator.yaml` | Orchestrator agent definition (reference). |
| `system-health-agent.yaml` | Sub-agent definition (reference). |
| `knowledge-agent.yaml` | Sub-agent definition (reference). |
| `Modelfile.ergo` | Ollama Modelfile for Ergo (base: `eve-v2-unleashed`, ctx: 65536, temp: 0.3). |
| `Modelfile.romi` | Ollama Modelfile for Romi (base: `qwen2.5:7b`, ctx: 32768, temp: 0.3). |

### 2.4 `agents/skills/` — Runner-Level Skill Definitions

| File | Purpose |
|---|---|
| `README.md` | Skill system documentation — format, triggers, dependencies. |
| `security-audit/SKILL.md` | Security audit skill (Proxy). Cron: Mon 6AM. Output: JSON audit report. |
| `code-review/SKILL.md` | Code review skill (Romi). On-demand. Output: JSON review report. |

### 2.5 `cli/` — Go CLI

| File | Purpose |
|---|---|
| `main.go` | Entry point. |
| `go.mod` | Go module definition (`nats.go`, `cobra`). |
| `completion.bash` | Bash tab completion (~500 lines). |
| `cmd/root.go` | Root command `agnetic`. |
| `cmd/agent.go` | `agnetic agent {run,status,stop}` |
| `cmd/ping.go` | `agnetic ping` — NATS connectivity test. |
| `cmd/skill.go` | `agnetic skill {list,show,trigger}` |
| `cmd/system.go` | `agnetic system health` |
| `cmd/telemetry.go` | `agnetic telemetry` — reads latest from NATS. |
| `cmd/version.go` | `agnetic version` |
| `cmd/workflow.go` | `agnetic workflow {run,list}` |

### 2.6 `scripts/` — Shell & Python Utilities

| File | Purpose |
|---|---|
| `start-agents.sh` | Boot all services: NATS → StarAgent → agents → dashboard → tray. |
| `start-dashboard.sh` | Dashboard boot: status bridge → Conky → tray → web server. |
| `start-tray.sh` | Launch `agnetic_tray.py` with PID tracking. |
| `apply-theme.sh` | Apply Cinnamon/GTK3/GTK4 theme via `gsettings` + symlinks. |
| `install-systemd.sh` | Copy systemd services to `/etc/systemd/system`, enable them. |
| `message_history.py` | JetStream consumer. Stores all `agnetic.agent.>` messages to `/tmp/agnetic-history/`. |
| `push-ci-workflows.py` | GitHub API — push CI workflows to all repos. |
| `query_agents.py` | Test script: NATS request-reply to each agent. |
| `setup-nats-auth.sh` | NATS auth setup + JetStream stream creation. |

### 2.7 `systemd/` — Systemd Service Units

| File | Purpose |
|---|---|
| `agnetic-nats.service` | Starts `nats-server` with production config. |
| `agnetic-staragent.service` | Starts StarAgent binary. BindsTo `nats`. |
| `agnetic-agents.service` | Starts `agent_daemon.py`. BindsTo `nats`. |
| `agnetic-dashboard.service` | Starts `dashboard/server.py`. After agents. |

### 2.8 `deploy/` — Advanced Systemd Units (Mesh)

| File | Purpose |
|---|---|
| `nats.service` | Alternative NATS service. |
| `staragent.service` | Alternative StarAgent service. |
| `agnetic-agent-mesh.target` | Target requiring NATS + StarAgent + agent template units. |
| `agnetic-agent@.service` | Template unit (`%i` = proxy/romi/ergo). |
| `agnetic-dashboard.target` | Target for dashboard stack. |
| `agnetic-dashboard-web.service` | Dashboard HTTP server. |
| `agnetic-status-bridge.service` | NATS→JSON bridge. |

### 2.9 `nats/` — NATS Configuration

| File | Purpose |
|---|---|
| `server.conf` | Production config: port 4222, JetStream (1GB mem / 10GB file), auth token, account permissions. |
| `agent-bus.conf` | Dev config: no auth, monitor port 8222, smaller JetStream limits. |
| `subjects.yaml` | NATS subject topology reference. |

### 2.10 `tray/` — System Tray Components

| File | Purpose |
|---|---|
| `agnetic-indicator.py` | GTK3 AppIndicator — reads `status.json`, colored icons, menu with commands. |
| `agnetic-status.py` | NATS→JSON bridge: subscribes status/telemetry/events, writes to `/tmp/agnetic-status.json`. |

### 2.11 `cinnamon/` — Cinnamon Desklet

| File | Purpose |
|---|---|
| `desklet.js` | Cinnamon desklet JS — polls `status.json`, displays agent rows + telemetry gauges. |
| `metadata.json` | Desklet UUID `agnetic-os@agnetic-os`. |
| `settings-schema.json` | Config: `refresh-interval` (1–60s, default 5). |
| `stylesheet.css` | Dark glass theme with Orbitron headers. |
| `link-desklet.sh` | Symlinks files into Cinnamon desklet directory. |

### 2.12 `conky/`

| File | Purpose |
|---|---|
| `agnetic.conkyrc` | Conky config — top-right overlay, agent status + telemetry via `execi python3 -c`. |

### 2.13 `skills/` — Skill Definitions

| File | Purpose |
|---|---|
| `ergo-automation/SKILL.md` | Ergo: scheduling, orchestration, automation, backup, reports. |
| `knowledge-store/SKILL.md` | Romi: document indexing, log archive, FTS5 search. |
| `proxy-diagnostics/SKILL.md` | Proxy: system diagnostics, log analysis, troubleshooting. |
| `romi-interface/SKILL.md` | Romi: NL understanding, task explanation, multi-turn conversation. |
| `system-health/SKILL.md` | Proxy: resource monitoring, process mgmt, alerting. |

### 2.14 `souls/` — Agent Persona Definitions

| File | Purpose |
|---|---|
| `proxy/SOUL.md` | Proxy persona: security engineer blend (Jarvis + Harper + Tyr + Beka). |
| `romi/SOUL.md` | Romi persona: Andromi — warm proactive PA. |
| `ergo/SOUL.md` | Ergo persona: Diplomatic strategist (Trance + Hunt + Andromeda). |

### 2.15 `agent/` — Rust StarAgent

| File | Purpose |
|---|---|
| `Cargo.toml` | Dependencies: `async-nats`, `sysinfo`, `tokio`, `serde`. |
| `src/main.rs` | Every 10s collects CPU, memory, disk, network → publishes `agnetic.telemetry`. |

### 2.16 `docs/`

| File | Purpose |
|---|---|
| `ARCHITECTURE.md` | Original architecture doc (predecessor to this file). |

### 2.17 `.github/workflows/`

| File | Purpose |
|---|---|
| `ci.yml` | CI pipeline: flake8 lint, gitleaks secret scan, trivy dependency scan, pytest placeholder. |

---

## 3. Component Map

### 3.1 High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           UI LAYER                                       │
│  ┌──────────┐  ┌────────────┐  ┌──────────┐  ┌───────┐  ┌───────────┐  │
│  │  Conky   │  │  Cinnamon  │  │  GTK4    │  │  Web  │  │   System  │  │
│  │ Desktop  │  │  Desklet   │  │  Bridge  │  │  Dash │  │   Tray    │  │
│  │ (conkyrc)│  │ (desklet.js)│  │(bridge_)│  │(index)│  │ (tray.py) │  │
│  └────┬─────┘  └─────┬──────┘  └────┬─────┘  └───┬───┘  └─────┬─────┘  │
│       └───────────────┴──────────────┴─────────────┴────────────┘      │
│                                   │ reads                                │
│                                   ▼                                      │
│                  ┌──────────────────────────────────┐                    │
│                  │ /tmp/agnetic-status.json         │                    │
│                  │ /tmp/agnetic-history/            │                    │
│                  └──────────┬───────────────────────┘                    │
├─────────────────────────────┼───────────────────────────────────────────┤
│                       BUS LAYER                                          │
│                  ┌──────────────────────┐                                │
│                  │    NATS/JetStream     │  :4222                        │
│                  │    agnetic.*         │                                │
│                  └──┬───────┬───────┬──┘                                │
│                     │       │       │                                    │
├─────────────────────┼───────┼───────┼──────────────────────────────────┤
│                  AGENT LAYER     │                                       │
│  ┌─────────┐ ┌─────────┐ ┌──────────┐ ┌──────────┐                     │
│  │  Proxy  │ │  Romi   │ │   Ergo   │ │StarAgent │                     │
│  │(qwen2.5)│ │(qwen2.5)│ │(eve-v2)  │ │ (Rust)   │                     │
│  │  agent_ │ │  agent_ │ │  agent_  │ │  every   │                     │
│  │ daemon  │ │ daemon  │ │  daemon  │ │   10s    │                     │
│  └────┬────┘ └───┬─────┘ └─────┬────┘ └──────────┘                     │
│       │          │              │                                       │
│       └──────────┴──────────────┘                                       │
│                       │                                                  │
│                  ┌────▼─────┐                                            │
│                  │  Ollama  │  :11434                                   │
│                  │  Server  │                                            │
│                  └──────────┘                                            │
└──────────────────────────────────────────────────────────────────────────┘
```

### 3.2 Data Flow Paths

| Data | Route | Latency | Persistence |
|---|---|---|---|
| **Telemetry** | StarAgent → NATS `agnetic.telemetry` → status bridge → `status.json` → all UIs | 2–10s | JetStream TELEMETRY |
| **Agent Command** | User (UI/CLI) → NATS `agnetic.agent.<name>.command.<cmd>` → Agent Daemon → Ollama → response → NATS reply → UI | 5–120s | JetStream AGENTS |
| **Agent Status** | Agent Daemon → NATS `agnetic.agent.<name>.status` → status bridge → `status.json` → all UIs | 1–5s | JetStream AGENTS |
| **Workflow** | Scheduler/User → NATS `agnetic.workflow.<name>` → Workflow Engine → multi-agent commands → collection → response | 30–300s | JetStream AGENTS |
| **Message History** | All NATS `agnetic.agent.>` → JetStream AGENTS stream → Message History Consumer → `/tmp/agnetic-history/` | <1s | File + JetStream |

### 3.3 Network Port Map

| Port | Service | Bind | Security |
|---|---|---|---|
| 4222 | NATS server | 127.0.0.1 | Token auth (production) |
| 8222 | NATS HTTP monitor | 127.0.0.1 | Dev only, no auth |
| 8899 | Web dashboard | 0.0.0.0 | Unsecured (LAN only) |
| 11434 | Ollama API | 127.0.0.1 | Unsecured (localhost) |

---

## 4. Dependency Graph

### 4.1 Runtime Dependency Graph

```
agnetic_tray.py (GTK3)
  ├── Python: gi (GTK3, AppIndicator3), cairo, json, os, signal, subprocess, threading, time
  ├── File: /tmp/agnetic-status.json
  └── System: libgtk-3, libayatana-appindicator3

bridge_console.py (GTK4)
  ├── Python: gi (GTK4), json, os, time, signal
  ├── File: /tmp/agnetic-status.json
  └── System: libgtk-4

dashboard/server.py (Web)
  ├── Python: aiohttp, nats-py, json, asyncio, logging, pathlib, datetime
  ├── NATS: 127.0.0.1:4222
  ├── File: /tmp/agnetic-status.json, /tmp/agnetic-history/
  └── External: nats-server

staragent (Rust binary)
  ├── Rust deps: async-nats, sysinfo, tokio, serde, serde_json, bytes
  ├── NATS: 127.0.0.1:4222
  └── Build: cargo, rustc 1.96.1

nats-server
  ├── Binary: /usr/local/bin/nats-server (or system package)
  ├── Config: nats/server.conf or nats/agent-bus.conf
  └── JetStream: AGENTS + TELEMETRY streams

agent_daemon.py (×3: proxy, romi, ergo)
  ├── Python: nats-py, httpx, PyYAML, asyncio, json, logging, pathlib, datetime
  ├── NATS: 127.0.0.1:4222
  ├── HTTP: 127.0.0.1:11434 (Ollama)
  ├── File: agents/*.yaml, skills/*/SKILL.md, /tmp/agnetic-status.json
  └── External: ollama server, qwen2.5:7b, eve-v2-unleashed

scheduler.py
  ├── Python: yaml, asyncio, json, pathlib, datetime, subprocess
  ├── File: agents/config.yaml
  └── NATS: publishes agnetic.workflow.<name>

workflows.py
  ├── Python: json, asyncio, logging, subprocess, datetime
  └── NATS: publishes agnetic.agent.*.command.* / subscribes replies

message_history.py
  ├── Python: nats-py, json, asyncio, logging, pathlib, datetime
  ├── NATS: subscribes agnetic.agent.> (JetStream, durable)
  └── File: /tmp/agnetic-history/<date>.jsonl

cinnamon/desklet.js
  ├── Cinnamon APIs: St, Desklet, Mainloop, Gio, Settings
  └── File: /tmp/agnetic-status.json

conky/agnetic.conkyrc
  ├── Binary: conky
  └── File: /tmp/agnetic-status.json (via python3 -c)

cli/agnetic (Go binary)
  ├── Go deps: nats.go v1.37.0, cobra v1.10.2, pflag
  ├── Build: go 1.22
  └── NATS: 127.0.0.1:4222
```

### 4.2 File Dependency Chain

All UI components ultimately depend on a single file:

```
/tmp/agnetic-status.json
  ├── Written by: tray/agnetic-status.py (NATS subscriber)
  ├── Read by: bridge_console.py, agnetic_tray.py, dashboard/server.py, desklet.js, conkyrc
  └── Schema: { agents: { <name>: { status, model, last_seen } }, telemetry: { cpu, memory, disk, net }, messages: [...] }
```

This means **adding a new UI layer** is trivial: write anything that reads `/tmp/agnetic-status.json` and renders it.

### 4.3 External Runtime Dependencies

| Dependency | Version | Role | Installation |
|---|---|---|---|
| **nats-server** | ≥2.10 | Message bus | `apt install nats-server` or binary download |
| **ollama** | ≥0.31 | LLM inference server | `curl -fsSL https://ollama.com/install.sh | sh` |
| **qwen2.5:7b** | 4.7 GB | Primary model (proxy, romi) | `ollama pull qwen2.5:7b` |
| **eve-v2-unleashed** | 3.4 GB | Strategic model (ergo) | `ollama pull jeffgreen311/eve-v2-unleashed-...` |
| **Python 3** | ≥3.10 | All Python components | System package |
| **Go** | ≥1.22 | CLI compilation | `apt install golang` |
| **Rust** | ≥1.70 | StarAgent compilation | `curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh` |
| **libgtk-3** | system | GTK3 tray indicator | `apt install libgtk-3-dev` |
| **libgtk-4** | system | GTK4 bridge console | `apt install libgtk-4-dev` |
| **Cinnamon Desktop** | ≥6.0 | Cinnamon desklet | Linux Mint / Pop!_OS Cinnamon session |
| **Conky** | system | Desktop overlay | `apt install conky` |

### 4.4 Python Package Dependencies (install via `pip install -r`)

All packages install into Hermes venv: `/home/tech/.hermes/hermes-agent/venv/`

| Package | Used By |
|---|---|
| `nats-py` | server.py, agent_daemon.py, agnetic-status.py, message_history.py, query_agents.py |
| `aiohttp` | dashboard/server.py |
| `httpx` | agents/agent_daemon.py (Ollama HTTP calls) |
| `PyYAML` | agents/agent_daemon.py, agents/scheduler.py |
| `PyGObject` (gi) | bridge_console.py, agnetic_tray.py, agnetic-indicator.py |

### 4.5 Build-Time Dependencies

| Artifact | Build Command | Input | Output |
|---|---|---|---|
| `cli/agnetic` | `cd cli && go build -o agnetic` | `main.go`, `cmd/*.go` | Go binary |
| `agent/target/release/staragent` | `cd agent && cargo build --release` | `src/main.rs`, `Cargo.toml` | Rust binary |
| `Modelfile.romi` model | `ollama create romi -f agents/Modelfile.romi` | `Modelfile.romi` | Ollama model |
| `Modelfile.ergo` model | `ollama create ergo -f agents/Modelfile.ergo` | `Modelfile.ergo` | Ollama model |

---

## 5. NATS Subject Map

### 5.1 Complete Subject Inventory

| Subject Pattern | Published By | Subscribed By | Payload | Frequency |
|---|---|---|---|---|
| `agnetic.telemetry` | StarAgent (Rust) | status-bridge, CLI telemetry | `{ cpu, memory_used, memory_total, disk_used, disk_total, rx_bytes, tx_bytes, timestamp }` | Every 10s |
| `agnetic.agent.<name>.command.>` | — | agent_daemon (wildcard) | — | N/A (subscription) |
| `agnetic.agent.<name>.command.<cmd>` | dashboard, workflows, CLI, scripts | agent_daemon | `{ command, args?, reply_to?, timestamp }` | On user/trigger |
| `agnetic.agent.<name>.status` | agent_daemon, CLI ping | status-bridge, message-history | `{ agent, status, command?, response?, timestamp }` | On status change |
| `agnetic.agent.<name>.event.*` | agent_daemon | status-bridge | `{ event, agent, data }` | On agent events |
| `agnetic.workflow.<name>` | scheduler, dashboard, CLI | workflows engine | `{ workflow, payload, reply_to? }` | On cron/user |
| `agnetic.workflow.<name>.<step>` | workflows engine | — | varies | Workflow steps |
| `agnetic.skill.<name>` | (future) | message-history | `{ skill, payload }` | Future use |
| `agnetic.history.latest` | message_history | dashboard (future) | `{ subject, data, timestamp }` | Per stored msg |
| `agnetic.reply.<ts>` | dashboard, scripts | dashboard, scripts | `{ agent, response, status, timestamp }` | Per command |

### 5.2 Subject Pattern Grammar

```
agnetic.agent.<agent>.command.<cmd>       # Publish: dashboard, workflows, CLI
                                            # Subscribe: agent_daemon (wildcard)
                                            
agnetic.agent.<agent>.status              # Publish: agent_daemon (after processing)
                                            # Subscribe: status bridge, message history

agnetic.agent.<agent>.event.<event>       # Publish: agent_daemon
                                            # Subscribe: status bridge

agnetic.workflow.<name>                   # Publish: scheduler, dashboard, CLI
                                            # Subscribe: workflows engine

agnetic.skill.<name>                      # Publish: future trigger system
                                            # Subscribe: message history
```

### 5.3 Agent Command Flow

```
User/CLI/Dashboard
    │
    │  (1) Publish to:
    ▼
agnetic.agent.<name>.command.<command>
    │
    │  (2) Agent Daemon receives (wildcard)
    ▼
Parse command → Build Ollama prompt → POST http://127.0.0.1:11434/api/generate
    │
    │  (3) Publish to:
    ├── agnetic.agent.<name>.status (for status bridge + all UIs)
    ├── Reply subject (if reply_to in payload)
    └── NATS reply inbox (if msg.reply from PublishRequest)
```

---

## 6. Development Guide

### 6.1 How to Add a New UI Layer

Any component that reads `/tmp/agnetic-status.json` and renders it is a valid UI layer. Example for a terminal-based UI:

```python
import json, time
while True:
    data = json.loads(open('/tmp/agnetic-status.json').read())
    os.system('clear')
    for name, agent in data.get('agents', {}).items():
        print(f"{name}: {agent.get('status', 'offline')}")
    time.sleep(2)
```

**No NATS dependency required.** Just poll the file.

### 6.2 How to Add a New Agent

1. **Create YAML definition**: `agents/<name>.yaml`
   ```yaml
   name: <name>
   model: <ollama-model>
   role: <role>
   skills:
     - <skill-name>
   ```
2. **Create Ollama Modelfile**: `agents/Modelfile.<name>` (optional — reuse existing model)
3. **Create soul**: `souls/<name>/SOUL.md`
4. **Register in config**: `agents/config.yaml` under `agents:`
5. **Create skill(s)**: `skills/<skill>/SKILL.md`
6. **No code changes needed** — `agent_daemon.py` is agent-agnostic. It reads the YAML and subscribes `agnetic.agent.<name>.command.>` automatically.

### 6.3 How to Add a New Skill

1. **Create directory**: `skills/<name>/SKILL.md`
2. **Format**:
   ```markdown
   # Skill: <name>

   ## Trigger
   - Subject: `agnetic.skill.<name>`

   ## Prompt
   You are <agent role>. <instruction>

   ## Output
   Format: json
   Schema: { ... }

   ## Dependencies
   - tool1, tool2
   ```
3. **Link to agent**: Add `<name>` to the agent's `skills:` list in `agents/config.yaml`.

### 6.4 How to Add a New Workflow

1. **Register in `agents/workflows.py`**:
   ```python
   @register("<name>")
   async def my_workflow(nc, payload):
       await nc.publish("agnetic.agent.proxy.command.do-thing", ...)
       await nc.publish("agnetic.agent.ergo.command.other-thing", ...)
       return {"status": "triggered", "workflow": "<name>"}
   ```
2. **Trigger** via:
   - NATS publish: `nats pub agnetic.workflow.<name> '{"workflow":"<name>"}'`
   - CLI: `agnetic workflow run <name>`
   - Dashboard: Workflow button
   - Cron: add schedule in `agents/config.yaml`

### 6.5 How to Extend the CLI

Add a new file in `cli/cmd/`:

```go
package cmd

import "github.com/spf13/cobra"

var myCmd = &cobra.Command{
    Use:   "mycommand",
    Short: "Does something useful",
    Run: func(cmd *cobra.Command, args []string) {
        // NATS connect → do work → output
    },
}

func init() {
    rootCmd.AddCommand(myCmd)
}
```

Then rebuild: `cd cli && go build -o agnetic`

### 6.6 How to Extend the Dashboard

The dashboard server (`dashboard/server.py`) is modular — add a new handler function and route:

```python
async def handle_something(request):
    # do work
    return web.json_response({"result": "ok"})

# In the app setup:
app.router.add_get("/api/something", handle_something)
```

### 6.7 How to Deploy (Production)

1. **Build**: `make build` (CLI binary)
2. **Install systemd services**: `sudo bash scripts/install-systemd.sh`
3. **Start services**:
   ```bash
   sudo systemctl start agnetic-nats
   sudo systemctl start agnetic-staragent
   sudo systemctl start agnetic-agents
   sudo systemctl start agnetic-dashboard
   ```
4. **Verify**: `agnetic ping` and open `http://localhost:8899`
5. **Enable at boot**: `sudo systemctl enable agnetic-*`

### 6.8 Development Quick Start

```bash
# 1. Start NATS
nats-server -c nats/agent-bus.conf &

# 2. Start StarAgent (telemetry)
cargo run --release --manifest-path agent/Cargo.toml &

# 3. Start agents
nohup python3 agents/agent_daemon.py > logs/agents.log 2>&1 &

# 4. Start message history
nohup python3 scripts/message_history.py > logs/message-history.log 2>&1 &

# 5. Start dashboard
nohup python3 dashboard/server.py > logs/dashboard.log 2>&1 &

# 6. Try the CLI
agnetic ping
agnetic agent status
agnetic telemetry
```

Or use the Makefile: `make run-all`

---

## 7. Configuration Reference

### 7.1 Configuration Files

| File | Format | Controls | Read By |
|---|---|---|---|
| `agents/config.yaml` | YAML | Master config: agent definitions (model, nats_url, enabled, skills, schedules), NATS settings, dashboard port/host | agent_daemon.py, scheduler.py |
| `agents/<name>.yaml` | YAML | Per-agent definition (model, role, skills, NATS subjects) | agent_daemon.py |
| `agents/Modelfile.<name>` | Ollama format | LLM parameters: base model, context size, temperature, system prompt | `ollama create <name> -f ...` |
| `nats/server.conf` | NATS config | Production NATS: port, auth token, JetStream limits, accounts, subject permissions | nats-server |
| `nats/agent-bus.conf` | NATS config | Dev NATS: no auth, smaller limits, HTTP monitor on :8222 | nats-server |
| `systemd/*.service` | systemd unit | ExecStart, After, BindsTo, User, Restart policy | systemd |
| `deploy/*.target` / `.service` | systemd unit | Mesh deployment: targets, template units, chained dependencies | systemd |
| `cinnamon/settings-schema.json` | JSON | Desklet refresh interval (1–60s, default 5) | Cinnamon |
| `conky/agnetic.conkyrc` | Lua | Conky: position, size, update interval, data sources, colors | conky |
| `.github/workflows/ci.yml` | YAML | CI: lint, gitleaks, trivy, pytest | GitHub Actions |
| `Makefile` | Makefile | Build, install, run, stop, clean targets | make |

### 7.2 Environment Variables

| Variable | Default | Used By | Purpose |
|---|---|---|---|
| `NATS_URL` | `nats://127.0.0.1:4222` | All NATS clients | NATS server address |
| `OLLAMA_URL` | `http://127.0.0.1:11434` | agent_daemon.py | Ollama API endpoint |
| `DASHBOARD_PORT` | `8899` | dashboard/server.py | HTTP listen port |
| `AGNETIC_ROOT` | auto-detect | Various | Project root directory |

### 7.3 Scheduled Cron Tasks

Defined in `agents/config.yaml` under each agent's `schedule:` list.

| Task | Schedule | Agent | Workflow |
|---|---|---|---|
| `nightly-health` | `0 2 * * *` (daily 2 AM) | Ergo | `system-health` |
| `weekly-audit` | `0 6 * * 1` (Monday 6 AM) | Ergo | `security-audit` |

### 7.4 JetStream Streams

| Stream | Subjects | Storage | Max Age | Max Msgs |
|---|---|---|---|---|
| AGENTS | `agnetic.agent.>`, `agnetic.workflow.>`, `agnetic.skill.>` | File | 72h | 1,000,000 |
| TELEMETRY | `agnetic.telemetry.>` | File | 24h | 500,000 |

---

## 8. Deployment Topology

### 8.1 Boot Sequence

```
1. nats-server (port 4222)
   └── Config: nats/server.conf (prod) or nats/agent-bus.conf (dev)

2. staragent (Rust binary)
   └── Publishes agnetic.telemetry every 10s

3. agent_daemon.py (3 daemons, or single with --agent flag)
   ├── proxy (subscribes proxy.command.>, calls qwen2.5:7b via Ollama)
   ├── romi  (subscribes romi.command.>, calls qwen2.5:7b via Ollama)
   └── ergo  (subscribes ergo.command.>, calls eve-v2-unleashed via Ollama)

4. agnetic-status.py (NATS → /tmp/agnetic-status.json bridge)

5. dashboard/server.py (port 8899, web UI + REST API)

6. message_history.py (JetStream consumer → /tmp/agnetic-history/)

7. UI Layer (all independent, all read /tmp/agnetic-status.json):
   ├── agnetic_tray.py (GTK3 system tray)
   ├── bridge_console.py (GTK4 native window)
   ├── conky (agnetic.conkyrc, desktop overlay)
   └── cinnamon desklet.js (Cinnamon desktop widget)

8. CLI (on-demand):
   └── agnetic (Go binary, connects NATS, runs command, exits)
```

### 8.2 Systemd Service Graph (Production)

```
multi-user.target
  └── agnetic-nats.service
        ├── agnetic-staragent.service  (BindsTo=nats)
        └── agnetic-agents.service     (BindsTo=nats)
              └── agnetic-dashboard.service  (After=agents)
```

### 8.3 Systemd Service Graph (Mesh/Advanced)

```
agnetic-agent-mesh.target
  ├── nats.service (Required)
  ├── staragent.service (Required)
  ├── agnetic-agent@proxy.service (Template)
  ├── agnetic-agent@romi.service (Template)
  ├── agnetic-agent@ergo.service (Template)
  └── agnetic-dashboard.target (BindsTo=mesh)
        ├── agnetic-status-bridge.service
        └── agnetic-dashboard-web.service
```

### 8.4 Process Ownership

| Process | User | Restart Policy | Dependencies |
|---|---|---|---|
| nats-server | tech | systemd: always, 3s | network |
| staragent | tech | systemd: always, 3s | nats |
| agent_daemon.py | tech | systemd: always, 5s | nats, ollama |
| dashboard/server.py | tech | systemd: always, 3s | nats, agents |
| message_history.py | tech | manual / systemd | nats |
| agnetic_tray.py | tech | autostart .desktop | status.json |

### 8.5 State / Data File Locations

| File | Format | Written By | Read By | Purpose |
|---|---|---|---|---|
| `/tmp/agnetic-status.json` | JSON | agnetic-status.py | ALL UI + dashboard API | Current system state |
| `/tmp/agnetic-history/<date>.jsonl` | JSONL | message_history.py | dashboard /api/history | Historical messages |
| `/tmp/nats-jetstream/` | NATS store | nats-server | nats-server | JetStream persistence |
| `agents/.proxy.pid` | PID | run_agent.sh | run_agent.sh, CLI | Process tracking |
| `agents/.romi.pid` | PID | run_agent.sh | run_agent.sh, CLI | Process tracking |
| `agents/.ergo.pid` | PID | run_agent.sh | run_agent.sh, CLI | Process tracking |
| `logs/*.log` | Text | various services | developers | Debugging |

---

## 9. Appendices

### 9.1 A. Architecture Diagram (ASCII)

```
                    ┌────────────────────────────────────────┐
                    │            OLLAMA INFERENCE             │
                    │         localhost:11434                 │
                    │  ┌─────────┐ ┌─────────┐ ┌──────────┐ │
                    │  │ qwen2.5 │ │ qwen2.5 │ │eve-v2    │ │
                    │  │ (proxy) │ │ (romi)  │ │ (ergo)   │ │
                    │  └────┬────┘ └────┬────┘ └─────┬────┘ │
                    └───────┼──────────┼─────────────┼───────┘
                            │          │             │
                    ┌───────▼──────────▼─────────────▼───────┐
                    │         AGENT DAEMON LAYER              │
                    │  ┌────────┐ ┌────────┐ ┌───────────┐   │
                    │  │ PROXY  │ │  ROMI  │ │   ERGO    │   │
                    │  │ Security│ │ User   │ │ Strategy  │   │
                    │  │ Engine │ │ Interface│ │Automation │   │
                    │  └────┬───┘ └───┬────┘ └─────┬─────┘   │
                    └───────┼─────────┼────────────┼─────────┘
                            │         │            │
                    ┌───────▼─────────▼────────────▼─────────┐
                    │           NATS MESSAGE BUS              │
                    │     agnetic.agent.* / agnetic.*       │
                    └───────┬─────────┬────────────┬─────────┘
                            │         │            │
              ┌─────────────┤         │            ├─────────────┐
              │             │         │            │             │
     ┌────────▼───┐  ┌─────▼─────┐   │   ┌────────▼───┐  ┌─────▼─────┐
     │  STATUS    │  │  MESSAGE  │   │   │  WORKFLOW  │  │ SCHEDULER │
     │  BRIDGE    │  │  HISTORY  │   │   │  ENGINE    │  │ (cron)    │
     │  (.py)     │  │  (.py)    │   │   │  (.py)     │  │  (.py)    │
     └─────┬──────┘  └───────────┘   │   └────────────┘  └───────────┘
           │                         │
           ▼                         ▼
   /tmp/agnetic-            ┌──────────────────┐
   status.json               │   WEB DASHBOARD  │
           │                 │  aiohttp :8899   │
           │                 └──────────────────┘
           │
     ┌─────┴──────────────────────────┐
     │         UI LAYER                │
     │  ┌──────┐ ┌──────┐ ┌────────┐ │
     │  │Tray  │ │Bridge│ │Conky │ │Desklet│ │
     │  │GTK3  │ │GTK4  │ │Lua   │ │Cinn. │ │
     │  └──────┘ └──────┘ └───────┘ └──────┘ │
     └───────────────────────────────────────┘
```

### 9.2 B. Quick Reference Card

| Command | Description |
|---|---|
| `agnetic ping` | Test NATS connectivity |
| `agnetic agent status` | Show all agent daemon statuses |
| `agnetic agent chat proxy` | Interactive chat with Proxy |
| `agnetic telemetry` | Show latest system telemetry |
| `agnetic workflow run security-audit` | Run security audit workflow |
| `agnetic skill list` | List installed skills |
| `make run-all` | Start all services (dev) |
| `nats pub agnetic.agent.ergo.command.status '{"command":"status"}'` | Send NATS command directly |

### 9.3 C. Color Tokens

| Token | Value | Usage |
|---|---|---|
| `--color-bg` | `#070B14` | Main background |
| `--color-surface` | `rgba(14,22,40,0.72)` | Glass panel background |
| `--color-primary` | `#7BC8E4` | Links, active states, telemetry |
| `--color-accent` | `#C4A97D` | Secondary highlights, Ergo |
| `--color-text` | `#E4DDD0` | Body text |
| `--color-success` | `#7BC8A4` | Agent online, nominal |
| `--color-warning` | `#D4A060` | Degraded, busy |
| `--color-alert` | `#D46060` | Offline, error |

### 9.4 D. File Size Metrics (approximate)

| Layer | Files | Total Lines |
|---|---|---|
| Python agents/scripts | ~15 | ~2,500 |
| Web dashboard | 2 | ~920 |
| Go CLI | 9 | ~1,000 |
| Cinnamon desklet | 5 | ~420 |
| Rust StarAgent | 2 | ~100 |
| NATS config | 3 | ~110 |
| Systemd units | 9 | ~200 |
| Skills/Souls docs | 10 | ~300 |
| Design/Architecture docs | 3 | ~700 |
| CI/Config | 3 | ~100 |
| **Total** | **~60** | **~6,400** |

---

> **End of Architecture & Dependency Map**
>
> This document should be kept in sync with the codebase. When adding a new component,
> update the relevant section: Component Map (§3), Dependency Graph (§4), or NATS Subject Map (§5).
