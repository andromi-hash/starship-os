# Starship OS — Agent Context File

## System Overview
Self-hosted AI agent mesh with 43 tools, 20 toolsets, 7 memory types, running on Ollama + NATS. Dashboard on port 8788. All code at `/opt/agnetic/`.

## Core Architecture
- `/opt/agnetic/lib/agent_daemon.py` — Main agent runtime: NATS subscribe → process_command → LLM provider → tool loop
- `/opt/agnetic/lib/tools.py` — 43 tools in TOOL_DEFINITIONS, dispatched via execute_tool(), composited via TOOLSETS
- `/opt/agnetic/lib/dashboard/server.py` — ThreadingHTTPServer dashboard, 20+ API endpoints
- `/opt/agnetic/services/` — 18 service modules (see below)

## Services (`/opt/agnetic/services/`)
| File | Purpose | Key Classes/Functions |
|------|---------|----------------------|
| `memory.py` | 7-type memory manager with LanceDB vector search | MemoryManager, MemoryType (7), ProspectiveMemoryManager, get_memory_manager() |
| `policy.py` | Hierarchical policy, extension-only semantics | PolicyManager (system→service→user tiers), CommandBlocklist (allow/block/deny via shutil.which) |
| `event_hooks.py` | Deterministic lifecycle hooks | HookEvent (8 events), HookManager, get_hook_manager(), exit-code blocking (0=allow, 2=block) |
| `droid_shield.py` | ML-augmented secret scanning | DroidShield, ScanResult, 12+ built-in patterns, scan_text/scan_file/scan_git_diff/redact |
| `service_accounts.py` | Persistent agent identities | ServiceAccountManager, authenticate(key), rotate_key(), JSON-backed at /var/lib/agnetic/ |
| `telemetry.py` | OTEL-native telemetry export | TelemetryExporter, modes: otlp/file/disabled, 60s periodic flush, JSONL fallback |
| `incident_response.py` | Runbook-as-code incident system | IncidentResponseManager, 6 built-in runbooks, full lifecycle (create→resolve/escalate) |
| `agent_email.py` | Dual-mode email: SMTP + Mailchain | AgentEmailService, get_email_service(), register/send/inbox/remove |
| `healer.py` | Self-healing agent health monitor | SelfHealer, liveness probes, auto-restart on 3+ consecutive errors, state at /var/lib/agnetic/ |
| `onboarding.py` | User onboarding wizard | run_wizard(), 6-step guided setup, state at /var/lib/agnetic/onboarding.json |
| `governance.py` | Risk-based action approval | GovernanceManager, check_action() |
| `provider_router.py` | Multi-LLM routing | get_provider(), query_provider(), supports Ollama + OpenRouter + custom |
| `checkpoint.py` | Filesystem snapshot/rollback | CheckpointManager, tar-based snapshots |
| `browser.py` | Playwright browser automation | BrowserManager, navigate/screenshot/click/fill |
| `mcp.py` | MCP server integration | MCPManager, init_mcp(), call_mcp_tool() |
| `context_loader.py` | Auto-context discovery | discover_context_files(), load_context() |
| `credential_pool.py` | Managed credential storage | CredentialPoolManager |
| `skills_hub.py` | Skills.sh marketplace | search/preview/test/install/list_skills |

## Dashboard (`/opt/agnetic/lib/dashboard/`)
- `server.py` — 652 lines, 20+ API endpoints (health, agents, chat, policy, memory, incidents, shield, telemetry, accounts, email)
- `static/style.css` — Airia-inspired warm purple/dark theme, glassmorphism, gradient accents
- `static/ui.js` — Global state (S), api() wrapper, renderView() dispatcher, showToast/confirmDialog, email CRUD UI
- `static/dashboard.js` — Health cards, quick actions grid, telemetry feed
- `static/agents.js` — Agent list with status dots, detail panels
- `static/chat.js` — SSE streaming chat, tool call cards, model selector
- `static/panels.js` — Policy/memory/shield/telemetry/accounts/skills panels
- `static/incidents.js` — Incident list with severity colors, runbook viewer
- `static/boot.js` — Init, nav binding, 30s polling, Ctrl+K palette, Ctrl+N new chat

## Email System
- Service: `services/agent_email.py` → AgentEmailService, get_email_service()
- Tools: `send_email`, `email_list_inbox`, `email_register_address`, `email_list_addresses`, `email_remove_address`
- Config via env vars: `AGNETIC_EMAIL_SMTP_HOST`, `AGNETIC_EMAIL_SMTP_PORT`, `AGNETIC_EMAIL_SMTP_USER`, `AGNETIC_EMAIL_SMTP_PASSWORD`, `AGNETIC_MAILCHAIN_API_URL`, `AGNETIC_MAILCHAIN_WALLET`
- Addresses stored at `/var/lib/agnetic/agent_email_addresses.json`

## Self-Healing (healer.py)
- SelfHealer singleton, state at /var/lib/agnetic/healer_state.json
- Liveness probes: agents report health; 3+ consecutive errors → auto-recover
- detect_stall(agent, timeout_seconds=120) → auto-recover stale agents
- summary() returns total/alive/stalled/recovery_count

## DFlash Speculative Decoding
- Merged into llama.cpp (PR #22105, June 2026)
- Up to 4.44x speedup on Qwen3.6, output-lossless at temperature 0
- Requires llama-server binary with DFlash support + compatible draft GGUF
- Ollama 0.31.2 does NOT expose DFlash yet (Ollama spec-decode support incomplete as of mid-2026)
- Workaround: run llama-server directly alongside Ollama

## Install & Onboarding
- Installer: `/opt/agnetic/install.sh` — auto-detects hardware, installs Ollama + models + services
- Onboarding wizard: `python3 -c "from services.onboarding import run_wizard; run_wizard()"`
- Systemd services: agnetic-core (agent daemon), agnetic-dashboard (WebUI)
- Health check: `agnetic-health` command

## C++ Performance Plan (`PERFORMANCE_PLAN.md`)
Priority order:
1. Vector search (LanceDB → custom C++ HNSW) — 100x speedup
2. Tool sandbox (subprocess → C++ fork+exec+seccomp) — 20x
3. Policy regex (Python re → C++ re2 DFA) — 100x
4. Telemetry serialization (JSON → flatbuffers) — 100x
5. Memory compaction (Python merge → C++ LSM-tree) — 20x
Total pipeline: ~170ms → ~6ms (~28x)

## System Documentation
- `SYSTEM_GUIDE.md` — Complete architecture, file-by-file breakdown, design rationale, performance profile, safety features
- `PERFORMANCE_PLAN.md` — C++ rewrite roadmap with expected speedups
- `AGENTS.md` — This file (opencode context persistence)

## Stress Test Results (most recent)
67/69 passed. 2 failures are expected (NATS + Dashboard not running).
All 14 service imports succeed, all instantiate, all tools load (43), all 7 memory types present, all 20 toolsets registered.
