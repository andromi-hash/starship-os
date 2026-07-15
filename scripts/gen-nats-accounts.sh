#!/usr/bin/env bash
# Starship OS — generate multi-tenant NATS accounts + optional nkeys
# Usage:
#   bash scripts/gen-nats-accounts.sh [--out DIR] [--no-nkeys]
# Writes:
#   $OUT/fleet-accounts.conf   — server config (secrets embedded)
#   $OUT/creds/*.env           — per-role client env (user/pass)
#   $OUT/creds/*.nk            — nkey seeds (if nk available)
#   $OUT/creds/manifest.json   — public metadata (no passwords)
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${STARSHIP_NATS_CREDS:-$REPO_DIR/nats/creds}"
USE_NKEYS=1
HOST="${STARSHIP_NATS_HOST:-127.0.0.1}"
PORT="${STARSHIP_NATS_PORT:-4222}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --out) OUT="$2"; shift 2 ;;
    --no-nkeys) USE_NKEYS=0; shift ;;
    --host) HOST="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    -h|--help)
      echo "Usage: $0 [--out DIR] [--no-nkeys] [--host HOST] [--port PORT]"
      exit 0
      ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

TMPL="$REPO_DIR/nats/fleet-accounts.conf.tmpl"
[[ -f "$TMPL" ]] || { echo "missing template: $TMPL" >&2; exit 1; }

mkdir -p "$OUT/creds"
chmod 700 "$OUT" "$OUT/creds" 2>/dev/null || true

rand_pass() {
  if command -v openssl &>/dev/null; then
    openssl rand -hex 16
  else
    head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n'
  fi
}

find_nk() {
  if command -v nk &>/dev/null; then
    command -v nk
    return
  fi
  for p in "$HOME/go/bin/nk" /root/go/bin/nk /usr/local/bin/nk; do
    [[ -x "$p" ]] && { echo "$p"; return; }
  done
  return 1
}

gen_nkey_pair() {
  # prints: SEED\nPUBLIC
  local nk_bin
  nk_bin="$(find_nk)" || return 1
  "$nk_bin" -gen user -pubout 2>/dev/null
}

# Generate passwords
SYS_PASS=$(rand_pass)
OPS_PASS=$(rand_pass)
EDGE_PASS=$(rand_pass)
RED_PASS=$(rand_pass)
BLUE_PASS=$(rand_pass)
TELEM_PASS=$(rand_pass)

# Optional nkeys
declare -A NK_PUB NK_SEED
for role in ops edge red blue telem; do
  NK_PUB[$role]=""
  NK_SEED[$role]=""
  if [[ "$USE_NKEYS" == "1" ]]; then
    if pair=$(gen_nkey_pair); then
      seed=$(echo "$pair" | sed -n '1p')
      pub=$(echo "$pair" | sed -n '2p')
      if [[ "$seed" == S* && "$pub" == U* ]]; then
        NK_SEED[$role]="$seed"
        NK_PUB[$role]="$pub"
        printf '%s\n' "$seed" > "$OUT/creds/${role}.nk"
        printf '%s\n' "$pub" > "$OUT/creds/${role}.nk.pub"
        chmod 600 "$OUT/creds/${role}.nk"
        chmod 644 "$OUT/creds/${role}.nk.pub"
      fi
    fi
  fi
done

nkey_line() {
  local role="$1"
  local pub="${NK_PUB[$role]:-}"
  if [[ -n "$pub" ]]; then
    # Sibling user entry (comma-separated inside users array)
    printf ', {nkey: "%s"}' "$pub"
  else
    printf ''
  fi
}

# Materialize server conf
CONF="$OUT/fleet-accounts.conf"
HTTP_PORT=$((PORT + 4000))
[[ "$PORT" == "4222" ]] && HTTP_PORT=8222
sed \
  -e "s|__SYS_PASS__|${SYS_PASS}|g" \
  -e "s|__OPS_PASS__|${OPS_PASS}|g" \
  -e "s|__EDGE_PASS__|${EDGE_PASS}|g" \
  -e "s|__RED_PASS__|${RED_PASS}|g" \
  -e "s|__BLUE_PASS__|${BLUE_PASS}|g" \
  -e "s|__TELEM_PASS__|${TELEM_PASS}|g" \
  -e "s|__OPS_NKEY_LINE__|$(nkey_line ops)|g" \
  -e "s|__EDGE_NKEY_LINE__|$(nkey_line edge)|g" \
  -e "s|__RED_NKEY_LINE__|$(nkey_line red)|g" \
  -e "s|__BLUE_NKEY_LINE__|$(nkey_line blue)|g" \
  -e "s|__TELEM_NKEY_LINE__|$(nkey_line telem)|g" \
  -e "s|^port: 4222|port: ${PORT}|" \
  -e "s|^http_port: 8222|http_port: ${HTTP_PORT}|" \
  "$TMPL" > "$CONF"
