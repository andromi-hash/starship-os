# Starship OS

**An AI agent-first operating system built on Ubuntu 24.04 LTS**  
**Version:** 2.1.0 · **Canonical repo:** [andromi-hash/starship-os](https://github.com/andromi-hash/starship-os)

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Platform: Ubuntu 24.04](https://img.shields.io/badge/Platform-Ubuntu%2024.04-orange.svg)](https://ubuntu.com)
[![NATS/JetStream](https://img.shields.io/badge/Bus-NATS%2FJetStream-green.svg)](https://nats.io)
[![Ollama GPU](https://img.shields.io/badge/Inference-Ollama%20GPU-red.svg)](https://ollama.com)
[![Version](https://img.shields.io/badge/version-2.1.0-purple.svg)](VERSION)
[![Security](https://img.shields.io/badge/Security-Policy-red.svg)](SECURITY.md)

Starship OS is a local-first, AI-native OS layer where autonomous agents communicate over NATS/JetStream, execute tools in a sandboxed environment, and present a real-time command-and-control dashboard. No cloud required. Everything runs on your hardware.

**Lineage:** Alpha scaffold → Alpha 2.0 ([agnetic-os](https://github.com/andromi-hash/agnetic-os) archive) → **Alpha 2.1 / Beta** (this monorepo).

### 2.1.0 highlights

- **Install roots:** `/opt/starship`, `/etc/starship` (legacy `/opt/agnetic` symlinks)
- **CLI:** `starshipctl` (compat `agneticctl`) · **Dashboard:** `:8788`
- **Fleet:** multi-plant topology, red/blue policy, cross-plant ACL, exercise UI
- **NATS:** dual-prefix `starship.*` / `agnetic.*`; ops multi-tenant accounts + nkeys; optional TLS
- **C11 path:** `sandbox_run` (seccomp/namespaces), `policyexec` (shared policy JSON)
- **Packaging:** `make deb` → `dist/starship-os_*.deb`; ISO autoinstall edge/server/ops
- **Models:** Eve-V2-Unleashed default (`num_ctx=16384`) — `config/models.yaml`
- **Plan:** `docs/plans/starship-os-streamline.md` · **Security:** [`SECURITY.md`](SECURITY.md)

---

## Quick Start

```bash
# Dev mesh (user-level)
make dev && make status
make smoke          # 40+ checks

# Default model alias (after ollama pull of upstream)
ollama create Eve-V2-Unleashed -f config/models/Eve-V2-Unleashed.Modelfile

# Debian package
make deb
sudo dpkg -i dist/starship-os_*.deb
STARSHIP_PROFILE=ops sudo /opt/starship/bin/starship-firstboot.sh
```

Open `http://localhost:8788`. CLI: `starshipctl fleet status`.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  User / OpenCode     Dashboard :8788      starshipctl CLI    │
└───────────┬──────────────────┬───────────────────┬───────────┘
            └──────────────────┼───────────────────┘
                               │
            ┌──────────────────┴──────────────────┐
            │   NATS/JetStream  starship.* (+ agnetic.*)
            │   accounts / token / TLS (ops)       │
            └──┬──────────┬──────────┬────────────┘
               │          │          │
          ┌────┴───┐ ┌────┴───┐ ┌────┴───┐  ┌──────────┐
          │ Proxy  │ │  Romi  │ │  Ergo  │  │ StarAgent│
          │ qwen   │ │ qwen   │ │ Eve-V2 │  │  (Rust)  │
          └────┬───┘ └────┬───┘ └────┬───┘  └────┬─────┘
               └──────────┼──────────┘           │
                    Ollama :11434          telemetry
               ┌──────────┴──────────────────────┐
               │ Tools: sandbox + policyexec     │
               │ Fleet ACL · AppArmor · secrets  │
               └─────────────────────────────────┘
```

---

## Components

| Component | Language | Description |
|-----------|----------|-------------|
| **Agents** | Python | Proxy, Romi, Ergo — NATS + Ollama tool loops |
| **starshipctl** | Go/Cobra | CLI (agents, fleet, telemetry, workflows); `agneticctl` symlink |
| **StarAgent** | Rust | CPU/mem/disk/net → `starship.telemetry` (dual-publish) |
| **Dashboard** | Python + JS | C2 UI :8788 — agents, chat SSE, fleet map, exercise controls |
| **Fleet** | Python | Plants, ops manager, red/blue, register/heartbeat |
| **C11** | C | `sandbox_run`, `policyexec` — optional native isolation |
| **Tool system** | Python | Sandboxed tools, toolsets, redaction, auto-repair |

---

## Agents

### Proxy — Tech Diagnostics & Security

| Field | Value |
|-------|-------|
| Model | `qwen2.5:7b` |
| Role | Tech agent — system diagnostics, troubleshooting, security audits |
| Skills | `system-health`, `proxy-diagnostics` |
| Personality | Red/blue team security engineer. Dry wit, calm precision, relentless hardening. Think Jarvis meets Harper. |

Proxy handles the hard, iterative, security-critical work. It runs system diagnostics, scans logs, manages processes, and performs red-team attack simulation with blue-team hardening.

### Romi — User Interface & Natural Language

| Field | Value |
|-------|-------|
| Model | `qwen2.5:7b` |
| Role | Client agent — user-facing interface, natural language interaction |
| Skills | `knowledge-store`, `romi-interface` |
| Personality | Warm, proactive personal assistant. Blends strategy, diplomacy, and technical execution with genuine warmth. Think Andromeda Ascendant. |

Romi is the user's primary interface. It interprets natural language requests, explains complex operations, maintains user preferences, and delegates technical work to Proxy or Ergo.

### Ergo — Automation & Orchestration

| Field | Value |
|-------|-------|
| Model | `jeffgreen311/eve-v2-unleashed-qwen3.5-8b-liberated-4k-4b-merged` |
| Role | Automation agent — scheduled tasks, workflow orchestration |
| Skills | `ergo-automation` |
| Personality | Central coordinating intelligence. Calm, precise, warmly diplomatic. Synthesizes perspectives into coherent strategy. |

Ergo is the CEO. It orchestrates multi-agent workflows, manages scheduled tasks via cron, delegates engineering to Proxy, and maintains strategic alignment across the system.

### StarAgent — System Telemetry (Rust)

| Field | Value |
|-------|-------|
| Language | Rust (async-nats, sysinfo, tokio) |
| Role | Cross-platform system monitor |
| Publishes | `starship.telemetry` (+ legacy `agnetic.telemetry`) every 10 seconds |

StarAgent is a standalone Rust binary that collects CPU usage, memory, disk, and network I/O, then publishes JSON telemetry to the NATS bus. No LLM required — pure systems code.

---

## Tool System

### Available Tools

| Tool | Description | Parameters |
|------|-------------|------------|
| `shell` | Execute a shell command | `command`, `timeout` |
| `read_file` | Read file contents | `path`, `lines` (optional) |
| `write_file` | Write content to a file | `path`, `content` |
| `list_dir` | List directory entries | `path` |
| `http_get` | HTTP GET request | `url`, `headers` (optional) |
| `http_post` | HTTP POST request | `url`, `body`, `headers` (optional) |
| `search_files` | Glob search or grep content | `pattern`, `path`, `content` (optional) |
| `delegate_to_agent` | Delegate task to another agent | `agent`, `command`, `args` (optional) |

### Toolsets

Toolsets compose tools into named groups for fine-grained access control:

| Toolset | Tools | Use Case |
|---------|-------|----------|
| `core` | shell, read_file, write_file, list_dir, search_files | Basic filesystem and shell operations |
| `network` | http_get, http_post | HTTP requests and API calls |
| `delegation` | delegate_to_agent | Multi-agent task delegation |
| `full` | All tools (core + network + delegation) | Complete agent access |
| `readonly` | read_file, list_dir, search_files, http_get | Read-only operations |
| `webhook_safe` | read_file, list_dir, search_files, http_get | Safe for untrusted input |

### Sandbox

The tool system enforces strict security constraints:

- **Blocked commands**: `rm -rf /`, `mkfs`, `dd`, `shutdown`, `reboot`, and other destructive operations
- **Privileged commands**: `sudo`, `su`, `chmod 777`, `chown`, `passwd` are denied
- **Path restrictions**: Prefer `/opt/starship`, `/etc/starship`, `/tmp`, `/var/log/starship` (legacy agnetic paths still accepted where configured)
- **Secret redaction**: Passwords, tokens, API keys automatically redacted from output
- **Timeout enforcement**: 30-second default with process kill on timeout
- **Output limits**: 50KB max output, 1MB max file size
- **Optional C11:** `STARSHIP_SANDBOX_NATIVE=1` · `STARSHIP_POLICY_NATIVE=1`

---

## Dashboard

The web dashboard at `http://localhost:8788` provides a real-time command center for the entire agent mesh.

### Features

- **Crew Manifest**: Live agent status with online/offline indicators, model info, and running state
- **Communications Hub**: Real-time chat with any agent via SSE streaming (Server-Sent Events)
- **Fleet Map**: Plants, exercise start/stop, node register (`/api/fleet`)
- **Telemetry Gauges**: CPU, RAM, Disk, Network ring gauges with auto-coloring
- **GPU Monitor**: Vendor, name, driver, CUDA version, VRAM usage
- **Ollama Model Manager**: List, pull, and delete Ollama models from the UI
- **Ship Logs**: Live log viewer with agent filtering
- **Message History**: Browse recent agent communications
- **Quick Actions**: One-click commands (Ergo Status, Romi Check, Proxy Ping, Security Audit, Health Check)

### API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/` | Dashboard HTML UI |
| `GET` | `/api/dashboard` | Complete dashboard data (agents, telemetry, GPU, Ollama, NATS) |
| `GET` | `/api/agents` | Agent configs and runtime status |
| `GET` | `/api/gpu` | GPU detection info |
| `GET` | `/api/ollama/models` | List installed Ollama models |
| `POST` | `/api/ollama/pull` | Pull an Ollama model (body: `{"model": "name"}`) |
| `POST` | `/api/ollama/delete` | Delete an Ollama model (body: `{"model": "name"}`) |
| `GET` | `/api/logs?agent=` | Agent log viewer (last 100 lines) |
| `GET` | `/api/history?limit=50` | Message history |
| `POST` | `/api/send` | Send command to agent via NATS (body: `{"agent", "command", "args"}`) |
| `POST` | `/api/chat/stream` | SSE streaming chat (body: `{"agent", "command", "args"}`) |
| `GET` | `/api/fleet` | Fleet topology / plant map |
| `POST` | `/api/fleet/exercise` | Start/stop red-blue exercise |
| `POST` | `/api/fleet/register` | Register this node |
| `GET` | `/api/health` | System health check |

---

## Security

Full policy: **[SECURITY.md](SECURITY.md)** · architecture: **[docs/SECURITY.md](docs/SECURITY.md)**

| Layer | Controls |
|-------|----------|
| Tools | Sandbox blocklists, path allowlists, redaction |
| C11 | `sandbox_run` (seccomp/NS), `policyexec` (shared JSON) |
| Fleet | Red-team tool deny, cross-plant ACL, range isolation |
| NATS | Dev open / token / multi-tenant accounts+nkeys / optional TLS |
| OS | systemd hardening, AppArmor profiles, non-root `agnetic` user |

```bash
# Ops multi-tenant bus + optional TLS
bash scripts/gen-nats-accounts.sh --out /etc/starship/nats
STARSHIP_NATS_TLS=1 bash scripts/gen-nats-tls.sh --out /etc/starship/nats/tls
export STARSHIP_SANDBOX_NATIVE=1 STARSHIP_POLICY_NATIVE=1
```

Report vulnerabilities via GitHub Security Advisories (see SECURITY.md) — not public issues.

---

## Installation

### Development Mode (no root required)

```bash
make dev && make status && make smoke
make stop
```

Logs: `logs/`. Dashboard: `http://localhost:8788`.

### System Install (requires root)

```bash
make install
# or
make deb && sudo dpkg -i dist/starship-os_*.deb
STARSHIP_PROFILE=ops sudo /opt/starship/bin/starship-firstboot.sh
```

Installs to **`/opt/starship`** + **`/etc/starship`** (symlink legacy `/opt/agnetic`).  
Creates users `agnetic` / `nats`, Python venv, systemd units.

### Profiles

| Profile | Intent | NATS (firstboot) |
|---------|--------|------------------|
| `edge` | Thin node | agent-bus |
| `server` | Default mesh | agent-bus |
| `ops` | Full mesh + fleet | multi-tenant accounts |

### ISO

```bash
make iso                    # bootable image
make iso-smoke              # static autoinstall/firstboot checks
```

Autoinstall user-data: `iso/autoinstall/user-data.{edge,server,ops}.yaml`.

---

## Configuration

### Agent YAML (e.g., `agents/proxy.yaml`)

```yaml
name: proxy
role: tech_agent
model: qwen2.5:7b
provider: ollama

capabilities:
  - system_diagnostics
  - log_analysis
  - troubleshooting

skills:
  - system-health
  - proxy-diagnostics

nats:
  subjects:
    command: "agnetic.agent.proxy.command.>"
    event: "agnetic.agent.proxy.event.>"
    status: "agnetic.agent.proxy.status"
```

### Main Config (`agents/config.yaml`)

```yaml
agents:
  proxy:
    model: qwen2.5:7b
    nats_url: nats://127.0.0.1:4222
    enabled: true
  romi:
    model: qwen2.5:7b
    enabled: true
  ergo:
    model: jeffgreen311/eve-v2-unleashed-qwen3.5-8b-liberated-4k-4b-merged
    enabled: true
    schedule:
      - name: nightly-health
        cron: "0 2 * * *"
        workflow: system-health

nats:
  url: nats://127.0.0.1:4222
  jetstream: true

dashboard:
  port: 8788
  host: 0.0.0.0
```

### GPU Detection

```bash
scripts/detect-gpu.sh detect    # Detect GPU vendor and save state
scripts/detect-gpu.sh configure # Configure Ollama for detected GPU
scripts/detect-gpu.sh health    # Check Ollama health and GPU status
scripts/detect-gpu.sh full      # Run all three
```

Supports NVIDIA (CUDA), AMD (ROCm), and CPU-only configurations. Automatically configures `OLLAMA_GPU_LAYERS`, parallelism, and model loading.

---

## CLI Reference

### Make Targets

| Target | Description |
|--------|-------------|
| `make dev` / `stop` / `status` | Dev mesh lifecycle |
| `make smoke` | Full smoke suite |
| `make c11` | Build/test all C11 spikes |
| `make iso-boot` | ISO static + optional QEMU probe |
| `make build` / `build-agent` | Go CLI + Rust StarAgent |
| `make sandbox` / `policyexec` / `bench` | C11 isolation + policy + timing |
| `make deb` / `install` | Package / system install |
| `make iso` / `iso-smoke` | ISO build / firstboot static checks |
| `make nats-accounts` | Generate multi-tenant NATS creds |

### starshipctl Commands

| Command | Description |
|---------|-------------|
| `starshipctl version` | Print version |
| `starshipctl ping` | Ping the NATS bus |
| `starshipctl fleet status` | Fleet / plant overview |
| `starshipctl fleet plants` | List plants |
| `starshipctl fleet register` | Register this node |
| `starshipctl fleet exercise start\|stop` | Red/blue exercise |
| `starshipctl agent …` | Run / status / chat / send |
| `starshipctl telemetry` | Latest StarAgent telemetry |
| `starshipctl skill …` / `workflow …` | Skills and workflows |
| `starshipctl system health` | System health overview |

Compat: `agneticctl` → same binary.

---

## API Reference

### POST `/api/send`

Send a command to an agent and receive a response.

**Request:**
```json
{
  "agent": "proxy",
  "command": "check health",
  "args": {}
}
```

**Response:**
```json
{
  "agent": "proxy",
  "status": "complete",
  "command": "check health",
  "response": "CPU: 12.3% | Memory: 2048MB/8192MB | Disk: 45GB/256GB",
  "timestamp": "2025-07-11T15:30:00"
}
```

### POST `/api/chat/stream`

SSE streaming endpoint for real-time chat with tool execution visibility.

**Request:**
```json
{
  "agent": "proxy",
  "command": "run diagnostics",
  "args": {}
}
```

**SSE Events:**
- `step` — `{"step": 1, "max_steps": 5}`
- `tool_start` — `{"tool": "shell", "args": {"command": "free -h"}}`
- `tool_complete` — `{"tool": "shell", "summary": "Output: ..."}`
- `token` — `{"text": "The system is..."}`
- `response` — `{"text": "Full response text"}`
- `done` — `{"id": "uuid"}`

### GET `/api/health`

```json
{
  "status": "ok",
  "nats_connected": true,
  "agents_running": {"proxy": true, "romi": true, "ergo": true},
  "staragent_running": true,
  "timestamp": "2025-07-11T15:30:00"
}
```

### GET `/api/ollama/models`

```json
{
  "models": [
    {"name": "qwen2.5:7b", "size": 4700000000},
    {"name": "eve-v2:8b", "size": 3400000000}
  ]
}
```

---

## Development

### Building from Source

**Prerequisites:**
- Go 1.22+
- Rust 1.70+ (with cargo)
- Python 3.11+ with venv
- Ollama installed and running
- NATS server installed

```bash
# Build CLI
cd agneticctl && go build -o agneticctl .

# Build StarAgent
cd agent && cargo build --release

# Create Python environment
python3 -m venv .venv
.venv/bin/pip install nats-py aiohttp httpx PyYAML httpx-sse

# Start everything
make dev
```

### Testing

```bash
# Verify NATS connectivity
agneticctl ping

# Check agent status
agneticctl agent status

# Test telemetry
agneticctl telemetry

# Run a workflow
agneticctl workflow run system-health

# Chat with an agent
agneticctl agent chat proxy
```

### Project Structure

```
agnetic-os/
├── agents/              # Agent daemons, configs, tools, workflows
│   ├── agent_daemon.py  # Main agent daemon (Python)
│   ├── tools.py         # Sandboxed tool system
│   ├── scheduler.py     # Cron-based task scheduler
│   ├── workflows.py     # Multi-agent workflow orchestrator
│   └── *.yaml           # Agent configuration files
├── agneticctl/          # Go CLI (Cobra)
│   └── cmd/             # CLI commands
├── agent/               # StarAgent (Rust telemetry collector)
│   └── src/main.rs
├── dashboard/           # Web dashboard
│   ├── server.py        # aiohttp API server
│   └── index.html       # Single-page web UI
├── skills/              # Agent skill definitions (Markdown)
├── souls/               # Agent personality definitions (Markdown)
├── nats/                # NATS server configuration
├── security/            # AppArmor profiles
├── systemd/             # Systemd service units
├── scripts/             # Build, install, and utility scripts
├── debian/              # Debian package structure
├── iso/                 # ISO image builder
├── tray/                # Status bridge (NATS → JSON)
└── logs/                # Runtime logs (gitignored)
```

---

## Roadmap

- [ ] **SSE streaming via NATS** — Real-time token streaming from agents through JetStream
- [ ] **Vector knowledge store** — Semantic search over accumulated agent knowledge
- [ ] **Multi-node mesh** — Agents across multiple machines via NATS leaf nodes
- [ ] **Custom skill marketplace** — Install community-built agent skills
- [ ] **ARM64 support** — Full build pipeline for Raspberry Pi / Apple Silicon
- [ ] **Voice interface** — Whisper STT + TTS pipeline for voice commands
- [ ] **Encrypted inter-agent comms** — TLS for NATS connections
- [ ] **WebUI OAuth** — Browser-based auth for the dashboard

---

## License

MIT License. See [LICENSE](LICENSE) for details.
