#!/usr/bin/env bash
# Starship OS — Linux Drone Agent Installer
# Installs staragent on a Linux endpoint as a systemd service.
# Designed for headless remote "drone" nodes connecting to a hub.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/andromi-hash/starship-os/master/scripts/install-agent-linux.sh | bash -s -- --nats-url nats://hub:4222 --nats-token YOUR_TOKEN
#
# Or download and run locally:
#   ./install-agent-linux.sh --nats-url nats://10.0.0.1:4222 --nats-token abc123

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
log()  { echo -e "${GREEN}[INSTALL]${NC} $*"; }
warn() { echo -e "${YELLOW}[INSTALL]${NC} $*"; }
err()  { echo -e "${RED}[INSTALL]${NC} $*" >&2; exit 1; }

# ─── Parse args ────────────────────────────────────────────────────
NATS_URL=""
NATS_TOKEN=""
HOSTNAME=""
SKIP_BINARY=false
BINARY_SOURCE=""
DOWNLOAD_BASE_URL=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --nats-url)       NATS_URL="$2"; shift 2 ;;
        --nats-token)     NATS_TOKEN="$2"; shift 2 ;;
        --hostname)       HOSTNAME="$2"; shift 2 ;;
        --skip-binary)    SKIP_BINARY=true; shift ;;
        --binary)         BINARY_SOURCE="$2"; shift 2 ;;
        --download-url)   DOWNLOAD_BASE_URL="$2"; shift 2 ;;
        --help)           echo "Usage: $0 --nats-url <url> --nats-token <token> [--hostname <name>] [--binary <path>] [--download-url <url>]"; exit 0 ;;
        *)                err "Unknown argument: $1 (use --help)" ;;
    esac
done

if [[ -z "$NATS_URL" ]]; then
    echo
    echo "Starship OS — Linux Drone Agent Installer"
    echo "=========================================="
    echo
    echo "Enter your Starship OS Hub connection details:"
    echo
    read -rp "  NATS Hub URL [nats://10.0.0.1:4222]: " input_url
    NATS_URL="${input_url:-nats://10.0.0.1:4222}"
    read -rp "  NATS Token (from hub /etc/starship/nats/fleet-bus.conf): " NATS_TOKEN
    read -rp "  Agent hostname [auto]: " HOSTNAME
    echo
fi

# ─── Pre-flight ────────────────────────────────────────────────────
if [[ "$(id -u)" != "0" ]]; then
    err "Must run as root (use sudo)"
fi

ARCH=$(uname -m)
OS=$(uname -s)
log "Detected: $OS $ARCH"

INSTALL_DIR="/opt/starship"
CONFIG_DIR="/etc/starship/agents"
LOG_DIR="/var/log/starship"

# ─── Get the staragent binary ──────────────────────────────────────
if [[ "$SKIP_BINARY" == "true" ]]; then
    log "Skipping binary installation (--skip-binary)"
elif [[ -n "$BINARY_SOURCE" ]]; then
    log "Using pre-built binary from: $BINARY_SOURCE"
    mkdir -p "$INSTALL_DIR/bin"
    cp "$BINARY_SOURCE" "$INSTALL_DIR/bin/staragent"
    chmod 755 "$INSTALL_DIR/bin/staragent"
