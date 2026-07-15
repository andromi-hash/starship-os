#!/usr/bin/env bash
# Starship OS — Debian Package Builder
# Builds a correct .deb layout: DEBIAN/ + opt/ + etc/ + lib/ + usr/
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
CONTROL_SRC="$REPO_DIR/debian/DEBIAN"
OUTPUT_DIR="$REPO_DIR/dist"
VERSION=$(grep "^Version:" "$CONTROL_SRC/control" | awk '{print $2}')
# Staging root (must contain DEBIAN + filesystem paths at top level)
PKG_ROOT="$REPO_DIR/dist/pkgroot"
export PATH="/tmp/go/bin:${HOME}/.cargo/bin:${PATH:-/usr/bin}"

echo -e "${BLUE}╔══════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║  Starship OS — Debian Builder       ║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════════╝${NC}"
echo ""

if [[ ! -f "$CONTROL_SRC/control" ]]; then
    err "debian/DEBIAN/control not found. Run from repo root."
fi

if [[ ! -f "$REPO_DIR/starshipctl/starshipctl" ]]; then
    warn "starshipctl not built — building now..."
    (cd "$REPO_DIR/starshipctl" && go build -o starshipctl .)
fi

if [[ ! -f "$REPO_DIR/agent/target/release/staragent" ]]; then
    warn "staragent not built — building now..."
    (cd "$REPO_DIR/agent" && cargo build --release)
fi

if [[ ! -x "$REPO_DIR/src/c/sandbox_spike/sandbox_run" ]]; then
    make -C "$REPO_DIR/src/c/sandbox_spike" all 2>/dev/null || true
fi

# ─── Assemble package root ──────────────────────────────────────────
rm -rf "$PKG_ROOT"
mkdir -p "$PKG_ROOT/DEBIAN"
mkdir -p "$PKG_ROOT/opt/starship/bin"
mkdir -p "$PKG_ROOT/opt/starship/lib/starship/agents/skills"
mkdir -p "$PKG_ROOT/opt/starship/lib/starship/dashboard"
mkdir -p "$PKG_ROOT/opt/starship/lib/starship/tray"
mkdir -p "$PKG_ROOT/opt/starship/lib/starship/scripts"
mkdir -p "$PKG_ROOT/opt/starship/lib/starship/skills"
mkdir -p "$PKG_ROOT/opt/starship/lib/starship/souls"
mkdir -p "$PKG_ROOT/opt/starship/lib/starship/services"
mkdir -p "$PKG_ROOT/etc/starship/nats"
mkdir -p "$PKG_ROOT/etc/starship/opencode"
mkdir -p "$PKG_ROOT/lib/systemd/system"
mkdir -p "$PKG_ROOT/usr/local/bin"
mkdir -p "$PKG_ROOT/var/lib/starship/nats"
mkdir -p "$PKG_ROOT/var/log/starship"

# Control scripts
cp "$CONTROL_SRC/control" "$PKG_ROOT/DEBIAN/"
cp "$CONTROL_SRC/postinst" "$PKG_ROOT/DEBIAN/" 2>/dev/null || true
cp "$CONTROL_SRC/prerm" "$PKG_ROOT/DEBIAN/" 2>/dev/null || true
cp "$CONTROL_SRC/postrm" "$PKG_ROOT/DEBIAN/" 2>/dev/null || true
chmod 755 "$PKG_ROOT/DEBIAN/postinst" "$PKG_ROOT/DEBIAN/prerm" "$PKG_ROOT/DEBIAN/postrm" 2>/dev/null || true
chmod 644 "$PKG_ROOT/DEBIAN/control"

log "Assembling package files into $PKG_ROOT ..."

