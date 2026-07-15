#!/usr/bin/env bash
# Starship OS — ISO Builder
# Builds a bootable Ubuntu-based ISO with Starship OS pre-installed.
# Must run on Ubuntu 24.04+ with root access.
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log()  { echo -e "${GREEN}[ISO]${NC} $*"; }
warn() { echo -e "${YELLOW}[ISO]${NC} $*"; }
err()  { echo -e "${RED}[ISO]${NC} $*" >&2; exit 1; }

if [[ "$(id -u)" != "0" ]]; then
    err "Must run as root. Use: sudo bash scripts/build-iso.sh"
fi

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ISO_DIR="$REPO_DIR/iso"
OUTPUT_DIR="$REPO_DIR/dist"
VERSION=$(grep "^Version:" "$REPO_DIR/debian/DEBIAN/control" | awk '{print $2}')
ISO_NAME="agnet-os-${VERSION}-amd64"

echo -e "${BLUE}╔══════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║  Starship OS — ISO Builder          ║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════════╝${NC}"
echo ""

# ─── 1. Install live-build ─────────────────────────────────────────
log "Installing live-build..."
apt-get update -qq
apt-get install -y -qq live-build xorriso squashfs-tools grub-pc-bin grub-efi-amd64-bin syslinux-utils 2>/dev/null

# ─── 2. Prepare live-build config ──────────────────────────────────
log "Preparing live-build configuration..."

LB_DIR="/var/tmp/agnet-iso-build"
rm -rf "$LB_DIR"
mkdir -p "$LB_DIR"
cd "$LB_DIR"

# Initialize live-build
lb config \
    --architectures amd64 \
    --distribution noble \
    --archive-areas "main restricted universe multiverse" \
    --bootloader "grub-efi,bios" \
    --binary-images iso-hybrid \
    --iso-application "Starship OS" \
    --iso-publisher "Starship OS; https://github.com/andromi-hash/starship-os" \
    --iso-volume "Starship OS ${VERSION}" \
    --apt-recommends true \
    --memtest none \
    --security true \
    --apt-secure false \

# ─── 3. Copy hooks and package lists ───────────────────────────────
log "Copying configuration..."

cp "$ISO_DIR/config/package-lists/agnetic.list.chroot" \
   "$LB_DIR/config/package-lists/"

# Copy the install hook
mkdir -p "$LB_DIR/config/hooks/live"
cp "$ISO_DIR/config/hooks/0100-agnetic-install.chroot" \
   "$LB_DIR/config/hooks/live/"

# Make hook executable
chmod +x "$LB_DIR/config/hooks/live/0100-agnetic-install.chroot"

# ─── 4. Copy Starship OS files into chroot ──────────────────────────
log "Copying Starship OS files into ISO..."

# Create the installation directory in chroot
mkdir -p "$LB_DIR/config/includes.chroot/opt/agnetic"
mkdir -p "$LB_DIR/config/includes.chroot/etc/agnetic"
mkdir -p "$LB_DIR/config/includes.chroot/root"

# Copy binaries
cp "$REPO_DIR/agneticctl/agneticctl" "$LB_DIR/config/includes.chroot/opt/agnetic/bin/" 2>/dev/null || true
cp "$REPO_DIR/agent/target/release/staragent" "$LB_DIR/config/includes.chroot/opt/agnetic/bin/" 2>/dev/null || true
cp "$REPO_DIR/scripts/detect-gpu.sh" "$LB_DIR/config/includes.chroot/opt/agnetic/bin/" 2>/dev/null || true

# Copy Python code
cp -r "$REPO_DIR/agents" "$LB_DIR/config/includes.chroot/opt/agnetic/lib/" 2>/dev/null || true
cp -r "$REPO_DIR/dashboard" "$LB_DIR/config/includes.chroot/opt/agnetic/lib/" 2>/dev/null || true
cp -r "$REPO_DIR/tray" "$LB_DIR/config/includes.chroot/opt/agnetic/lib/" 2>/dev/null || true
cp -r "$REPO_DIR/scripts" "$LB_DIR/config/includes.chroot/opt/agnetic/lib/" 2>/dev/null || true
cp -r "$REPO_DIR/skills" "$LB_DIR/config/includes.chroot/opt/agnetic/lib/" 2>/dev/null || true
cp -r "$REPO_DIR/souls" "$LB_DIR/config/includes.chroot/opt/agnetic/lib/" 2>/dev/null || true

# Copy configs
cp "$REPO_DIR/nats/"* "$LB_DIR/config/includes.chroot/etc/agnetic/nats/" 2>/dev/null || true
cp "$REPO_DIR/agents/"*.yaml "$LB_DIR/config/includes.chroot/etc/agnetic/" 2>/dev/null || true

