#!/usr/bin/env bash
# Starship OS — Daemon Uninstaller
# Stops services, removes files, removes users.
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}[UNINSTALL]${NC} $*"; }
warn() { echo -e "${YELLOW}[UNINSTALL]${NC} $*"; }

if [[ "$(id -u)" != "0" ]]; then
    echo -e "${RED}Must run as root.${NC}"
    exit 1
fi

echo "This will stop all Starship OS services and remove installed files."
read -p "Continue? [y/N] " -n 1 -r
echo ""
[[ ! $REPLY =~ ^[Yy]$ ]] && exit 0

# Stop and disable services
log "Stopping services..."
for unit in agnetic-dashboard agnetic-message-history agnetic-status-bridge \
            agnetic-agent@proxy agnetic-agent@romi agnetic-agent@ergo \
            agnetic-staragent agnetic-nats; do
    systemctl stop "$unit.service" 2>/dev/null || true
    systemctl disable "$unit.service" 2>/dev/null || true
done

# Remove systemd units
log "Removing systemd units..."
rm -f /etc/systemd/system/agnetic-*.service
rm -f /etc/systemd/system/agnetic-*.target
rm -f /etc/systemd/system/ollama.service.d/override.conf
rmdir /etc/systemd/system/ollama.service.d 2>/dev/null || true
systemctl daemon-reload

# Remove installed files
log "Removing files..."
rm -rf /opt/starship /opt/agnetic
rm -rf /etc/starship /etc/agnetic
rm -rf /var/lib/starship /var/lib/agnetic
rm -rf /var/log/starship /var/log/agnetic
rm -f /usr/local/bin/starshipctl /usr/local/bin/agneticctl

# Remove users (optional, keeps home dirs)
log "Removing system users..."
userdel agnetic 2>/dev/null || true
userdel nats 2>/dev/null || true

log "Uninstall complete."
log "Note: Ollama, NATS binary, Go, Rust were not removed (installed separately)."
