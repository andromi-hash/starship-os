#!/usr/bin/env bash
# Starship OS — Systemd Daemon Installer
# Installs all components to /opt/agnetic, creates users, enables services.
# Must run as root.
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log()  { echo -e "${GREEN}[INSTALL]${NC} $*"; }
warn() { echo -e "${YELLOW}[INSTALL]${NC} $*"; }
err()  { echo -e "${RED}[INSTALL]${NC} $*" >&2; exit 1; }
info() { echo -e "${BLUE}[INSTALL]${NC} $*"; }

# ─── Pre-flight checks ──────────────────────────────────────────────
if [[ "$(id -u)" != "0" ]]; then
    err "Must run as root. Use: sudo $0"
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

echo -e "${BLUE}╔══════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║  Starship OS — Daemon Installer     ║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════════╝${NC}"
echo ""

# ─── 1. Create system users ─────────────────────────────────────────
log "Creating system users..."

if ! id -u agnetic &>/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin \
        --comment "Starship OS Service Account" agnetic
    log "Created user: agnetic"
else
    info "User agnetic already exists"
fi

if ! id -u nats &>/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin \
        --comment "NATS Message Bus" nats
    log "Created user: nats"
else
    info "User nats already exists"
fi

# ─── 2. Create directory structure ──────────────────────────────────
log "Creating directory structure..."

DIRS=(
    /opt/agnetic/bin
    /opt/agnetic/lib/agnetic
    /opt/agnetic/lib/agnetic/agents
    /opt/agnetic/lib/agnetic/agents/skills
    /opt/agnetic/lib/agnetic/dashboard
    /opt/agnetic/lib/agnetic/tray
    /opt/agnetic/lib/agnetic/scripts
    /opt/agnetic/lib/agnetic/skills
    /opt/agnetic/lib/agnetic/souls
    /opt/agnetic/venv
    /etc/agnetic
    /etc/agnetic/nats
    /var/lib/agnetic
    /var/lib/agnetic/nats
    /var/lib/agnetic/message-history
    /var/log/agnetic
)

for d in "${DIRS[@]}"; do
    mkdir -p "$d"
done
log "Directories created"

# ─── 3. Install binaries ───────────────────────────────────────────
log "Installing binaries..."

# agneticctl CLI
if [[ -f "$REPO_DIR/agneticctl/agneticctl" ]]; then
    cp "$REPO_DIR/agneticctl/agneticctl" /opt/agnetic/bin/
    chmod 755 /opt/agnetic/bin/agneticctl
    ln -sf /opt/agnetic/bin/agneticctl /usr/local/bin/agneticctl
    log "Installed: agneticctl"
else
    warn "agneticctl binary not found — build it first"
fi

# StarAgent
if [[ -f "$REPO_DIR/agent/target/release/staragent" ]]; then
    cp "$REPO_DIR/agent/target/release/staragent" /opt/agnetic/bin/
    chmod 755 /opt/agnetic/bin/staragent
    log "Installed: staragent"
else
    warn "staragent binary not found — build it first"
fi

# ─── 4. Install Python application code ─────────────────────────────
log "Installing Python application code..."

# Agent daemon
cp "$REPO_DIR/agents/agent_daemon.py" /opt/agnetic/lib/agnetic/agents/
cp "$REPO_DIR/agents/run_agent.sh" /opt/agnetic/lib/agnetic/agents/ 2>/dev/null || true
cp "$REPO_DIR/agents/scheduler.py" /opt/agnetic/lib/agnetic/agents/ 2>/dev/null || true
cp "$REPO_DIR/agents/workflows.py" /opt/agnetic/lib/agnetic/agents/ 2>/dev/null || true
chmod +x /opt/agnetic/lib/agnetic/agents/agent_daemon.py
chmod +x /opt/agnetic/lib/agnetic/agents/run_agent.sh 2>/dev/null || true

# Dashboard
cp "$REPO_DIR/dashboard/server.py" /opt/agnetic/lib/agnetic/dashboard/
cp "$REPO_DIR/dashboard/index.html" /opt/agnetic/lib/agnetic/dashboard/
chmod +x /opt/agnetic/lib/agnetic/dashboard/server.py

# Status bridge
cp "$REPO_DIR/tray/agnetic-status.py" /opt/agnetic/lib/agnetic/tray/
chmod +x /opt/agnetic/lib/agnetic/tray/agnetic-status.py

# Scripts
cp "$REPO_DIR/scripts/message_history.py" /opt/agnetic/lib/agnetic/scripts/
chmod +x /opt/agnetic/lib/agnetic/scripts/message_history.py

# Skills
cp -r "$REPO_DIR/skills/"* /opt/agnetic/lib/agnetic/skills/ 2>/dev/null || true

# Souls
cp -r "$REPO_DIR/souls/"* /opt/agnetic/lib/agnetic/souls/ 2>/dev/null || true

# Skills (agents subdir)
cp -r "$REPO_DIR/agents/skills/"* /opt/agnetic/lib/agnetic/agents/skills/ 2>/dev/null || true

# GPU detection
cp "$REPO_DIR/scripts/detect-gpu.sh" /opt/agnetic/bin/
chmod +x /opt/agnetic/bin/detect-gpu.sh

