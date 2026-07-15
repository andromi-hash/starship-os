#!/bin/bash
# Starship OS Dashboard Launcher
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG_DIR="$PROJECT_DIR/logs"
mkdir -p "$LOG_DIR"

echo "=== Starship OS Dashboard ==="

# 1. Status bridge (NATS -> JSON file)
echo "[1/2] Starting status bridge..."
pkill -f "agnetic-status.py" 2>/dev/null || true
"$PROJECT_DIR/.venv/bin/python3" "$PROJECT_DIR/tray/agnetic-status.py" \
  > "$LOG_DIR/status-bridge.log" 2>&1 &
STATUS_PID=$!
echo "  PID $STATUS_PID"

# 2. Web dashboard server
echo "[2/2] Starting web dashboard on http://localhost:8788"
pkill -f "dashboard/server.py" 2>/dev/null || true
DASHBOARD_PORT=8788 nohup "$PROJECT_DIR/.venv/bin/python3" "$PROJECT_DIR/dashboard/server.py" \
  > "$LOG_DIR/dashboard-server.log" 2>&1 &
DASH_PID=$!
echo "  PID $DASH_PID"

echo ""
echo "=== Dashboard running ==="
echo "  Status bridge:  PID $STATUS_PID"
echo "  Web:            http://localhost:8788"
