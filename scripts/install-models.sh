#!/usr/bin/env bash
# Starship OS — pull Ollama models for selected hardware profile
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PROFILES="$REPO_DIR/config/profiles.yaml"
MODELS_YAML="$REPO_DIR/config/models.yaml"
PROFILE="${1:-}"

if [[ -z "$PROFILE" ]]; then
  if [[ -f /etc/starship/profile.yaml ]]; then
    PROFILE=$(awk '/^profile:/{print $2; exit}' /etc/starship/profile.yaml)
  elif [[ -f "${XDG_CONFIG_HOME:-$HOME/.config}/starship/profile.yaml" ]]; then
    PROFILE=$(awk '/^profile:/{print $2; exit}' "${XDG_CONFIG_HOME:-$HOME/.config}/starship/profile.yaml")
  else
    bash "$REPO_DIR/scripts/select-profile.sh" >/dev/null
    PROFILE=$(awk '/^profile:/{print $2; exit}' "${XDG_CONFIG_HOME:-$HOME/.config}/starship/profile.yaml" 2>/dev/null || echo server)
    [[ -f /etc/starship/profile.yaml ]] && PROFILE=$(awk '/^profile:/{print $2; exit}' /etc/starship/profile.yaml)
  fi
fi

PROFILE="${PROFILE:-server}"
INCLUDE_OPTIONAL="${STARSHIP_PULL_OPTIONAL:-0}"

if ! command -v ollama &>/dev/null; then
  echo "ERROR: ollama not installed" >&2
  exit 1
fi

if ! command -v python3 &>/dev/null; then
  echo "ERROR: python3 required" >&2
  exit 1
fi

mapfile -t PULL_LIST < <(python3 - "$PROFILES" "$MODELS_YAML" "$PROFILE" "$INCLUDE_OPTIONAL" <<'PY'
import sys, yaml
from pathlib import Path
profiles_path, models_path, name, opt = sys.argv[1:5]
profiles = yaml.safe_load(Path(profiles_path).read_text())
models = yaml.safe_load(Path(models_path).read_text())["models"]
p = profiles["profiles"][name]
want = list(p.get("models", {}).get("required", []))
if opt in ("1", "true", "yes"):
    want += list(p.get("models", {}).get("optional", []))
# resolve aliases to upstream pull names
for m in want:
    meta = models.get(m, {})
    upstream = meta.get("upstream", m)
    # strip :latest for ollama pull consistency
    print(upstream)
PY
)

echo "=== Pulling models for profile: $PROFILE ==="
for m in "${PULL_LIST[@]}"; do
  echo "→ ollama pull $m"
  ollama pull "$m" || echo "WARN: failed to pull $m" >&2
done

# Create Eve alias if Modelfile present
MODEFILE="$REPO_DIR/config/models/Eve-V2-Unleashed.Modelfile"
if [[ -f "$MODEFILE" ]]; then
  if ! ollama list 2>/dev/null | grep -qi 'Eve-V2-Unleashed'; then
    echo "→ ollama create Eve-V2-Unleashed"
    ollama create Eve-V2-Unleashed -f "$MODEFILE" || true
  fi
fi

echo "Done. Models:"
ollama list 2>/dev/null || true
