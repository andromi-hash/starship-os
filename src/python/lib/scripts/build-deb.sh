#!/usr/bin/env bash
# Starship OS — Debian Package Builder
# Builds .deb from the repo. Must run from repo root.
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log()  { echo -e "${GREEN}[BUILD]${NC} $*"; }
warn() { echo -e "${YELLOW}[BUILD]${NC} $*"; }
err()  { echo -e "${RED}[BUILD]${NC} $*" >&2; exit 1; }

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$REPO_DIR/debian"
OUTPUT_DIR="$REPO_DIR/dist"
VERSION=$(grep "^Version:" "$BUILD_DIR/DEBIAN/control" | awk '{print $2}')

echo -e "${BLUE}╔══════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║  Starship OS — Debian Builder       ║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════════╝${NC}"
echo ""

# ─── Pre-flight ─────────────────────────────────────────────────────
if [[ ! -f "$BUILD_DIR/DEBIAN/control" ]]; then
    err "debian/DEBIAN/control not found. Run from repo root."
fi

if [[ ! -f "$REPO_DIR/agneticctl/agneticctl" ]]; then
    warn "agneticctl not built — building now..."
    cd "$REPO_DIR/agneticctl" && go build -o agneticctl .
    cd "$REPO_DIR"
fi

if [[ ! -f "$REPO_DIR/agent/target/release/staragent" ]]; then
    warn "staragent not built — building now..."
    cd "$REPO_DIR/agent" && cargo build --release
    cd "$REPO_DIR"
fi

# ─── Assemble installed/ tree ──────────────────────────────────────
INSTALLED="$BUILD_DIR/installed"
rm -rf "$INSTALLED"
mkdir -p "$INSTALLED/opt/agnetic/bin"
mkdir -p "$INSTALLED/opt/agnetic/lib/agnetic/agents/skills"
mkdir -p "$INSTALLED/opt/agnetic/lib/agnetic/dashboard"
mkdir -p "$INSTALLED/opt/agnetic/lib/agnetic/tray"
mkdir -p "$INSTALLED/opt/agnetic/lib/agnetic/scripts"
mkdir -p "$INSTALLED/opt/agnetic/lib/agnetic/skills"
mkdir -p "$INSTALLED/opt/agnetic/lib/agnetic/souls"
mkdir -p "$INSTALLED/etc/agnetic/nats"
mkdir -p "$INSTALLED/lib/systemd/system"

log "Assembling package files..."

# Binaries
cp "$REPO_DIR/agneticctl/agneticctl" "$INSTALLED/opt/agnetic/bin/"
cp "$REPO_DIR/agent/target/release/staragent" "$INSTALLED/opt/agnetic/bin/"
cp "$REPO_DIR/scripts/detect-gpu.sh" "$INSTALLED/opt/agnetic/bin/"

# Python application code
cp "$REPO_DIR/agents/agent_daemon.py" "$INSTALLED/opt/agnetic/lib/agnetic/agents/"
cp "$REPO_DIR/agents/run_agent.sh" "$INSTALLED/opt/agnetic/lib/agnetic/agents/" 2>/dev/null || true
cp "$REPO_DIR/agents/scheduler.py" "$INSTALLED/opt/agnetic/lib/agnetic/agents/" 2>/dev/null || true
cp "$REPO_DIR/agents/workflows.py" "$INSTALLED/opt/agnetic/lib/agnetic/agents/" 2>/dev/null || true
cp -r "$REPO_DIR/agents/skills/"* "$INSTALLED/opt/agnetic/lib/agnetic/agents/skills/" 2>/dev/null || true

cp "$REPO_DIR/dashboard/server.py" "$INSTALLED/opt/agnetic/lib/agnetic/dashboard/"
cp "$REPO_DIR/dashboard/index.html" "$INSTALLED/opt/agnetic/lib/agnetic/dashboard/"

cp "$REPO_DIR/tray/agnetic-status.py" "$INSTALLED/opt/agnetic/lib/agnetic/tray/"

cp "$REPO_DIR/scripts/message_history.py" "$INSTALLED/opt/agnetic/lib/agnetic/scripts/"

