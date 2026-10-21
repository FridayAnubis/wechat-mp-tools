#!/bin/sh
# 容器入口：按 APP_ENTRY 决定启动哪个入口
#   公众号版（默认）：app_mp.py
#   全功能版：app.py
set -e

PORT="${PORT:-5200}"
APP_ENTRY="${APP_ENTRY:-app_mp.py}"

mkdir -p /app/data 2>/dev/null || true

if [ ! -w /app/data ]; then
    echo "[entrypoint] 警告：/app/data 不可写，配置与下载记录无法保存。" >&2
    echo "[entrypoint] 宿主机执行：sudo chown -R $(id -u):$(id -g) ./data" >&2
fi

# PDF 导出走 Chrome 命令行，backend/downloader.py 会优先读 CHROME_PATH
# 注意目录名随架构变化：amd64 是 chrome-linux，arm64 是 chrome-linux-arm64
CHROME_BIN="$(ls -d /app/ms-playwright/chromium-*/chrome-linux*/chrome 2>/dev/null | head -n 1 || true)"
if [ -n "$CHROME_BIN" ]; then
    export CHROME_PATH="$CHROME_BIN"
    echo "[entrypoint] Chrome: $CHROME_PATH"
else
    echo "[entrypoint] 精简模式：未内置 Chromium / ffmpeg"
    echo "[entrypoint]   PDF 导出不可用，风控兜底走不通；文章 HTML/Markdown 与 RSS 正常"
    echo "[entrypoint]   需要 PDF 请用 --build-arg INSTALL_BROWSERS=1 重建"
fi

if [ ! -f "/app/$APP_ENTRY" ]; then
    echo "[entrypoint] 找不到入口文件 /app/$APP_ENTRY，请检查 Dockerfile 与 APP_ENTRY 是否匹配" >&2
    exit 1
fi

echo "[entrypoint] 入口 ${APP_ENTRY} | 时区 ${TZ} | 监听 0.0.0.0:${PORT}"

exec python "$APP_ENTRY" --no-browser --host 0.0.0.0 --port "${PORT}"
