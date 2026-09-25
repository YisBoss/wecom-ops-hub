#!/bin/sh
# 以 root 启动时：确保数据目录存在且归属正确，然后降权到 app 用户运行。
# 这样用户不必手动 chown 宿主机上的 ./data 目录（compose 首次会把它建成 root:root）。
set -e

DATA_DIR="${HUB_DATA_DIR:-/data}"

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA_DIR"
    chown -R app:app "$DATA_DIR" 2>/dev/null || true
    if command -v runuser >/dev/null 2>&1; then
        exec runuser -u app -- "$@"
    fi
    # 极端情况下没有 runuser，就降级用 su
    exec su -s /bin/sh app -c "$(printf '%s ' "$@")"
fi

exec "$@"
