#!/bin/bash
# Install Starship OS systemd services
set -e

SERVICES_DIR="/home/tech/agnetic-os/systemd"
SYSTEMD_DIR="/etc/systemd/system"

echo "=== Installing Starship OS Systemd Services ==="
echo ""

for svc in agnetic-nats agnetic-staragent agnetic-agents agnetic-dashboard; do
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
sudo systemctl enable agnetic-nats agnetic-staragent agnetic-agents agnetic-dashboard 2>/dev/null || true

echo ""
echo "=== Current status ==="
for svc in agnetic-nats agnetic-staragent agnetic-agents agnetic-dashboard; do
    if systemctl is-active --quiet "$svc" 2>/dev/null; then
        echo "  ✓ $svc is active"
    else
        echo "  ○ $svc is inactive"
    fi
done

echo ""
echo "=== Next steps ==="
echo "  To start all services: sudo systemctl start agnetic-nats agnetic-staragent agnetic-agents agnetic-dashboard"
echo "  To view logs: journalctl -u agnetic-nats -f"
