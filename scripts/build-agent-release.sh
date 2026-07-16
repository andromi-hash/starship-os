#!/usr/bin/env bash
# Starship OS — Build Agent Release Archives (Linux + Windows)
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
log()  { echo -e "${GREEN}[BUILD]${NC} $*"; }
err()  { echo -e "${RED}[BUILD]${NC} $*" >&2; exit 1; }

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
OUTPUT_DIR="$REPO_DIR/dist"
export PATH="$HOME/.cargo/bin:$PATH"

mkdir -p "$OUTPUT_DIR"

# ─── Linux x86_64 ──────────────────────────────────────────────────
log "Building staragent for Linux x86_64..."
cd "$REPO_DIR/agent"
cargo build --release --target x86_64-unknown-linux-gnu

STAGING=$(mktemp -d)
cp "$REPO_DIR/agent/target/x86_64-unknown-linux-gnu/release/staragent" "$STAGING/staragent"
cp "$REPO_DIR/scripts/install-agent-linux.sh" "$STAGING/"
tar czf "$OUTPUT_DIR/staragent-linux-x86_64.tar.gz" -C "$STAGING" staragent install-agent-linux.sh
rm -rf "$STAGING"
log "  → $OUTPUT_DIR/staragent-linux-x86_64.tar.gz ($(du -h "$OUTPUT_DIR/staragent-linux-x86_64.tar.gz" | cut -f1))"

# ─── Windows x86_64 ────────────────────────────────────────────────
log "Building staragent for Windows x86_64..."
CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER=x86_64-w64-mingw32-gcc \
    cargo build --release --target x86_64-pc-windows-gnu

STAGING=$(mktemp -d)
cp "$REPO_DIR/agent/target/x86_64-pc-windows-gnu/release/staragent.exe" "$STAGING/"
cp "$REPO_DIR/packaging/windows/install.bat" "$STAGING/"
cp "$REPO_DIR/packaging/windows/uninstall.bat" "$STAGING/"
cp "$REPO_DIR/packaging/windows/configure.bat" "$STAGING/"
cp "$REPO_DIR/packaging/windows/README.txt" "$STAGING/"
(cd "$STAGING" && zip -q "$OUTPUT_DIR/staragent-windows-x86_64.zip" ./*)
rm -rf "$STAGING"
log "  → $OUTPUT_DIR/staragent-windows-x86_64.zip ($(du -h "$OUTPUT_DIR/staragent-windows-x86_64.zip" | cut -f1))"

echo
echo -e "${GREEN}============================================${NC}"
echo -e "${GREEN}  Release archives built${NC}"
echo -e "${GREEN}============================================${NC}"
ls -lh "$OUTPUT_DIR"/staragent-*.tar.gz "$OUTPUT_DIR"/staragent-*.zip 2>/dev/null
