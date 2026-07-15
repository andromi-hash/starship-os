#!/usr/bin/env bash
set -euo pipefail

# ─────────────────────────────────────────────────────────────────────
# Starship OS — Universal Installer
# "Installs on any hardware, feels warm and powerful"
# ─────────────────────────────────────────────────────────────────────

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; CYAN='\033[0;36m'; NC='\033[0m'
log()  { printf "${GREEN}==>${NC} %s\n" "$*"; }
warn() { printf "${YELLOW}==>${NC} %s\n" "$*"; }
err()  { printf "${RED}==>${NC} %s\n" "$*"; }
header() { printf "\n${CYAN}━━━ %s ━━━${NC}\n" "$*"; }

# ── Detect Hardware ──────────────────────────────────────────────────
header "Hardware Detection"
echo "  OS: $(uname -s) $(uname -r)"
echo "  Arch: $(uname -m)"
echo "  CPU: $(grep -m1 'model name' /proc/cpuinfo 2>/dev/null | cut -d: -f2 | sed 's/^ //' || echo 'unknown')"
echo "  Cores: $(nproc)"
TOTAL_RAM=$(free -m | awk '/^Mem:/{print $2}')
echo "  RAM: ${TOTAL_RAM}MB"
HAS_GPU=false
if command -v nvidia-smi &>/dev/null && nvidia-smi &>/dev/null; then
    HAS_GPU=true
    GPU_INFO=$(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits 2>/dev/null | head -1)
    GPU_NAME=$(echo "$GPU_INFO" | cut -d, -f1 | sed 's/^ //')
    GPU_MEM=$(echo "$GPU_INFO" | cut -d, -f2 | sed 's/^ //')
    echo "  GPU: ${GPU_NAME} (${GPU_MEM}MB VRAM)"
elif lspci 2>/dev/null | grep -qi 'vga.*intel'; then
    echo "  GPU: Intel Integrated"
    HAS_GPU=true
fi

# ── Auto-Configure Based on Hardware ─────────────────────────────────
header "Auto-Configuration"
if [ "$HAS_GPU" = true ] && [ "${GPU_MEM:-0}" -ge 4096 ]; then
    MODEL="qwen2.5:7b"
    EMBED_MODEL="nomic-embed-text"
    CONTEXT_LENGTH="32768"
    log "GPU detected with >=4GB VRAM — using 7B model with full GPU offload"
elif [ "$TOTAL_RAM" -ge 8192 ]; then
    MODEL="qwen2.5:3b"
    EMBED_MODEL="nomic-embed-text"
    CONTEXT_LENGTH="16384"
    log "No GPU or <4GB VRAM — using 3B model for CPU inference"
    warn "For best performance, consider a GPU with 6GB+ VRAM"
else
    MODEL="qwen2.5:1.5b"
    EMBED_MODEL="nomic-embed-text"
    CONTEXT_LENGTH="8192"
    log "Low-resource mode — using 1.5B model"
    warn "Recommend: 8GB+ RAM for comfortable usage"
fi

# ── Install System Dependencies ──────────────────────────────────────
header "System Dependencies"
if [ -f /etc/debian_version ]; then
    log "Debian/Ubuntu detected"
    sudo apt-get update -qq
    sudo apt-get install -y -qq python3 python3-pip python3-venv \
        curl wget git build-essential cmake lspci >/dev/null 2>&1
elif [ -f /etc/redhat-release ]; then
    log "RHEL/Fedora detected"
    sudo dnf install -y python3 python3-pip python3-virtualenv \
        curl wget git gcc-c++ make cmake >/dev/null 2>&1
elif [ -f /etc/alpine-release ]; then
    log "Alpine detected"
    sudo apk add python3 py3-pip curl wget git build-base cmake >/dev/null 2>&1
else
    warn "Unknown distro — installing Python packages only"
fi

# ── Install Ollama ─────────────────────────────────────────────────
header "Ollama (Local LLM Engine)"
if ! command -v ollama &>/dev/null; then
    log "Installing Ollama..."
    curl -fsSL https://ollama.com/install.sh | sh
else
    log "Ollama already installed ($(ollama --version 2>/dev/null || echo 'unknown'))"
fi

# ── Install Python Dependencies ──────────────────────────────────────
header "Python Dependencies"
log "Creating virtual environment..."
python3 -m venv /opt/agnetic/.venv 2>/dev/null || true
source /opt/agnetic/.venv/bin/activate 2>/dev/null || true
pip install -q httpx nats-py lancedb pyarrow numpy aiohttp 2>/dev/null || true
pip install -q anthropic openai 2>/dev/null || true

# ── Pull Models ──────────────────────────────────────────────────────
header "AI Models"
log "Pulling ${MODEL}..."
ollama pull "${MODEL}" 2>&1 | tail -1
log "Pulling ${EMBED_MODEL}..."
ollama pull "${EMBED_MODEL}" 2>&1 | tail -1

