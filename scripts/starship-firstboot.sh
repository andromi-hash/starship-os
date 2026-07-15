#!/usr/bin/env bash
# Starship OS — first boot after autoinstall / package install
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="/etc/starship/firstboot.env"
[[ -f "$ENV_FILE" ]] && # shellcheck disable=SC1090
  source "$ENV_FILE"

PROFILE="${STARSHIP_PROFILE:-server}"
mkdir -p /etc/starship /opt/starship /var/lib/starship /var/log/starship
ln -sfn /opt/starship /opt/agnetic 2>/dev/null || true
ln -sfn /etc/starship /etc/agnetic 2>/dev/null || true

echo "=== Starship firstboot (profile=$PROFILE) ==="

# 1) Profile selection (force profile from autoinstall)
if [[ -x "$REPO_DIR/scripts/select-profile.sh" ]]; then
  bash "$REPO_DIR/scripts/select-profile.sh" "$PROFILE" || true
else
  echo "profile: $PROFILE" > /etc/starship/profile.yaml
fi

# 2) GPU / Ollama env
if [[ -x "$REPO_DIR/scripts/detect-gpu.sh" ]]; then
  bash "$REPO_DIR/scripts/detect-gpu.sh" || true
fi

# 3) Models for profile
if [[ -x "$REPO_DIR/scripts/install-models.sh" ]]; then
  bash "$REPO_DIR/scripts/install-models.sh" "$PROFILE" || true
fi

# 4) Enable mesh if units present
if command -v systemctl &>/dev/null; then
  systemctl daemon-reload 2>/dev/null || true
  systemctl enable --now agnetic-mesh.target 2>/dev/null || true
fi

echo "Firstboot complete. Dashboard: http://localhost:8788"
echo "CLI: starshipctl --help"