# Binaries
cp "$REPO_DIR/starshipctl/starshipctl" "$PKG_ROOT/opt/starship/bin/"
ln -sf /opt/starship/bin/starshipctl "$PKG_ROOT/usr/local/bin/starshipctl"
ln -sf /opt/starship/bin/starshipctl "$PKG_ROOT/usr/local/bin/agneticctl"
cp "$REPO_DIR/agent/target/release/staragent" "$PKG_ROOT/opt/starship/bin/"
cp "$REPO_DIR/scripts/detect-gpu.sh" "$PKG_ROOT/opt/starship/bin/"
cp "$REPO_DIR/scripts/starship-firstboot.sh" "$PKG_ROOT/opt/starship/bin/"
cp "$REPO_DIR/scripts/select-profile.sh" "$PKG_ROOT/opt/starship/bin/" 2>/dev/null || true
cp "$REPO_DIR/scripts/gen-nats-accounts.sh" "$PKG_ROOT/opt/starship/bin/" 2>/dev/null || true
cp "$REPO_DIR/scripts/gen-nats-tls.sh" "$PKG_ROOT/opt/starship/bin/" 2>/dev/null || true
if [[ -x "$REPO_DIR/src/c/sandbox_spike/sandbox_run" ]]; then
    cp "$REPO_DIR/src/c/sandbox_spike/sandbox_run" "$PKG_ROOT/opt/starship/bin/"
    ln -sf /opt/starship/bin/sandbox_run "$PKG_ROOT/usr/local/bin/sandbox_run"
fi

# Python / services
cp "$REPO_DIR/agents/agent_daemon.py" "$PKG_ROOT/opt/starship/lib/starship/agents/"
for f in nats_subjects.py nats_connect.py sandbox_native.py fleet_policy.py tools.py security.py \
         run_agent.sh scheduler.py workflows.py; do
    cp "$REPO_DIR/agents/$f" "$PKG_ROOT/opt/starship/lib/starship/agents/" 2>/dev/null || true
done
cp -r "$REPO_DIR/agents/skills/"* "$PKG_ROOT/opt/starship/lib/starship/agents/skills/" 2>/dev/null || true
cp "$REPO_DIR/services/fleet.py" "$PKG_ROOT/opt/starship/lib/starship/services/" 2>/dev/null || true

cp "$REPO_DIR/dashboard/server.py" "$PKG_ROOT/opt/starship/lib/starship/dashboard/"
cp "$REPO_DIR/dashboard/index.html" "$PKG_ROOT/opt/starship/lib/starship/dashboard/"
cp "$REPO_DIR/tray/agnetic-status.py" "$PKG_ROOT/opt/starship/lib/starship/tray/" 2>/dev/null || true

cp "$REPO_DIR/scripts/message_history.py" "$PKG_ROOT/opt/starship/lib/starship/scripts/"
cp "$REPO_DIR/scripts/starship-firstboot.sh" "$PKG_ROOT/opt/starship/lib/starship/scripts/"
cp "$REPO_DIR/scripts/gen-nats-accounts.sh" "$PKG_ROOT/opt/starship/lib/starship/scripts/" 2>/dev/null || true
cp "$REPO_DIR/scripts/gen-nats-tls.sh" "$PKG_ROOT/opt/starship/lib/starship/scripts/" 2>/dev/null || true

cp -r "$REPO_DIR/skills/"* "$PKG_ROOT/opt/starship/lib/starship/skills/" 2>/dev/null || true
cp -r "$REPO_DIR/souls/"* "$PKG_ROOT/opt/starship/lib/starship/souls/" 2>/dev/null || true

# Configs
cp "$REPO_DIR/nats/agent-bus.conf" "$PKG_ROOT/etc/starship/nats/"
cp "$REPO_DIR/nats/fleet-bus.conf" "$PKG_ROOT/etc/starship/nats/" 2>/dev/null || true
cp "$REPO_DIR/nats/fleet-auth.yaml" "$PKG_ROOT/etc/starship/nats/" 2>/dev/null || true
cp "$REPO_DIR/nats/fleet-accounts.conf.tmpl" "$PKG_ROOT/etc/starship/nats/" 2>/dev/null || true
cp "$REPO_DIR/nats/server.conf" "$PKG_ROOT/etc/starship/nats/" 2>/dev/null || true
cp "$REPO_DIR/nats/subjects.yaml" "$PKG_ROOT/etc/starship/nats/" 2>/dev/null || true
ln -sfn /etc/starship/nats/agent-bus.conf "$PKG_ROOT/etc/starship/nats/active.conf"
cp "$REPO_DIR/config/fleet.yaml" "$PKG_ROOT/etc/starship/" 2>/dev/null || true
cp "$REPO_DIR/config/profiles.yaml" "$PKG_ROOT/etc/starship/" 2>/dev/null || true
for f in config.yaml proxy.yaml romi.yaml ergo.yaml orchestrator.yaml; do
    cp "$REPO_DIR/agents/$f" "$PKG_ROOT/etc/starship/" 2>/dev/null || true
done