chmod 600 "$CONF"

write_role_env() {
  local role="$1" user="$2" pass="$3" account="$4"
  local f="$OUT/creds/${role}.env"
  cat > "$f" <<EOF
# Starship OS NATS client — role=${role} account=${account}
# Generated $(date -u +%Y-%m-%dT%H:%M:%SZ)
NATS_URL=nats://${user}:${pass}@${HOST}:${PORT}
NATS_USER=${user}
NATS_PASSWORD=${pass}
STARSHIP_NATS_ACCOUNT=${account}
STARSHIP_NATS_MODE=accounts
STARSHIP_NATS_ROLE=${role}
EOF
  if [[ -n "${NK_SEED[$role]:-}" ]]; then
    cat >> "$f" <<EOF
STARSHIP_NATS_NKEY_SEED=${NK_SEED[$role]}
STARSHIP_NATS_NKEY_PUB=${NK_PUB[$role]}
EOF
  fi
  chmod 600 "$f"
}

write_role_env ops   ops   "$OPS_PASS"   STARSHIP_OPS
write_role_env edge  edge  "$EDGE_PASS"  STARSHIP_EDGE
write_role_env red   red   "$RED_PASS"   STARSHIP_RANGE
write_role_env blue  blue  "$BLUE_PASS"  STARSHIP_RANGE
write_role_env telem telem "$TELEM_PASS" STARSHIP_TELEM

# sys env (admin)
cat > "$OUT/creds/sys.env" <<EOF
NATS_URL=nats://sys:${SYS_PASS}@${HOST}:${PORT}
NATS_USER=sys
NATS_PASSWORD=${SYS_PASS}
STARSHIP_NATS_ACCOUNT=SYS
STARSHIP_NATS_MODE=accounts
STARSHIP_NATS_ROLE=sys
EOF
chmod 600 "$OUT/creds/sys.env"

# Default client for ops/fleet daemon
cp "$OUT/creds/ops.env" "$OUT/nats.env"
chmod 600 "$OUT/nats.env"

# Public manifest (no secrets)
python3 - "$OUT" "$USE_NKEYS" <<'PY'
import json, sys, os
from pathlib import Path
out = Path(sys.argv[1])
use_nkeys = sys.argv[2] == "1"
roles = {}
for role in ("ops", "edge", "red", "blue", "telem"):
    pub = out / "creds" / f"{role}.nk.pub"
    roles[role] = {
        "account": {
            "ops": "STARSHIP_OPS",
            "edge": "STARSHIP_EDGE",
            "red": "STARSHIP_RANGE",
            "blue": "STARSHIP_RANGE",
            "telem": "STARSHIP_TELEM",
        }[role],
        "nkey_pub": pub.read_text().strip() if pub.exists() else None,
        "env_file": f"creds/{role}.env",
    }
manifest = {
    "version": "2.1",
    "mode": "accounts",
    "nkeys": use_nkeys and any(r["nkey_pub"] for r in roles.values()),
    "server_conf": "fleet-accounts.conf",
    "roles": roles,
}
(out / "creds" / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
print(json.dumps({"ok": True, "out": str(out), "nkeys": manifest["nkeys"], "roles": list(roles)}, indent=2))
PY

echo "Generated:"
echo "  server: $CONF"
echo "  clients: $OUT/creds/*.env"
echo "  default: $OUT/nats.env  (ops)"
if find_nk &>/dev/null && [[ "$USE_NKEYS" == "1" ]]; then
  echo "  nkeys:   $OUT/creds/*.nk"
else
  echo "  nkeys:   skipped (install nk: go install github.com/nats-io/nkeys/nk@latest)"
fi
