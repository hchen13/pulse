#!/bin/bash
# Pulse 启动脚本
# 用法：
#   ./start.sh daemon   — 启动定时采集 daemon（阻塞）
#   ./start.sh web      — 启动 Web Dashboard
#   ./start.sh fetch    — 立即采集一次
#   ./start.sh run      — 立即执行完整流程（采集+分析+推送）

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# 激活 venv
VENV="$SCRIPT_DIR/venv"
if [ ! -d "$VENV" ]; then
    echo "创建虚拟环境..."
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install -e . -q
fi

PULSE="$VENV/bin/pulse"

case "$1" in
    daemon)
        echo "启动 Pulse Daemon..."
        exec "$PULSE" daemon
        ;;
    web)
        PORT="${2:-8765}"
        echo "启动 Pulse Web Dashboard: http://localhost:$PORT"
        exec "$PULSE" serve --port "$PORT"
        ;;
    fetch)
        exec "$PULSE" fetch "${@:2}"
        ;;
    run)
        exec "$PULSE" run
        ;;
    *)
        echo "用法: $0 {daemon|web|fetch|run}"
        echo ""
        echo "  daemon   — 启动定时 daemon（按 config.yaml 中的 cron 调度）"
        echo "  web      — 启动 Web Dashboard（默认端口 8765）"
        echo "  fetch    — 立即采集一次"
        echo "  run      — 立即执行完整流程（采集+分析+推送）"
        exit 1
        ;;
esac