log "Application code installed"

# ─── 5. Install YAML configs ───────────────────────────────────────
log "Installing YAML configs..."

cp "$REPO_DIR/agents/config.yaml" /etc/agnetic/ 2>/dev/null || true
cp "$REPO_DIR/agents/proxy.yaml" /etc/agnetic/ 2>/dev/null || true
cp "$REPO_DIR/agents/romi.yaml" /etc/agnetic/ 2>/dev/null || true
cp "$REPO_DIR/agents/ergo.yaml" /etc/agnetic/ 2>/dev/null || true
cp "$REPO_DIR/agents/orchestrator.yaml" /etc/agnetic/ 2>/dev/null || true

log "Configs installed to /etc/agnetic/"

# ─── 6. Install NATS config ────────────────────────────────────────
log "Installing NATS configuration..."

cp "$REPO_DIR/nats/agent-bus.conf" /etc/agnetic/nats/
cp "$REPO_DIR/nats/server.conf" /etc/agnetic/nats/ 2>/dev/null || true
cp "$REPO_DIR/nats/subjects.yaml" /etc/agnetic/nats/ 2>/dev/null || true

# Update NATS config to use correct paths
sed -i 's|/home/tech/agnetic-os/nats|/etc/agnetic/nats|g' /etc/agnetic/nats/agent-bus.conf 2>/dev/null || true

log "NATS config installed to /etc/agnetic/nats/"

# ─── 7. Create Python venv ─────────────────────────────────────────
log "Creating Python venv..."

python3 -m venv /opt/agnetic/venv
/opt/agnetic/venv/bin/pip install --upgrade pip -q
/opt/agnetic/venv/bin/pip install nats-py aiohttp httpx PyYAML lancedb ollama -q
log "Python venv created with dependencies"

# ─── 8. Install systemd units ──────────────────────────────────────
log "Installing systemd units..."

cp "$REPO_DIR/systemd/agnetic-nats.service" /etc/systemd/system/
cp "$REPO_DIR/systemd/agnetic-staragent.service" /etc/systemd/system/
cp "$REPO_DIR/systemd/agnetic-agent@.service" /etc/systemd/system/
cp "$REPO_DIR/systemd/agnetic-dashboard.service" /etc/systemd/system/
cp "$REPO_DIR/systemd/agnetic-status-bridge.service" /etc/systemd/system/
cp "$REPO_DIR/systemd/agnetic-message-history.service" /etc/systemd/system/
cp "$REPO_DIR/systemd/agnetic-mesh.target" /etc/systemd/system/

systemctl daemon-reload
log "Systemd units installed and daemon reloaded"

# ─── 9. Set ownership ─────────────────────────────────────────────
log "Setting ownership..."

chown -R agnetic:agnetic /opt/agnetic
chown -R agnetic:agnetic /etc/agnetic
chown -R agnetic:agnetic /var/lib/agnetic
chown -R agnetic:agnetic /var/log/agnetic
chown -R nats:nats /var/lib/agnetic/nats

log "Ownership set"

# ─── 10. Enable and start services ─────────────────────────────────
log "Enabling services..."

systemctl enable agnetic-nats.service
systemctl enable agnetic-staragent.service
systemctl enable agnetic-agent@proxy.service
systemctl enable agnetic-agent@romi.service
systemctl enable agnetic-agent@ergo.service
systemctl enable agnetic-status-bridge.service
systemctl enable agnetic-message-history.service
systemctl enable agnetic-dashboard.service
systemctl enable agnetic-mesh.target

log "All services enabled"

# ─── 11. Start services ────────────────────────────────────────────
log "Starting services..."

systemctl start agnetic-nats.service
sleep 2
systemctl start agnetic-staragent.service
sleep 1
systemctl start agnetic-agent@proxy.service
systemctl start agnetic-agent@romi.service
systemctl start agnetic-agent@ergo.service
systemctl start agnetic-status-bridge.service
systemctl start agnetic-message-history.service
systemctl start agnetic-dashboard.service

sleep 3
log "All services started"

# ─── 12. Verify ────────────────────────────────────────────────────
echo ""
echo -e "${BLUE}═══════════════════════════════════════════════${NC}"
echo -e "${BLUE}  Installation Complete — Service Status${NC}"
echo -e "${BLUE}═══════════════════════════════════════════════${NC}"
echo ""

for unit in agnetic-nats agnetic-staragent agnetic-agent@proxy agnetic-agent@romi agnetic-agent@ergo agnetic-status-bridge agnetic-message-history agnetic-dashboard; do
    status=$(systemctl is-active "$unit.service" 2>/dev/null || echo "inactive")
    if [[ "$status" == "active" ]]; then
        echo -e "  ${GREEN}●${NC} $unit.service — ${GREEN}running${NC}"
    else
        echo -e "  ${RED}●${NC} $unit.service — ${RED}$status${NC}"
    fi
done

echo ""
echo -e "  Dashboard: http://localhost:8788"
echo -e "  NATS:      nats://localhost:4222"
echo -e "  CLI:       agneticctl --help"
echo ""
echo -e "  Logs:      journalctl -u agnetic-* -f"
echo -e "  Status:    systemctl status agnetic-mesh.target"
echo ""
