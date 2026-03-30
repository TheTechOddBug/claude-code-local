#!/bin/bash
# Orchestrator API — Browser + macOS + Recording + Local AI
# Double-click to launch

MLX_PYTHON="/Users/dtribe/.local/mlx-server/bin/python3"
MLX_SERVER="/Users/dtribe/.local/mlx-native-server/server.py"
API_SERVER="/Users/dtribe/.local/orchestrator-api/api.py"

# Start MLX server if not running
if ! lsof -i :4000 >/dev/null 2>&1; then
  echo "  Loading Qwen 3.5 122B..."
  "$MLX_PYTHON" "$MLX_SERVER" >/tmp/mlx-server.log 2>&1 &
  while ! curl -s http://localhost:4000/health 2>/dev/null | grep -q "ok"; do sleep 2; done
  echo "  MLX server ready"
fi

# Start Brave with remote debugging if not running
if ! lsof -i :9222 >/dev/null 2>&1; then
  if pgrep -f "Brave Browser" >/dev/null 2>&1; then
    echo "  Restarting Brave with remote debugging..."
    osascript -e 'quit app "Brave Browser"'
    sleep 2
  fi
  open -a "Brave Browser" --args --remote-debugging-port=9222
  echo -n "  Waiting for Brave"
  for i in $(seq 1 15); do
    if lsof -i :9222 >/dev/null 2>&1; then echo " ready!"; break; fi
    echo -n "."; sleep 1
  done
fi

# Kill any existing API on 4001
lsof -ti :4001 | xargs kill 2>/dev/null

clear
echo ""
echo "  ╔═══════════════════════════════════════════════╗"
echo "  ║  Orchestrator API                             ║"
echo "  ║  Browser + macOS + Recording + Local AI       ║"
echo "  ║  http://localhost:4001/docs                   ║"
echo "  ╚═══════════════════════════════════════════════╝"
echo ""
echo "  Services:"
echo "    ⚡ MLX Server    → localhost:4000"
echo "    🌐 Brave CDP     → localhost:9222"
echo "    🎯 API Server    → localhost:4001"
echo ""
echo "  Quick test:"
echo "    curl -X POST localhost:4001/tasks -H 'Content-Type: application/json' -d '{\"prompt\":\"your task here\"}'"
echo ""

exec "$MLX_PYTHON" "$API_SERVER"
