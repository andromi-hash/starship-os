#!/bin/bash
# Starship OS — System Tray Launcher
set -e
PID_FILE="/tmp/starship-tray.pid"

if [ -f "$PID_FILE" ]; then
  OLD_PID=$(cat "$PID_FILE")
  if kill -0 "$OLD_PID" 2>/dev/null; then
    echo "Tray already running (PID $OLD_PID)"
    exit 0
  fi
fi

cd /home/tech/starship-os
nohup python3 starship_tray.py > /tmp/starship-tray.log 2>&1 &
echo $! > "$PID_FILE"
echo "Starship OS tray started (PID $(cat $PID_FILE))"
