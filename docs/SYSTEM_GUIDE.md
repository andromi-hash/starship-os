# Starship OS (Starship OS) — Complete System Guide

## What This Is

Starship OS is a self-hosted, private AI agent mesh that runs on your own hardware.
Think of it as an operating system for AI agents — it manages, coordinates, secures,
and heals a fleet of agents that work for you locally and manages micro agents installed remotly accros an enterprise system.

## Architecture Overview

```
┌─────────────────────────────────────────────────────┐
│                  Web Dashboard (:8788)               │
│  Airia-style UI — monitor, chat, configure, heal     │
├─────────────────────────────────────────────────────┤
│                  Agent Daemon                        │
│  6 agents: core, coder, secops, data, subagents      │
├─────────────────────────────────────────────────────┤
│                Service Layer                         │
│  ┌────────┬────────┬────────┬────────┬────────┐     │
│  │Policy  │Memory  │Hooks   │Shield  │Telemetry│     │
│  │Engine  │7-types │Lifecyc │Secrets │OTEL     │     │
│  ├────────┼────────┼────────┼────────┼────────┤     │
│  │Accounts│Incident│Email   │Healer  │Provider │     │
│  │Identity│Resp    │SMTP+WC │Self-   │Router   │     │
│  │        │        │        │Heal    │         │     │
│  └────────┴────────┴────────┴────────┴────────┘     │
├─────────────────────────────────────────────────────┤
│                Tool System (43 tools)                │
│  shell, code, memory, email, browser, MCP, hooks...  │
├─────────────────────────────────────────────────────┤
│              NATS Message Bus (:4222)                 │
├─────────────────────────────────────────────────────┤
│            Ollama LLM Engine (:11435)                  │
│  qwen2.5:7b, nomic-embed-text, DFlash (optional)     │
├─────────────────────────────────────────────────────┤
│              Your Hardware (CPU/GPU/RAM)             │
└─────────────────────────────────────────────────────┘
```

## Files & What They Do

### `/opt/agnetic/services/` — Core Services (14 files)

| File | What It Does | Why It Exists |
|------|-------------|---------------|
| `memory.py` | 7-type memory manager with LanceDB vector search | Agents need to remember facts, past episodes, procedures, and future intentions |
| `policy.py` | Hierarchical policy with extension-only semantics + command blocklist | Inspired by Factory AI: system→service→user tiers, path-resolved allow/block/deny |
| `event_hooks.py` | Deterministic lifecycle hooks (PreToolUse, PostToolUse, etc.) | Inspired by Factory AI: shell-command hooks with exit-code blocking |
| `droid_shield.py` | ML-augmented secret scanning + git guardrails | Inspired by Factory AI: detect API keys, tokens, private keys before they leak |
| `service_accounts.py` | Persistent agent identities with API key auth | Each agent gets a service account with roles and permissions |
| `telemetry.py` | OTEL-native telemetry with HTTP + JSONL fallback | Inspired by Factory AI: structured event export for observability |
| `incident_response.py` | Runbook-as-code incident response | Automated recovery procedures: disk-space, high-cpu, security-breach, etc. |
| `agent_email.py` | Dual-mode email (SMTP direct + Mailchain Web3) | Agents send notifications and receive instructions via email |
| `healer.py` | Self-healing agent health monitor | Kubernetes-style liveness probes for agents — auto-restart on stall |
| `onboarding.py` | User onboarding wizard | Warm, guided 6-step setup on first launch |
| `governance.py` | Agent action governance (pre-Factory AI) | Risk-based approval for dangerous commands |
| `provider_router.py` | Multi-LLM provider routing (Ollama + OpenRouter + custom) | Use any model from any provider with fallback |
| `checkpoint.py` | Filesystem checkpoint/rollback | Snapshot state before risky operations |
| `browser.py` | Playwright-based browser automation | Agents browse the web interactively |
| `mcp.py` | Model Context Protocol server integration | Connect external MCP tools |
| `context_loader.py` | Auto-discover context files | Load READMEs, configs, docs as agent context |
| `credential_pool.py` | Managed credential pools | Store and rotate API keys safely |
| `skills_hub.py` | Skills.sh marketplace integration | Install community skills |

### `/opt/agnetic/lib/dashboard/` — Web UI (12 files)

| File | What It Does |
|------|-------------|
| `server.py` | ThreadingHTTPServer on port 8788, 20+ API endpoints, SSE streaming |
| `static/index.html` | Three-panel layout with modular JS loading |
| `static/style.css` | 994-line dark theme, CSS grid, conversation bubbles, severity colors |
| `static/ui.js` | API wrapper, global state, sidebar, view/panel rendering |
| `static/dashboard.js` | Health cards, quick actions, telemetry feed |
| `static/agents.js` | Agent list with status dots, detail panel, chat button |
| `static/chat.js` | SSE streaming, tool call cards, model selector, session management |
| `static/panels.js` | Policy viewer, memory browser, telemetry, shield scan, accounts, skills |
| `static/incidents.js` | Incident list with severity, runbook viewer, resolve action |
| `static/boot.js` | Init, navigation, 30s agent polling, Ctrl+K palette |

