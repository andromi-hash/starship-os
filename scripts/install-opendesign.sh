#!/bin/bash
# Install Open Design for Starship OS
set -e

INSTALL_DIR="/opt/open-design"

echo "=== Installing Open Design ==="

# Check if already installed
if [ -d "$INSTALL_DIR" ]; then
    echo "Open Design already installed at $INSTALL_DIR"
    echo "Update with: cd $INSTALL_DIR && git pull && pnpm install"
    exit 0
fi

# Check prerequisites
if ! command -v node &>/dev/null; then
    echo "ERROR: Node.js required. Install: curl -fsSL https://deb.nodesource.com/setup_22.x | sudo bash - && sudo apt install -y nodejs"
    exit 1
fi

if ! command -v pnpm &>/dev/null; then
    echo "Installing pnpm..."
    npm install -g pnpm
fi

# Clone
echo "Cloning Open Design..."
git clone https://github.com/nexu-io/open-design.git "$INSTALL_DIR"

# Install dependencies
echo "Installing dependencies..."
cd "$INSTALL_DIR" && pnpm install

echo ""
echo "=== Open Design installed ==="
echo "Start daemon: cd $INSTALL_DIR && pnpm tools-dev"
echo "Or use via agent tool: opendesign skill=web-prototype prompt='...'"