else
    # Determine download URL
    case "$OS $ARCH" in
        "Linux x86_64")  BINARY="staragent-linux-x86_64"; PLATFORM="linux" ;;
        "Linux aarch64") BINARY="staragent-linux-aarch64"; PLATFORM="linux" ;;
        "Linux armv7l")  BINARY="staragent-linux-armv7";  PLATFORM="linux" ;;
        *) err "Unsupported platform: $OS $ARCH" ;;
    esac

    if [[ -n "$DOWNLOAD_BASE_URL" ]]; then
        DOWNLOAD_URL="${DOWNLOAD_BASE_URL}/${PLATFORM}?token=${NATS_TOKEN}"
        log "Downloading staragent from hub..."
    else
        DOWNLOAD_URL="https://github.com/andromi-hash/starship-os/releases/latest/download/${BINARY}.tar.gz"
        log "Downloading staragent from GitHub releases..."
    fi
    log "  $DOWNLOAD_URL"

    mkdir -p "$INSTALL_DIR/bin"
    TMP_DIR=$(mktemp -d)
    if curl -fsSL "$DOWNLOAD_URL" -o "$TMP_DIR/staragent.tar.gz" 2>/dev/null; then
        tar xzf "$TMP_DIR/staragent.tar.gz" -C "$TMP_DIR"
        if [[ -f "$TMP_DIR/staragent" ]]; then
            cp "$TMP_DIR/staragent" "$INSTALL_DIR/bin/staragent"
            chmod 755 "$INSTALL_DIR/bin/staragent"
            log "Binary downloaded and installed"
        else
            rm -rf "$TMP_DIR"
            err "Downloaded archive does not contain staragent binary"
        fi
        rm -rf "$TMP_DIR"
    else
        rm -rf "$TMP_DIR"
        warn "Could not download release binary."
        warn "Build it manually: cd agent && cargo build --release"
        warn "Then re-run with: --binary ./agent/target/release/staragent"
        warn "Or download from: https://github.com/andromi-hash/starship-os/releases"
        exit 1
    fi
fi

# ─── Create config ─────────────────────────────────────────────────
log "Creating config..."
mkdir -p "$CONFIG_DIR" "$LOG_DIR"

HOSTNAME_CFG=""
if [[ -n "$HOSTNAME" ]]; then
    HOSTNAME_CFG="hostname: \"$HOSTNAME\""
fi

cat > "$CONFIG_DIR/staragent.yaml" <<YAMLEOF
# Starship OS — StarAgent Configuration
# Installed by install-agent-linux.sh

nats:
  url: "$NATS_URL"
  token: "$NATS_TOKEN"

telemetry:
  interval_secs: 10

commands:
  subscribe:
    - "starship.agent.staragent.command.>"
    - "agnetic.agent.staragent.command.>"

$HOSTNAME_CFG
YAMLEOF

chmod 644 "$CONFIG_DIR/staragent.yaml"
log "Config written to $CONFIG_DIR/staragent.yaml"

# ─── Create systemd service ────────────────────────────────────────
log "Installing systemd service..."
cat > /etc/systemd/system/agnetic-staragent.service <<UNIT
[Unit]
Description=Starship OS - StarAgent Telemetry Collector
After=network.target
Documentation=https://github.com/andromi-hash/starship-os

[Service]
Type=simple
ExecStart=$INSTALL_DIR/bin/staragent
Restart=always
RestartSec=5
Environment=RUST_LOG=info
Environment=STARSHIP_ROOT=$INSTALL_DIR
Environment=STARAGENT_CONFIG=$CONFIG_DIR/staragent.yaml
NoNewPrivileges=true
ProtectSystem=full
ReadWritePaths=$LOG_DIR /tmp
ProtectHome=true

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable agnetic-staragent.service
systemctl start agnetic-staragent.service

log "Systemd service installed and started"

# ─── Verify ────────────────────────────────────────────────────────
sleep 2
SERVICE_STATUS=$(systemctl is-active agnetic-staragent.service 2>/dev/null || echo "inactive")
if [[ "$SERVICE_STATUS" == "active" ]]; then
    echo
    echo -e "${GREEN}============================================${NC}"
    echo -e "${GREEN}  Installation Complete — Agent Online${NC}"
    echo -e "${GREEN}============================================${NC}"
    echo
    echo "  Binary:    $INSTALL_DIR/bin/staragent"
    echo "  Config:    $CONFIG_DIR/staragent.yaml"
    echo "  Logs:      $LOG_DIR"
    echo "  Service:   agnetic-staragent.service"
    echo
    echo "  Dashboard: Open Shield tab (⛨) on your hub to see this node."
    echo "  Logs:      journalctl -u agnetic-staragent -f"
    echo
else
    warn "Service installed but not active. Check: journalctl -u agnetic-staragent -n 50"
fi
