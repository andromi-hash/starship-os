# Changelog

All notable changes to **Starship OS**.

## [2.2.0] — 2026-07-17

### Added
- **Dual-Brain Memory Stack (Phase 1A)** — Agent-specific `memory/<agent>/MEMORY.md` + `USER.md` files for romi/ergo/proxy with frozen system prompt injection. `TEMPORAL`, `KNOWLEDGE_GRAPH`, `PREFERENCE` memory types added to both LanceDB memory services. 7 memory tools: `memory_note`, `user_profile`, `archive_search`, `temporal_graph`, `temporal_chain`, `kg_query`, `kg_store`.
- **FTS5 Session Archive (Phase 1B)** — `services/archive.py` with SQLite + FTS5 full-text search, `write()`/`search()`/`stats()` + CLI mode. Archive write hooks in both daemons (fires after each command). `archive_search` tool uses `ArchiveService` with JSONL fallback.
- **Cron Scheduler (Phase 1C)** — `create_schedule`, `list_schedules`, `remove_schedule` tools with natural-language cron parser (`every hour`, `daily at 9am`, `every 30 minutes`) in both tool files. Reuses existing `services/webhooks.py` scheduler backend.
- **Parallel Delegation (Phase 1D)** — `delegate` tool on primary daemon for fan-out parallel execution via `asyncio.gather`. Advanced daemon already had `spawn_subagent`/`list_subagents`/`kill_subagent` with `SubAgentManager` and LanceDB registry.

- **Knowledge Graph Integration (Phase 2A)** — `kg_store` and `kg_query` tools in both daemons now backed by real storage: LanceDB (`src/python/services/memory.py`) for advanced daemon, SQLite (`services/memory.py`) for primary daemon. Triples stored as structured metadata (subject/predicate/object) with semantic search retrieval.
- **Preference Memory (Phase 2B)** — `preference_note` and `preference_query` tools in both daemons. User preferences stored as key-value pairs with context, using PREFERENCE memory type. Retrievable by key across sessions.
- **Temporal Graph Chain (Phase 2C)** — `temporal_graph`, `temporal_chain`, `temporal_snapshot` tools in both daemons. State transitions recorded via `services/audit.py` (existing audit trail with `before_state`/`after_state` columns). `temporal_snapshot` writes new transitions; `temporal_graph` queries by entity; `temporal_chain` reconstructs full action chains via `parent_action_id` linked-list traversal.
- **Obsidian HITL Vault (Phase 2D)** — `services/hitl_vault.py` bridges the HITL approval system to a markdown vault directory compatible with Obsidian. 6 vault tools: `vault_sync` (sync hitl.db → markdown), `vault_list`, `vault_note` (ad-hoc markdown notes with frontmatter), `vault_approve`, `vault_deny`, `vault_stats`. Vault directory defaults to `memory/vault/`.
- **Goals → Missions → Tasks System** — Full three-layer strategic planning hierarchy. `services/goals.py` with SQLite persistence, status lifecycle, health computation (on_track/at_risk/off_track), and audit integration. Dashboard "Goals" widget with expandable goal/mission/task tree, horizontal progress bars, health indicators, and Chart.js timeline bar chart. 9 agent tools: `goal_create`, `goal_list`, `goal_update`, `mission_create`, `mission_list`, `task_create`, `task_list`, `task_complete`. REST API: 14 endpoints for Goal/Mission/Task CRUD + health recompute. Integrates with `services/audit.py` (temporal snapshots), `services/memory.py` (KNOWLEDGE_GRAPH task dependency triples), `services/hitl.py` (goal status approval gates), `services/archive.py` (goal completion snapshots), `services/webhooks.py` (scheduled health checks).

### Changed
- `memory/romi/MEMORY.md`, `memory/romi/USER.md`, `memory/ergo/MEMORY.md`, `memory/ergo/USER.md`, `memory/proxy/MEMORY.md`, `memory/proxy/USER.md` — created as stubs
- `services/memory.py` — `MemoryType` enum extended with `TEMPORAL`, `KNOWLEDGE_GRAPH`, `PREFERENCE`
- `src/python/services/memory.py` — same enum extension
- `agents/tools.py` — 7 memory tools + 3 schedule tools + `delegate` parallel tool + KG wired to SQLite storage added
- `src/python/lib/tools.py` — 7 memory tools + 3 schedule tools + KG wired to LanceDB storage added
- `agents/agent_daemon.py` — memory context injection + archive write hook added
- `src/python/lib/agent_daemon.py` — memory context injection + archive write hook added

## [2.1.0] — 2026-07-15

### Added
- **Phase 5 complete** — OpenCode firstboot pantheon, `starshipctl tui`, ISO boot smoke, C11 `starshipd` + `heald` spikes
- OpenCode install on ops/server firstboot + `/etc/starship/opencode/` preset
- Interactive TUI: `starshipctl tui` (status/fleet/agents/smoke/opencode)
- `scripts/iso-boot-smoke.sh` — static gates + optional QEMU probe
- C11 `starshipd` (dual-prefix agent loop spike) + `heald` (liveness probe spike)
- Full 2.1 stack: fleet ACL, NATS accounts/TLS, C11 sandbox/policyexec, deb packaging

### Changed
- Version **2.1.0** (GA cut from beta.1 streamline)
- Ops firstboot: multi-tenant NATS + native sandbox + OpenCode pantheon config

## [2.1.0-beta.1] — 2026-07-15

Beta packaging cut (Phases 0–4): fleet, NATS accounts, C11 sandbox/policyexec, SECURITY.md.

## [2.1.0-alpha.3] — 2026-07-15

Phase 2–3 packaging: fleet ACL, NATS fleet-bus/accounts, C11 bench + seccomp,
ISO firstboot smoke, deb layout, policyexec spike. Superseded by beta.1.

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

### Changed
- Product branding strings → Starship OS; debian package `starship-os`

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