cp -r "$REPO_DIR/skills/"* "$INSTALLED/opt/agnetic/lib/agnetic/skills/" 2>/dev/null || true
cp -r "$REPO_DIR/souls/"* "$INSTALLED/opt/agnetic/lib/agnetic/souls/" 2>/dev/null || true

# Configs
cp "$REPO_DIR/nats/agent-bus.conf" "$INSTALLED/etc/agnetic/nats/"
cp "$REPO_DIR/nats/server.conf" "$INSTALLED/etc/agnetic/nats/" 2>/dev/null || true
cp "$REPO_DIR/nats/subjects.yaml" "$INSTALLED/etc/agnetic/nats/" 2>/dev/null || true
cp "$REPO_DIR/agents/config.yaml" "$INSTALLED/etc/agnetic/" 2>/dev/null || true
cp "$REPO_DIR/agents/proxy.yaml" "$INSTALLED/etc/agnetic/" 2>/dev/null || true
cp "$REPO_DIR/agents/romi.yaml" "$INSTALLED/etc/agnetic/" 2>/dev/null || true
cp "$REPO_DIR/agents/ergo.yaml" "$INSTALLED/etc/agnetic/" 2>/dev/null || true
cp "$REPO_DIR/agents/orchestrator.yaml" "$INSTALLED/etc/agnetic/" 2>/dev/null || true

# Systemd units
cp "$REPO_DIR/systemd/agnetic-nats.service" "$INSTALLED/lib/systemd/system/"
cp "$REPO_DIR/systemd/agnetic-staragent.service" "$INSTALLED/lib/systemd/system/"
cp "$REPO_DIR/systemd/agnetic-agent@.service" "$INSTALLED/lib/systemd/system/"
cp "$REPO_DIR/systemd/agnetic-dashboard.service" "$INSTALLED/lib/systemd/system/"
cp "$REPO_DIR/systemd/agnetic-status-bridge.service" "$INSTALLED/lib/systemd/system/"
cp "$REPO_DIR/systemd/agnetic-message-history.service" "$INSTALLED/lib/systemd/system/"
cp "$REPO_DIR/systemd/agnetic-mesh.target" "$INSTALLED/lib/systemd/system/"

# Set permissions
chmod 755 "$INSTALLED/opt/agnetic/bin/"*
chmod 755 "$INSTALLED/opt/agnetic/lib/agnetic/agents/agent_daemon.py" 2>/dev/null || true
chmod 755 "$INSTALLED/opt/agnetic/lib/agnetic/agents/run_agent.sh" 2>/dev/null || true
chmod 755 "$INSTALLED/opt/agnetic/lib/agnetic/dashboard/server.py" 2>/dev/null || true
chmod 755 "$INSTALLED/opt/agnetic/lib/agnetic/tray/agnetic-status.py" 2>/dev/null || true
chmod 755 "$INSTALLED/opt/agnetic/lib/agnetic/scripts/message_history.py" 2>/dev/null || true

log "Package files assembled"

# ─── Build .deb ─────────────────────────────────────────────────────
mkdir -p "$OUTPUT_DIR"

DEB_NAME="agnet-os_${VERSION}_amd64.deb"

# Use dpkg-deb to build (no root needed)
dpkg-deb --build "$BUILD_DIR" "$OUTPUT_DIR/$DEB_NAME"

log "Built: $OUTPUT_DIR/$DEB_NAME"

# ─── Summary ────────────────────────────────────────────────────────
echo ""
echo -e "${BLUE}═══════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Package built successfully!${NC}"
echo -e "${BLUE}═══════════════════════════════════════════════${NC}"
echo ""
echo -e "  Package:  $OUTPUT_DIR/$DEB_NAME"
echo -e "  Size:     $(du -h "$OUTPUT_DIR/$DEB_NAME" | cut -f1)"
echo -e "  Version:  $VERSION"
echo ""
echo -e "  Install:  sudo dpkg -i $OUTPUT_DIR/$DEB_NAME"
echo -e "  Remove:   sudo dpkg -r agnetic-os"
echo ""
