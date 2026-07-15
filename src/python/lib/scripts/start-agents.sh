#!/bin/bash
# Starship OS - Start all agents
# Run this on boot to start the agent mesh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "=== Starship OS Agent Mesh ==="
echo ""

# 1. Ensure NATS is running
if ! pgrep -x nats-server > /dev/null 2>&1; then
    echo "[1/3] Starting NATS server..."
    nats-server -c "$PROJECT_DIR/nats/agent-bus.conf" > /dev/null 2>&1 &
    sleep 1
    echo "  NATS started on port 4222"
else
    echo "[1/3] NATS server already running"
fi

# 2. Ensure StarAgent is running
if ! pgrep -x staragent > /dev/null 2>&1; then
    echo "[2/3] Starting StarAgent..."
    nohup "$PROJECT_DIR/agent/target/release/staragent" > "$PROJECT_DIR/logs/staragent.log" 2>&1 &
    echo "  StarAgent started"
else
    echo "[2/3] StarAgent already running"
fi

# 3. Start all Hermes agents
echo "[3/3] Starting Hermes agents..."
"$PROJECT_DIR/agents/run_agent.sh" start

# 4. Start status bridge + dashboard server
if ! pgrep -f "$PROJECT_DIR/dashboard/server.py" > /dev/null 2>&1; then
    echo "[4/4] Starting dashboard server..."
    nohup "$PROJECT_DIR/.venv/bin/python3" \
        "$PROJECT_DIR/dashboard/server.py" > "$PROJECT_DIR/logs/dashboard.log" 2>&1 &
    echo "  Dashboard at http://localhost:8788"
fi

echo ""
echo "=== All agents running ==="
echo "Use '$PROJECT_DIR/agents/run_agent.sh status' to check status"
echo "Use '$PROJECT_DIR/agents/run_agent.sh stop' to stop all agents"
