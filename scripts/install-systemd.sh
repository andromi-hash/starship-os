#!/bin/bash
# Install Starship OS systemd services
set -e

SERVICES_DIR="/home/tech/starship-os/systemd"
SYSTEMD_DIR="/etc/systemd/system"

echo "=== Installing Starship OS Systemd Services ==="
echo ""

for svc in starship-nats starship-staragent starship-agents starship-dashboard; do
    src="$SERVICES_DIR/$svc.service"
    if [ -f "$src" ]; then
        echo "  Installing $svc.service..."
        sudo cp "$src" "$SYSTEMD_DIR/$svc.service"
        sudo systemctl daemon-reload 2>/dev/null
    else
        echo "  WARNING: $src not found, skipping"
    fi
done

echo ""
echo "=== Enabling services ==="
sudo systemctl enable starship-nats starship-staragent starship-agents starship-dashboard 2>/dev/null || true

echo ""
echo "=== Current status ==="
for svc in starship-nats starship-staragent starship-agents starship-dashboard; do
    if systemctl is-active --quiet "$svc" 2>/dev/null; then
        echo "  ✓ $svc is active"
    else
        echo "  ○ $svc is inactive"
    fi
done

echo ""
echo "=== Next steps ==="
echo "  To start all services: sudo systemctl start starship-nats starship-staragent starship-agents starship-dashboard"
echo "  To view logs: journalctl -u starship-nats -f"
