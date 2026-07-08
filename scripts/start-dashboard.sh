#!/bin/bash
# Starship OS Dashboard Launcher
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
HERMES_VENV="$HOME/.hermes/hermes-agent/venv"
LOG_DIR="$PROJECT_DIR/logs"
mkdir -p "$LOG_DIR"

echo "=== Starship OS Dashboard ==="

# 1. Status bridge (NATS -> JSON file)
echo "[1/4] Starting status bridge..."
pkill -f "starship-status.py" 2>/dev/null || true
"$HERMES_VENV/bin/python3" "$PROJECT_DIR/tray/starship-status.py" \
  > "$LOG_DIR/status-bridge.log" 2>&1 &
STATUS_PID=$!
echo "  PID $STATUS_PID"

# 2. Conky dashboard
echo "[2/4] Starting Conky dashboard..."
pkill -x conky 2>/dev/null || true
sleep 1
conky -c "$PROJECT_DIR/conky/starship.conkyrc" -d > /dev/null 2>&1 || true
echo "  Started"

# 3. System tray indicator
echo "[3/4] Starting system tray indicator..."
nohup python3 "$PROJECT_DIR/tray/starship-indicator.py" \
  > "$LOG_DIR/tray-indicator.log" 2>&1 &
TRAY_PID=$!
echo "  PID $TRAY_PID"

# 4. Web dashboard server
echo "[4/4] Starting web dashboard on http://localhost:8899"
pkill -f "dashboard/server.py" 2>/dev/null || true
nohup "$HERMES_VENV/bin/python3" "$PROJECT_DIR/dashboard/server.py" \
  > "$LOG_DIR/dashboard-server.log" 2>&1 &
DASH_PID=$!
echo "  PID $DASH_PID"

echo ""
echo "=== Dashboard running ==="
echo "  Status bridge:  PID $STATUS_PID"
echo "  Conky:          running"
echo "  Tray indicator: PID $TRAY_PID"
echo "  Web:            http://localhost:8899"
