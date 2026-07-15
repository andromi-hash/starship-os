#!/bin/bash
# Starship OS Agent Launcher
# Usage: ./run_agent.sh <agent_name> [--model MODEL]
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AGENT_NAME="${1:-proxy}"
PYTHON="$(command -v python3)"
PID_FILE="$SCRIPT_DIR/.${AGENT_NAME}.pid"
LOG_FILE="$SCRIPT_DIR/../logs/${AGENT_NAME}.log"

mkdir -p "$(dirname "$LOG_FILE")"

shift 2>/dev/null || true

case "${AGENT_NAME}" in
  start)
    # Start all agents
    "$0" proxy "$@"
    "$0" romi "$@"
    "$0" ergo "$@"
    exit 0
    ;;
  stop)
    for a in proxy romi ergo; do
      pf="$SCRIPT_DIR/.${a}.pid"
      if [ -f "$pf" ]; then
        pid=$(cat "$pf")
        echo "Stopping agent '$a' (PID $pid)..."
        kill "$pid" 2>/dev/null && rm -f "$pf" || echo "  (not running)"
      fi
    done
    exit 0
    ;;
  status)
    echo "Agent status:"
    for a in proxy romi ergo; do
      pf="$SCRIPT_DIR/.${a}.pid"
      if [ -f "$pf" ] && kill -0 "$(cat "$pf")" 2>/dev/null; then
        echo "  ✓ $a (PID $(cat "$pf"))"
      else
        echo "  ✗ $a (stopped)"
        rm -f "$pf" 2>/dev/null
      fi
    done
    exit 0
    ;;
esac

echo "Starting agent '$AGENT_NAME'..."

cd "$SCRIPT_DIR"
export AGNETIC_ROOT="$SCRIPT_DIR/.."
export NATS_URL="${NATS_URL:-nats://127.0.0.1:4222}"

nohup "$PYTHON" "$SCRIPT_DIR/agent_daemon.py" "$AGENT_NAME" "$@" \
  > "$LOG_FILE" 2>&1 &
PID=$!
echo $PID > "$PID_FILE"
echo "Agent '$AGENT_NAME' started (PID $PID, log: $LOG_FILE)"
