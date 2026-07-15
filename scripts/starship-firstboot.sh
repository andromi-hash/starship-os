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

# 4) Fleet topology + node registration
if [[ -f "$REPO_DIR/config/fleet.yaml" ]]; then
  cp "$REPO_DIR/config/fleet.yaml" /etc/starship/fleet.yaml
fi
# Map profile → default plant
case "$PROFILE" in
  edge) PLANT=plant-edge; ROLES="proxy,plant-controller" ;;
  ops)  PLANT=plant-alpha; ROLES="proxy,romi,ergo,ops" ;;
  *)    PLANT=plant-alpha; ROLES="proxy,romi,ergo" ;;
esac
ROLES_CSV="${STARSHIP_FLEET_ROLES:-$ROLES}"
ROLES_YAML=$(echo "$ROLES_CSV" | awk -F, '{for(i=1;i<=NF;i++){gsub(/^ +| +$/,"",$i); printf "%s\"%s\"", (i>1?", ":""), $i}}')
cat > /etc/starship/fleet-node.yaml <<EOF
node:
  plant: ${STARSHIP_FLEET_PLANT:-$PLANT}
  roles: [${ROLES_YAML}]
  team: ${STARSHIP_FLEET_TEAM:-ops}
  profile: $PROFILE
EOF
if [[ -f "$REPO_DIR/services/fleet.py" ]]; then
  python3 "$REPO_DIR/services/fleet.py" register || true
elif [[ -f /opt/starship/lib/starship/services/fleet.py ]]; then
  python3 /opt/starship/lib/starship/services/fleet.py register || true
fi

# 5) Enable mesh + fleet if units present
if command -v systemctl &>/dev/null; then
  systemctl daemon-reload 2>/dev/null || true
  systemctl enable --now agnetic-mesh.target 2>/dev/null || true
  systemctl enable --now starship-fleet.service 2>/dev/null || true
fi

echo "Firstboot complete. Dashboard: http://localhost:8788"
echo "CLI: starshipctl fleet status"
