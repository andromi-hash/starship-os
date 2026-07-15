#!/bin/bash
# Install OpenCode for Starship OS
set -e

INSTALL_DIR="${HOME}/.opencode/bin"
mkdir -p "$INSTALL_DIR"

echo "=== Installing OpenCode ==="

# Check if already installed
if command -v opencode &>/dev/null; then
    echo "OpenCode already installed: $(opencode --version)"
    exit 0
fi

# Install via official script
echo "Installing via official installer..."
curl -fsSL https://opencode.ai/install | bash

# Verify
if [ -f "$INSTALL_DIR/opencode" ]; then
    echo "OpenCode installed to $INSTALL_DIR/opencode"
    echo "Add to PATH: export PATH=\"$INSTALL_DIR:\$PATH\""
    "$INSTALL_DIR/opencode" --version
else
    echo "ERROR: Installation failed"
    exit 1
fi

echo "Note: For codex:* modes (review, adversarial-review, rescue, transfer), install Codex CLI separately (local native execution for identical output)."
echo "Autonomous delegation from opencode to Codex on stall is enabled."
