SHELL := /bin/bash
CARGO := $(HOME)/.cargo/bin/cargo
GO := /tmp/go/bin/go
export PATH := /tmp/go/bin:$(HOME)/.cargo/bin:$(HOME)/.local/bin:$(PATH)

.PHONY: all build build-agent cli install uninstall run dev stop clean status profile sandbox smoke bench

all: build build-agent

# ─── Build ──────────────────────────────────────────────────────────
build:
	cd starshipctl && $(GO) build -o starshipctl .
	@ln -sf starshipctl starshipctl/agneticctl 2>/dev/null || true

build-agent:
	cd agent && $(CARGO) build --release

cli: build
	mkdir -p ~/.local/bin
	cp starshipctl/starshipctl ~/.local/bin/starshipctl
	ln -sf starshipctl ~/.local/bin/agneticctl

# ─── Install (requires root) ────────────────────────────────────────
install:
	@echo "Building binaries..."
	@$(MAKE) build build-agent
	@echo ""
	sudo bash scripts/install-daemon.sh

uninstall:
	sudo bash scripts/uninstall-daemon.sh

# ─── Dev mode (user-level, no root) ────────────────────────────────
dev: cli
	@echo "Starting services in dev mode..."
	setsid nats-server -c nats/agent-bus.conf > /dev/null 2>&1 < /dev/null &
	sleep 1
	setsid .venv/bin/python3 agents/agent_daemon.py proxy > logs/agents-proxy.log 2>&1 < /dev/null &
	setsid .venv/bin/python3 agents/agent_daemon.py romi > logs/agents-romi.log 2>&1 < /dev/null &
	setsid .venv/bin/python3 agents/agent_daemon.py ergo > logs/agents-ergo.log 2>&1 < /dev/null &
	setsid .venv/bin/python3 tray/agnetic-status.py > logs/status-bridge.log 2>&1 < /dev/null &
	setsid .venv/bin/python3 scripts/message_history.py > logs/message-history.log 2>&1 < /dev/null &
	DASHBOARD_PORT=8788 setsid .venv/bin/python3 dashboard/server.py > logs/dashboard.log 2>&1 < /dev/null &
	sleep 2
	@$(MAKE) status

stop:
	-pkill nats-server 2>/dev/null
	-pkill staragent 2>/dev/null
	-pkill -f "agent_daemon.py" 2>/dev/null
	-pkill -f "agnetic-status.py" 2>/dev/null
	-pkill -f "message_history.py" 2>/dev/null
	-pkill -f "dashboard/server.py" 2>/dev/null
	@echo "All services stopped"

status:
	@echo ""
	@echo "=== Starship OS — Service Status ==="
	@echo ""
	@pgrep nats-server > /dev/null && echo "  ● nats-server     — running" || echo "  ● nats-server     — stopped"
	@pgrep staragent > /dev/null && echo "  ● staragent       — running" || echo "  ● staragent       — stopped"
	@pgrep -f "agent_daemon.py proxy" > /dev/null && echo "  ● agent proxy     — running" || echo "  ● agent proxy     — stopped"
	@pgrep -f "agent_daemon.py romi" > /dev/null && echo "  ● agent romi      — running" || echo "  ● agent romi      — stopped"
	@pgrep -f "agent_daemon.py ergo" > /dev/null && echo "  ● agent ergo      — running" || echo "  ● agent ergo      — stopped"
	@pgrep -f "agnetic-status.py" > /dev/null && echo "  ● status-bridge   — running" || echo "  ● status-bridge   — stopped"
	@pgrep -f "message_history.py" > /dev/null && echo "  ● message-history — running" || echo "  ● message-history — stopped"
	@ss -tlnp 2>/dev/null | grep -q 8788 && echo "  ● dashboard       — running (:8788)" || echo "  ● dashboard       — stopped"
	@ss -tlnp 2>/dev/null | grep -q 8790 && echo "  ● dashboard-dev   — running (:8790 fleet UI)" || true
	@pgrep -f "fleet.py daemon" > /dev/null && echo "  ● fleet-daemon    — running" || echo "  ● fleet-daemon    — stopped"
	@echo ""
	@echo "=== Ollama Models ==="
	@$(HOME)/.local/bin/ollama list 2>/dev/null || ollama list 2>/dev/null || echo "  (ollama not available)"
	@echo ""

# ─── Hardware profile ───────────────────────────────────────────────
profile:
	@bash scripts/select-profile.sh $(PROFILE)

# ─── C11 sandbox spike (ADR 0001) ───────────────────────────────────
sandbox:
	$(MAKE) -C src/c/sandbox_spike all test

bench:
	@bash scripts/bench-sandbox.sh $(or $(N),200)

# ─── Smoke tests ────────────────────────────────────────────────────
smoke:
	@bash scripts/smoke-test.sh

# ─── Clean ──────────────────────────────────────────────────────────
clean:
	rm -f starshipctl/starshipctl starshipctl/agneticctl
	rm -rf agent/target
	rm -rf __pycache__ agents/__pycache__ dashboard/__pycache__

# ─── Debian package ──────────────────────────────────────────────────
deb: build build-agent
	@bash scripts/build-deb.sh

# ─── ISO image ──────────────────────────────────────────────────────
iso: build build-agent
	@echo "Building ISO (requires root)..."
	sudo bash scripts/build-iso.sh

docker:
	docker build -t agnetic-os .
