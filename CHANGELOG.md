# Changelog

All notable changes to **Starship OS**.

## [2.1.0-alpha.2] — 2026-07-15

### Added
- **starshipctl** CLI rename (compat `agneticctl` symlink) + `/opt/starship` install roots
- **Dashboard** default port **8788**; systemd units on starship paths
- **OpenCode vendoring** — opencode-ai 1.18.2 + oh-my-opencode-slim 2.2.2 pins/tarballs
- **NATS dual-publish** — `starship.*` primary + `agnetic.*` legacy
- **Hardware profiles** — edge / server / ops (`config/profiles.yaml`, `select-profile.sh`)
- **C11 sandbox spike** — ADR 0001 + `src/c/sandbox_spike/sandbox_run`
- **ISO autoinstall** stubs for edge/server/ops
- **Fleet manager** — plants, ops manager, red/blue (`config/fleet.yaml`, `services/fleet.py`, `starshipctl fleet`)
- **Dashboard fleet map** — `/api/fleet` + plant map panel
- **Red-team policy** — `fleet_policy.py` denies OpenCode/shell/write for red-team
- **Cross-plant ACL** — `acl` block in `fleet.yaml`; `check_cross_plant` in policy engine
- **Multi-node NATS fleet auth** — `nats/fleet-bus.conf` + `fleet-auth.yaml`; `STARSHIP_NATS_TOKEN`
- **Ops firstboot fleet-bus** — token materialization, `active.conf`, `nats.env` / `fleet.env`
- **C11 sandbox bench** — `make bench` / `scripts/bench-sandbox.sh` (p50 ≪ 2ms, ADR 0001)
- **Native sandbox bridge** — `agents/sandbox_native.py` via `STARSHIP_SANDBOX_NATIVE=1`

### Changed
- Product branding strings → Starship OS; debian package `starship-os`
- `delegate_to_agent` dual-publishes `starship.*` / `agnetic.*` and accepts `plant`

## [2.1.0-alpha.1] — 2026-07-15

### Added
- **Monorepo reconcile** — agnetic-os (2.0 packaging) + WSL Alpha 2.1 services under `starship-os`
- **Governance stack** — policy, event hooks, Droid Shield, service accounts, OTEL telemetry
- **Incident response** — runbook-as-code system
- **7-type memory** — Working, Semantic, Episodic, Procedural, Retrieval, Parametric, Prospective + LanceDB
- **Agent email** — SMTP + Mailchain dual-mode service + tools
- **Self-healing** — healer heartbeats in agent daemon
- **Web C2 dashboard** — Airia-inspired UI (port 8788), org chart, goals, email panel
- **C++ vector_index** — embedding normalize / mean-pool / batch-dot (pybind11)
- **Model registry** — `config/models.yaml`; default **Eve-V2-Unleashed** (`num_ctx=16384`)
- **Plans** — streamline plan + OpenCode/oh-my-opencode-slim addendum
- **Architecture docs** — overview, module catalog, third_party NOTICE

### Changed
- Canonical product name: **Starship OS**; GitHub: `andromi-hash/starship-os`
- Version scheme: `2.1.0-alpha.x` (2.0 line was agnetic-os packaging)

### Security
- Credentials files scrubbed from tree; expanded `.gitignore` for secrets

## [0.2.0] — 2026-07-11

### Added
- **Tool System** — 8 sandboxed tools: shell, read_file, write_file, list_dir, http_get, http_post, search_files, delegate_to_agent
- **Tool Compositing** — TOOLSETS pattern (core, network, delegation, full, readonly, webhook_safe)
- **CommandExecutor** — Sandboxed execution with dry-run, timeout, and redaction
- **Typed Errors** — ToolError, SandboxError, TimeoutError, AccessDeniedError
- **Tool Call Auto-Repair** — Fixes malformed JSON arguments from models
- **SSE Streaming** — `/api/chat/stream` endpoint for real-time token-by-token chat
- **Multi-Agent Delegation** — `delegate_to_agent` tool for Ergo→Proxy/Romi coordination
- **NATS Authentication** — Per-agent tokens with subject-level permissions
- **Encrypted Config** — AES-256-GCM with PBKDF2 key derivation
- **Secrets Manager** — Encrypted API key/token storage
- **AppArmor Profiles** — agnetic-agent, ollama, nats (deny-by-default)
- **Secret Redaction** — Auto-redacts tokens/keys from tool output
- **README.md** — Comprehensive project documentation
- **AGENT_GUIDE.md** — Developer guide for creating new agents
- **SECURITY.md** — Security architecture documentation

### Changed
- Agent daemon now uses chat API with tool calling loop (max 10 rounds)
- Dashboard server.py enhanced with streaming endpoint
- Tool arguments auto-repaired before execution

## [0.1.0] — 2026-07-11

### Added
- **Restructured** as Starship OS monorepo
- **GPU Detection** — `scripts/detect-gpu.sh` (NVIDIA/AMD/Intel, WSL2 support)
- **Systemd Daemon Mode** — 7 service units with security hardening
- **Debian Packaging** — `.deb` package with postinst/prerm/postrm scripts
- **ISO Building** — live-build configuration for Ubuntu 24.04
- **Dynamic Dashboard** — Reads agent YAML configs, GPU info, Ollama models
- **Ollama Model Manager** — List, pull, delete models from web UI
- **Agent Auto-Pull** — Agents pull their model on first start
- **CLI** — `agneticctl` (Go/Cobra) with ping, agent, version commands
- **StarAgent** — Rust telemetry collector → NATS
- **3 Agent Daemons** — proxy, romi, ergo with YAML configs
- **NATS + JetStream** — Agent-to-agent message bus
- **Makefile** — build, dev, status, stop, install, deb, iso targets

### Infrastructure
- Go 1.24.4, Rust 1.97.0, NATS 2.14.3
- Python venv with nats-py, aiohttp, httpx, PyYAML
- GitHub: https://github.com/andromi-hash/agnetic-os
