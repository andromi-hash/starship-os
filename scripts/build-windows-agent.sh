#!/usr/bin/env bash
# Starship OS — Build Windows Agent Installer ZIP
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
log()  { echo -e "${GREEN}[BUILD]${NC} $*"; }
err()  { echo -e "${RED}[BUILD]${NC} $*" >&2; exit 1; }

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
OUTPUT_DIR="$REPO_DIR/dist"
PKG_NAME="starship-staragent-windows-x86_64"

log "Building Windows agent installer..."

# Ensure cross-compiler is available
export PATH="$HOME/.cargo/bin:$PATH"
if ! rustup target list --installed | grep -q x86_64-pc-windows-gnu; then
    log "Adding Windows target..."
    rustup target add x86_64-pc-windows-gnu
fi

# Build
log "Cross-compiling staragent for Windows..."
cd "$REPO_DIR/agent"
CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER=x86_64-w64-mingw32-gcc \
    cargo build --release --target x86_64-pc-windows-gnu

# Assemble package
log "Assembling installer package..."
STAGING=$(mktemp -d)
mkdir -p "$STAGING"

cp "$REPO_DIR/agent/target/x86_64-pc-windows-gnu/release/staragent.exe" "$STAGING/"
cp "$REPO_DIR/packaging/windows/install.bat" "$STAGING/"
cp "$REPO_DIR/packaging/windows/uninstall.bat" "$STAGING/"
cp "$REPO_DIR/packaging/windows/configure.bat" "$STAGING/"
cp "$REPO_DIR/packaging/windows/README.txt" "$STAGING/"

# Create ZIP
mkdir -p "$OUTPUT_DIR"
ZIP_PATH="$OUTPUT_DIR/${PKG_NAME}.zip"
rm -f "$ZIP_PATH"
(cd "$STAGING" && zip -q "$ZIP_PATH" ./*)

rm -rf "$STAGING"

log "Windows agent installer built:"
log "  $ZIP_PATH"
log "  Size: $(du -h "$ZIP_PATH" | cut -f1)"
