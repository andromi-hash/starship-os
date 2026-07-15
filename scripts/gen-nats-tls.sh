#!/usr/bin/env bash
# Starship OS — generate self-signed TLS material for NATS fleet (optional)
# Usage: bash scripts/gen-nats-tls.sh [--out DIR] [--host CN]
# Writes: ca.pem, server-cert.pem, server-key.pem, client-cert.pem, client-key.pem
set -euo pipefail

OUT="${STARSHIP_NATS_TLS:-}"
HOST="${STARSHIP_NATS_TLS_HOST:-starship-nats.local}"
DAYS=825

while [[ $# -gt 0 ]]; do
  case "$1" in
    --out) OUT="$2"; shift 2 ;;
    --host) HOST="$2"; shift 2 ;;
    --days) DAYS="$2"; shift 2 ;;
    -h|--help)
      echo "Usage: $0 [--out DIR] [--host CN] [--days N]"
      exit 0
      ;;
    *) echo "unknown: $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$OUT" ]]; then
  if [[ "$(id -u)" == "0" ]]; then
    OUT=/etc/starship/nats/tls
  else
    OUT="$(cd "$(dirname "$0")/.." && pwd)/nats/tls"
  fi
fi

mkdir -p "$OUT"
chmod 700 "$OUT"

if [[ -f "$OUT/server-cert.pem" && -f "$OUT/server-key.pem" && "${STARSHIP_NATS_TLS_FORCE:-}" != "1" ]]; then
  echo "TLS already present in $OUT (set STARSHIP_NATS_TLS_FORCE=1 to regenerate)"
  exit 0
fi

command -v openssl >/dev/null || { echo "openssl required" >&2; exit 1; }

# CA
openssl req -x509 -newkey rsa:4096 -sha256 -days "$DAYS" -nodes \
  -keyout "$OUT/ca-key.pem" -out "$OUT/ca.pem" \
  -subj "/O=Starship OS/CN=Starship Fleet CA" 2>/dev/null

# Server
openssl req -newkey rsa:4096 -nodes -keyout "$OUT/server-key.pem" \
  -out "$OUT/server.csr" \
  -subj "/O=Starship OS/CN=${HOST}" 2>/dev/null
openssl x509 -req -in "$OUT/server.csr" -CA "$OUT/ca.pem" -CAkey "$OUT/ca-key.pem" \
  -CAcreateserial -out "$OUT/server-cert.pem" -days "$DAYS" -sha256 \
  -extfile <(printf "subjectAltName=DNS:%s,DNS:localhost,IP:127.0.0.1" "$HOST") 2>/dev/null

# Client (mutual TLS optional)
openssl req -newkey rsa:4096 -nodes -keyout "$OUT/client-key.pem" \
  -out "$OUT/client.csr" \
  -subj "/O=Starship OS/CN=starship-client" 2>/dev/null
openssl x509 -req -in "$OUT/client.csr" -CA "$OUT/ca.pem" -CAkey "$OUT/ca-key.pem" \
  -CAcreateserial -out "$OUT/client-cert.pem" -days "$DAYS" -sha256 2>/dev/null

rm -f "$OUT/server.csr" "$OUT/client.csr" "$OUT/ca.srl"
chmod 600 "$OUT"/*-key.pem
chmod 644 "$OUT/ca.pem" "$OUT/server-cert.pem" "$OUT/client-cert.pem"
chown -R nats:nats "$OUT" 2>/dev/null || true

# Snippet to append to fleet-accounts / fleet-bus
cat > "$OUT/tls.conf.snippet" <<EOF
# Include from NATS conf: include ./tls/tls.conf.snippet
# Or merge manually under top-level.
tls {
  cert_file: "${OUT}/server-cert.pem"
  key_file:  "${OUT}/server-key.pem"
  ca_file:   "${OUT}/ca.pem"
  verify: false
  timeout: 5
}
EOF

cat > "$OUT/client.env" <<EOF
# Source for TLS clients (nats-py: tls=... or NATS_URL=tls://)
STARSHIP_NATS_TLS=1
STARSHIP_NATS_CA=${OUT}/ca.pem
STARSHIP_NATS_CERT=${OUT}/client-cert.pem
STARSHIP_NATS_KEY=${OUT}/client-key.pem
NATS_URL=tls://${HOST}:4222
EOF
chmod 600 "$OUT/client.env"

echo "TLS material: $OUT"
echo "  ca.pem server-cert.pem server-key.pem client-*.pem"
echo "  snippet: $OUT/tls.conf.snippet"
echo "  client:  $OUT/client.env"
