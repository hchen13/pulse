#!/bin/bash
# Pulse launchd services 安装脚本
# 用法: bash scripts/install.sh [--uninstall]

set -e

PULSE_DIR="/Users/claire/.openclaw/agents/xinge/projects/pulse"
PLIST_DAEMON="com.pulse.daemon"
PLIST_WEB="com.pulse.web"
LAUNCHD_DIR="$HOME/Library/LaunchAgents"

uninstall() {
    echo "=== Uninstalling Pulse services ==="
    for svc in "$PLIST_DAEMON" "$PLIST_WEB"; do
        if launchctl list "$svc" &>/dev/null; then
            echo "Stopping $svc..."
            launchctl unload "$LAUNCHD_DIR/$svc.plist" 2>/dev/null || true
        fi
        if [ -f "$LAUNCHD_DIR/$svc.plist" ]; then
            rm -f "$LAUNCHD_DIR/$svc.plist"
            echo "Removed $LAUNCHD_DIR/$svc.plist"
        fi
    done
    echo "Done."
}

install() {
    echo "=== Installing Pulse services ==="

    # 确保 logs 目录存在
    mkdir -p "$PULSE_DIR/logs"
    mkdir -p "$PULSE_DIR/data"

    # 确保 LaunchAgents 目录存在
    mkdir -p "$LAUNCHD_DIR"

    for svc in "$PLIST_DAEMON" "$PLIST_WEB"; do
        src="$PULSE_DIR/$svc.plist"
        dst="$LAUNCHD_DIR/$svc.plist"

        if [ ! -f "$src" ]; then
            echo "ERROR: plist not found: $src"
            exit 1
        fi

        # 如果已加载，先卸载
        if launchctl list "$svc" &>/dev/null; then
            echo "Unloading existing $svc..."
            launchctl unload "$dst" 2>/dev/null || true
        fi

        # 复制 plist
        cp "$src" "$dst"
        echo "Installed: $dst"

        # 加载服务
        launchctl load "$dst"
        echo "Loaded: $svc"
    done

    echo ""
    echo "=== Status ==="
    sleep 2
    for svc in "$PLIST_DAEMON" "$PLIST_WEB"; do
        if launchctl list "$svc" &>/dev/null; then
            pid=$(launchctl list "$svc" | grep -o '"PID" = [0-9]*' | awk '{print $3}')
            echo "✓ $svc running (PID: ${pid:-unknown})"
        else
            echo "✗ $svc NOT running"
        fi
    done

    echo ""
    echo "Web dashboard: http://localhost:8765"
    echo "Daemon logs:   $PULSE_DIR/logs/daemon.stdout.log"
    echo "Web logs:      $PULSE_DIR/logs/web.stdout.log"
}

if [ "$1" = "--uninstall" ]; then
    uninstall
else
    install
fi
