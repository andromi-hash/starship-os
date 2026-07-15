#!/bin/bash
# WSL post-import setup for Starship OS
# Run inside the new WSL distro after: wsl --import AgneticOS ...

set -e

echo "=== Agnetic WSL Post-Setup ==="

# Create user if not
if ! id -u agnetic >/dev/null 2>&1; then
  useradd -m -s /bin/bash agnetic
  echo "agnetic:agnetic" | chpasswd
  usermod -aG sudo agnetic
fi

# Start services (assuming systemd or manual)
# For WSL, may need /etc/wsl.conf with [boot] systemd=true

echo "Setup complete. Run: su - agnetic"
echo "Then: /opt/agnetic/lib/scripts/start-agents.sh &"
echo "Dashboard: python3 /opt/agnetic/lib/dashboard/server.py"