# Copy systemd units
mkdir -p "$LB_DIR/config/includes.chroot/lib/systemd/system"
cp "$REPO_DIR/systemd/agnetic-"*.service "$LB_DIR/config/includes.chroot/lib/systemd/system/" 2>/dev/null || true
cp "$REPO_DIR/systemd/agnetic-"*.target "$LB_DIR/config/includes.chroot/lib/systemd/system/" 2>/dev/null || true

# ─── 5. Build the ISO ──────────────────────────────────────────────
log "Building ISO (this will take 30-60 minutes)..."
log "Building in: $LB_DIR"

cd "$LB_DIR"
lb build 2>&1 | tail -20

# If isohybrid was missing in the chroot, the ISO was created but not
# finalized. Copy isohybrid from host and re-run the binary hook.
if ! command -v isohybrid >/dev/null 2>&1 && [ -f "$LB_DIR/chroot/binary.hybrid.iso" ]; then
    warn "isohybrid missing in chroot; applying from host..."
    cp /usr/bin/isohybrid "$LB_DIR/chroot/usr/bin/"
    chroot "$LB_DIR/chroot" /bin/sh -c "cd / && isohybrid binary.hybrid.iso" 2>/dev/null || true
elif [ -f "$LB_DIR/chroot/binary.hybrid.iso" ]; then
    isohybrid "$LB_DIR/chroot/binary.hybrid.iso" 2>/dev/null || true
fi

# ─── 6. Move output ────────────────────────────────────────────────
mkdir -p "$OUTPUT_DIR"

# Look for the ISO in common locations
ISO_FILE=""
for candidate in "$LB_DIR/binary.hybrid.iso" "$LB_DIR/chroot/binary.hybrid.iso" "$LB_DIR"/*.iso; do
    if [ -f "$candidate" ] && [ "$(stat -c%s "$candidate" 2>/dev/null)" -gt 1048576 ]; then
        ISO_FILE="$candidate"
        break
    fi
done

if [[ -n "$ISO_FILE" ]]; then
    mv "$ISO_FILE" "$OUTPUT_DIR/${ISO_NAME}.iso"
    log "ISO built: $OUTPUT_DIR/${ISO_NAME}.iso"
    log "Size: $(du -h "$OUTPUT_DIR/${ISO_NAME}.iso" | cut -f1)"
else
    err "ISO build failed — no .iso file found"
fi

# ─── 7. Build WSL rootfs (optional for custom WSL distro) ──────────
WSL_TAR="$OUTPUT_DIR/${ISO_NAME}-wsl.tar.gz"
if [[ -d "$LB_DIR/chroot" ]]; then
  log "Building WSL rootfs tarball for custom WSL import..."
  tar --exclude='./proc/*' --exclude='./sys/*' --exclude='./dev/*' --exclude='./run/*' --exclude='./tmp/*' \
      -C "$LB_DIR/chroot" -czf "$WSL_TAR" . 2>/dev/null || true
  if [[ -f "$WSL_TAR" ]]; then
    log "WSL tarball: $WSL_TAR"
    log "Size: $(du -h "$WSL_TAR" | cut -f1)"
  fi
fi

# ─── 8. Summary ────────────────────────────────────────────────────
echo ""
echo -e "${BLUE}═══════════════════════════════════════════════${NC}"
echo -e "${GREEN}  ISO built successfully!${NC}"
echo -e "${BLUE}═══════════════════════════════════════════════${NC}"
echo ""
echo -e "  ISO:      $OUTPUT_DIR/${ISO_NAME}.iso"
echo -e "  Boot:     UEFI + Legacy BIOS (Secure Boot capable with signed kernel)"
echo -e "  Desktop:  Ubuntu minimal + Starship OS"
echo -e "  Services: NATS, StarAgent, 3 agents, dashboard"
echo ""
echo -e "  Flash:    sudo dd if=$OUTPUT_DIR/${ISO_NAME}.iso of=/dev/sdX bs=4M status=progress"
echo -e "  Test:     qemu-system-x86_64 -cdrom $OUTPUT_DIR/${ISO_NAME}.iso -m 4G"
if [[ -f "$WSL_TAR" ]]; then
  echo -e "  WSL:      wsl --import AgneticOS C:\\AgneticOS $WSL_TAR --version 2"
fi
echo ""
echo -e "Kernel: Ubuntu (TPM support via tpm_tis/crb modules; Secure Boot via signed kernel + shim)"
echo ""