### `/opt/agnetic/lib/tools.py` — Tool System

43 tools across 20 toolsets. Pattern borrowed from Hermes Agent (composable toolsets)
and Flamingo Stack (typed errors, sandbox, redaction).

Key toolsets: core, network, delegation, coding, planning, memory, email, hooks,
credentials, browser, checkpoint, MCP, plugins, skillshub.

### `/opt/agnetic/lib/agent_daemon.py` — Agent Runtime

The main agent loop. Subscribes to NATS, loads agent config, builds system prompt
with memory context + telemetry + skills, routes to LLM provider, handles tool loops.

## Design Decisions & Rationale

### Why Hermes WebUI pattern?
- Zero build step: vanilla JS, no npm, no webpack — works anywhere
- ThreadingHTTPServer: ship a dashboard with zero external deps
- SSE streaming: simple real-time without WebSocket complexity

### Why Factory AI patterns?
- Policy extension-only semantics: prevents policy drift across tiers
- Exit-code hook blocking: hooks return 0=allow, 2=block — deterministic and scriptable
- Path-resolved blocklist: `shutil.which()` resolves aliases before matching

### Why LanceDB for memory?
- Embedded vector database: no server to manage
- Native Python: zero extra deps
- Supports 7 memory types with one unified interface

### Why SMTP + Mailchain for email?
- SMTP: works with any email provider (Gmail, Outlook, self-hosted)
- Mailchain: Web3 native email for decentralized/blockchain use cases
- Dual mode: agent can use either transparently

### Why self-healing?
- Agents are autonomous and can stall in tool loops
- Liveness probes detect stalls within 120s
- systemd Restart=always + healer.py = automatic recovery

## Performance Profile

| Operation | Python | C++ (planned) | Speedup |
|-----------|--------|---------------|---------|
| Vector search (10K) | 50ms | 0.5ms | 100x |
| Tool sandbox exec | 10ms | 0.5ms | 20x |
| Policy check | 1ms | 0.01ms | 100x |
| Telemetry export | 10ms | 0.1ms | 100x |
| Memory compaction | 100ms | 5ms | 20x |

See `PERFORMANCE_PLAN.md` for the full C++ roadmap.

## Resource Usage

| Component | RAM | Disk | CPU |
|-----------|-----|------|-----|
| Ollama (7B model) | ~6GB | ~5GB | 50-90% during inference |
| Ollama (3B model) | ~2GB | ~2GB | 30-60% during inference |
| NATS server | ~20MB | ~10MB | <1% idle |
| Dashboard | ~30MB | ~2MB | <1% |
| Agent daemon (per agent) | ~50MB | ~5MB | 1-5% |
| Memory system | ~100MB | ~500MB | <1% |
| **Total (7B GPU)** | ~7GB | ~8GB | Variable |
| **Total (3B CPU)** | ~3GB | ~3GB | Variable |

## DFlash Speculative Decoding

Available as an optimization for Qwen3-based models. Requires:
1. `llama-server` binary compiled with DFlash support (PR #22105)
2. A DFlash draft GGUF (e.g., `z-lab/Qwen3.6-27B-DFlash`)
3. Run: `llama-server -m target.gguf -md draft.gguf --spec-type draft-dflash`

Typical speedup: 2-4x on GPU, 1.5-2x on CPU.
Greedy decoding (temperature 0) is output-lossless — same quality, faster.

## Quick Reference

```bash
# Install
curl -fsSL https://raw.githubusercontent.com/your-repo/install.sh | bash

# Start
sudo systemctl start agnetic-core
sudo systemctl start agnetic-dashboard

# Dashboard
open http://localhost:8788

# Health
agnetic-health

# Run an agent
python3 /opt/agnetic/lib/agent_daemon.py agnetic-coder --model qwen2.5:7b

# Send a command via NATS
nats pub 'agnetic.agent.agnetic-core.command.execute' \
  '{"command": "summarize the latest logs"}'

# Check onboarding progress
python3 -c "from services.onboarding import get_progress; print(get_progress())"

# Recovery history
python3 -c "from services.healer import get_healer; print(get_healer().summary())"
```

## Safety Features

1. **Droid Shield**: 12+ secret patterns auto-detected before any tool call
2. **Policy Engine**: 3-tier hierarchy prevents privilege escalation
3. **CommandBlocklist**: path-resolved allow/block/deny prevents dangerous commands
4. **Event Hooks**: PreToolUse hooks can block tool calls with exit code 2
5. **Sandbox**: All shell commands run through restricted executor
6. **Redaction**: API keys, tokens, and passwords redacted from all output
7. **Service Accounts**: Every agent has an identity with scoped permissions
8. **Self-Healing**: Stalled agents auto-restart within 2 minutes
9. **Checkpoint/Rollback**: Filesystem snapshots before risky operations