# Systemd
for u in agnetic-nats.service agnetic-staragent.service agnetic-agent@.service \
         agnetic-dashboard.service agnetic-status-bridge.service \
         agnetic-message-history.service agnetic-mesh.target starship-fleet.service; do
    cp "$REPO_DIR/systemd/$u" "$PKG_ROOT/lib/systemd/system/" 2>/dev/null || true
done

# Permissions
chmod 755 "$PKG_ROOT/opt/starship/bin/"* 2>/dev/null || true
chmod 755 "$PKG_ROOT/opt/starship/lib/starship/agents/agent_daemon.py" 2>/dev/null || true
chmod 755 "$PKG_ROOT/opt/starship/lib/starship/agents/run_agent.sh" 2>/dev/null || true
chmod 755 "$PKG_ROOT/opt/starship/lib/starship/dashboard/server.py" 2>/dev/null || true
chmod 755 "$PKG_ROOT/opt/starship/lib/starship/services/fleet.py" 2>/dev/null || true
chmod 755 "$PKG_ROOT/opt/starship/lib/starship/scripts/"* 2>/dev/null || true

# Legacy symlinks in package (postinst also creates live ones)
ln -sfn /opt/starship "$PKG_ROOT/opt/agnetic" 2>/dev/null || true

log "Package files assembled"

# ─── Validate layout ────────────────────────────────────────────────
for need in \
    "DEBIAN/control" \
    "opt/starship/bin/starshipctl" \
    "opt/starship/bin/staragent" \
    "opt/starship/bin/sandbox_run" \
    "opt/starship/bin/starship-firstboot.sh" \
    "opt/starship/lib/starship/agents/agent_daemon.py" \
    "opt/starship/lib/starship/services/fleet.py" \
    "etc/starship/fleet.yaml" \
    "etc/starship/nats/agent-bus.conf" \
    "lib/systemd/system/starship-fleet.service" \
    "usr/local/bin/starshipctl"; do
    if [[ ! -e "$PKG_ROOT/$need" && ! -L "$PKG_ROOT/$need" ]]; then
        err "missing required path in package: $need"
    fi
done
# active.conf is a symlink (target may not exist until install)
if [[ ! -L "$PKG_ROOT/etc/starship/nats/active.conf" ]]; then
    err "missing required symlink: etc/starship/nats/active.conf"
fi
# Reject nested installed/ mistake
if [[ -d "$PKG_ROOT/installed" ]]; then
    err "invalid layout: installed/ must not appear in package root"
fi
log "Layout validation OK"

# ─── Build .deb ─────────────────────────────────────────────────────
mkdir -p "$OUTPUT_DIR"
DEB_NAME="starship-os_${VERSION}_amd64.deb"
# root-owner for reproducible package metadata when run as root; else current user
dpkg-deb --root-owner-group --build "$PKG_ROOT" "$OUTPUT_DIR/$DEB_NAME" 2>/dev/null \
  || dpkg-deb --build "$PKG_ROOT" "$OUTPUT_DIR/$DEB_NAME"

log "Built: $OUTPUT_DIR/$DEB_NAME"

# ─── Post-build verify (avoid pipefail+grep -q SIGPIPE) ─────────────
LIST=$(mktemp)
dpkg-deb -c "$OUTPUT_DIR/$DEB_NAME" > "$LIST"
if grep -q './installed/' "$LIST"; then
    rm -f "$LIST"
    err "package still contains nested ./installed/ paths"
fi
if ! grep -q 'opt/starship/bin/starshipctl' "$LIST"; then
    rm -f "$LIST"
    err "package missing opt/starship/bin/starshipctl"
fi
FILE_COUNT=$(wc -l < "$LIST")
rm -f "$LIST"

echo ""
echo -e "${BLUE}═══════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Package built successfully!${NC}"
echo -e "${BLUE}═══════════════════════════════════════════════${NC}"
echo ""
echo -e "  Package:  $OUTPUT_DIR/$DEB_NAME"
echo -e "  Size:     $(du -h "$OUTPUT_DIR/$DEB_NAME" | cut -f1)"
echo -e "  Version:  $VERSION"
echo -e "  Files:    ${FILE_COUNT:-?}"
echo ""
echo -e "  Install:  sudo dpkg -i $OUTPUT_DIR/$DEB_NAME"
echo -e "  Remove:   sudo dpkg -r starship-os"
echo ""
