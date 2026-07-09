#!/bin/bash
# Setup NATS with auth + JetStream
set -e

NATS_URL="nats://127.0.0.1:4222"
CONF="$HOME/starship-os/nats/server.conf"

echo "=== Stopping existing NATS ==="
pkill -x nats-server 2>/dev/null || true
sleep 1

echo "=== Starting NATS with auth + JetStream ==="
nats-server -c "$CONF" &
sleep 1

echo "=== Verifying NATS is running ==="
pgrep -x nats-server && echo "OK" || echo "FAIL"

echo "=== Testing auth ==="
nats pub --server="$NATS_URL" starship.test.hello "test" 2>/dev/null && echo "OK" || echo "FAIL"

echo "=== Creating JetStream streams ==="
nats str add --server="$NATS_URL" AGENTS --subjects "starship.agent.>" --storage file --max-age 72h --max-msgs 1000000 2>/dev/null || true
nats str add --server="$NATS_URL" TELEMETRY --subjects "starship.telemetry.>" --storage file --max-age 24h --max-msgs 500000 2>/dev/null || true

echo "=== Setup complete ==="
echo "Update agent daemon to use: $NATS_URL"
echo "Credentials: starship_user_2026"