# ── Create System Users & Directories ────────────────────────────────
header "System Setup"
sudo mkdir -p /opt/agnetic /var/lib/agnetic /var/log/agnetic /etc/agnetic
sudo chmod 755 /opt/agnetic /var/lib/agnetic /var/log/agnetic
log "Directories created"

# ── Register Agnetic as a Systemd Service ───────────────────────────
header "System Service"
if [ -d /etc/systemd/system ]; then
    cat > /tmp/agnetic-core.service << 'EOF'
[Unit]
Description=Starship OS Core Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/agnetic
ExecStart=/usr/bin/python3 /opt/agnetic/lib/agent_daemon.py agnetic-core
Restart=always
RestartSec=10
Environment=AGNETIC_ROOT=/opt/agnetic
Environment=OLLAMA_HOST=http://127.0.0.1:11435
Environment=PYTHONPATH=/opt/agnetic

[Install]
WantedBy=multi-user.target
EOF
    cat > /tmp/agnetic-dashboard.service << 'EOF'
[Unit]
Description=Starship OS Dashboard
After=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/agnetic
ExecStart=/usr/bin/python3 /opt/agnetic/lib/dashboard/server.py
Restart=always
RestartSec=5
Environment=AGNETIC_DASHBOARD_PORT=8788
Environment=PYTHONPATH=/opt/agnetic

[Install]
WantedBy=multi-user.target
EOF
    sudo mv /tmp/agnetic-core.service /etc/systemd/system/
    sudo mv /tmp/agnetic-dashboard.service /etc/systemd/system/
    sudo systemctl daemon-reload
    log "Systemd services installed"
fi

# ── Create Default Config ───────────────────────────────────────────
header "Default Configuration"
if [ ! -f /etc/agnetic/policy.json ]; then
    cat > /tmp/policy.json << EOF
{
  "system": {
    "allow_network_access": true,
    "max_memory_mb": ${TOTAL_RAM},
    "log_level": "info",
    "audit_enabled": true
  },
  "service": {
    "rate_limit_per_min": 60,
    "max_concurrent_tasks": 10,
    "agent_timeout_seconds": 300
  },
  "user": {
    "override_policy": false,
    "custom_rules": []
  }
}
EOF
    sudo cp /tmp/policy.json /etc/agnetic/policy.json
    log "Policy configured"
fi

# ── Welcome Message ─────────────────────────────────────────────────
header "Install Complete"
echo ""
echo "  ${CYAN}╔══════════════════════════════════════════╗${NC}"
echo "  ${CYAN}║       ${GREEN}AGNETIC OS${NC} — Your Agent Mesh     ${CYAN}║${NC}"
echo "  ${CYAN}║       ${BLUE}Starship OS${NC} — Warm. Safe. Fast.   ${CYAN}║${NC}"
echo "  ${CYAN}╚══════════════════════════════════════════╝${NC}"
echo ""
echo "  Dashboard:  http://$(hostname -I 2>/dev/null | awk '{print $1}'):8788"
echo "  Agent Bus:  nats://127.0.0.1:4222"
echo "  LLM API:    http://127.0.0.1:11435"
echo "  Config:     /etc/agnetic/"
echo "  Logs:       /var/log/agnetic/"
echo "  Data:       /var/lib/agnetic/"
echo ""
echo "  ${YELLOW}Quick Start:${NC}"
echo "    sudo systemctl start agnetic-core  # Launch the orchestrator agent"
echo "    sudo systemctl start agnetic-dashboard  # Launch the Web UI"
echo "    firefox http://localhost:8788  # Open your mission control"
echo ""
echo "  ${YELLOW}Self-Healing:${NC}"
echo "    Both services auto-restart on failure (systemd Restart=always)"
echo "    Run 'agnetic-health' to check system status"
echo ""

# ── Install the health check command ────────────────────────────────
cat > /usr/local/bin/agnetic-health << 'HEAL'
#!/usr/bin/env bash
echo "╔══════════════════════════════════════╗"
echo "║  Starship OS — System Health Check    ║"
echo "╚══════════════════════════════════════╝"
echo ""
for svc in ollama agnetic-core agnetic-dashboard; do
    if systemctl is-active --quiet "$svc" 2>/dev/null; then
        echo "  \u2713 $svc: running"
    elif systemctl is-enabled --quiet "$svc" 2>/dev/null; then
        echo "  \u26A0 $svc: installed but inactive"
    else
        echo "  \u2717 $svc: not found"
    fi
done
echo ""
echo "Resources:"
echo "  CPU: $(grep 'cpu ' /proc/stat | awk '{usage=($2+$4)*100/($2+$4+$5)} END {printf "%.1f%%", usage}')"
echo "  RAM: $(free -m | awk '/^Mem:/{printf "%dMB / %dMB", $3, $2}')"
echo "  Disk: $(df -h / | awk 'NR==2{print $3 " / " $2 " (" $5 ")"}')"
if command -v nvidia-smi &>/dev/null; then
    echo "  GPU: $(nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total --format=csv,noheader,nounits 2>/dev/null | head -1 || echo 'N/A')"
fi
HEAL
chmod +x /usr/local/bin/agnetic-health
log "Health check command installed: agnetic-health"
