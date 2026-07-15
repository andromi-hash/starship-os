#!/bin/bash
# Starship OS — Start all services
# Starts NATS, telemetry, scheduler, API server, workflow engine, and all agents.
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AGNETIC_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
LIB_DIR="$AGNETIC_ROOT/lib"
SERVICES_DIR="$AGNETIC_ROOT/services"
LOG_DIR="${AGNETIC_ROOT}/logs"
PYTHON="${PYTHON:-python3}"
NATS_URL="${NATS_URL:-nats://127.0.0.1:4222}"

mkdir -p "$LOG_DIR"

echo "=== Starship OS Startup ==="
echo "Root: $AGNETIC_ROOT"
echo ""

# 1. Ensure NATS is running
if ! nats-server --version 2>/dev/null; then
    echo "[SKIP] nats-server not found (install: apt install nats-server)"
else
    if pgrep -x nats-server >/dev/null 2>&1; then
        echo "[OK] NATS already running"
    else
        echo "[START] Starting NATS..."
        nats-server --jetstream &
        sleep 1
    fi
fi

# 2. Start system telemetry
echo "[START] System telemetry..."
nohup "$PYTHON" "$SERVICES_DIR/system_telemetry.py" > "$LOG_DIR/telemetry.log" 2>&1 &
echo "  PID: $!"

# 3. Start workflow engine
echo "[START] Workflow engine..."
nohup "$PYTHON" "$LIB_DIR/workflow_engine.py" > "$LOG_DIR/workflow-engine.log" 2>&1 &
echo "  PID: $!"

# 4. Start scheduler
echo "[START] Scheduler..."
nohup "$PYTHON" "$LIB_DIR/scheduler.py" > "$LOG_DIR/scheduler.log" 2>&1 &
echo "  PID: $!"

# 5. Start API server (if aiohttp available)
if "$PYTHON" -c "import aiohttp" 2>/dev/null; then
    echo "[START] API server..."
    nohup "$PYTHON" -c "
import asyncio
from services.api_server import ApiServer
from nats import connect

async def main():
    nc = await connect('$NATS_URL')
    api = ApiServer(nats=nc)
    await api.start()
    while True:
        await asyncio.sleep(60)

asyncio.run(main())
" > "$LOG_DIR/api-server.log" 2>&1 &
    echo "  PID: $!"
else
    echo "[SKIP] API server (aiohttp not installed)"
fi

# 6. Start agents
for agent in ergo proxy romi system_health knowledge_store codex-agent designer-agent; do
    echo "[START] Agent: $agent..."
    nohup "$PYTHON" "$LIB_DIR/agent_daemon.py" "$agent" > "$LOG_DIR/${agent}.log" 2>&1 &
    echo "  PID: $!"
    sleep 0.5
done

echo ""
echo "=== All services started ==="
echo "Logs: $LOG_DIR"
echo "API: http://localhost:8080"
echo ""
echo "To stop: pkill -f agent_daemon.py; pkill -f system_telemetry.py; pkill -f workflow_engine.py; pkill -f scheduler.py"
