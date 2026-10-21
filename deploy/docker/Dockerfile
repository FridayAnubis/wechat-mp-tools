#
# 只打包「公众号 + RSS」的独立服务（入口 app_mp.py）。
# 抖音 / 快手 / 小红书 / B 站 / 视频号 / 转码 / 桌面壳 都不进镜像，
# 依赖从 12 个降到 6 个。应用代码只有 4 MB，镜像的绝大部分是 Python 运行时。
#
# 实测体积（arm64）：
#   默认（无浏览器）解压 244 MB / 拉取 51 MB
#   带 Chromium     解压 1.96 GB / 拉取 536 MB
#
# 构建（在仓库根目录）：
#   docker build -f deploy/docker/Dockerfile -t wechat-mp-tools-mp:latest .
#
# 需要 PDF 导出时才装浏览器（+1.7 GB，换来 PDF 与风控兜底）：
#   docker build -f deploy/docker/Dockerfile --build-arg INSTALL_BROWSERS=1 -t wechat-mp-tools-mp:pdf .

FROM python:3.12-slim

ARG INSTALL_BROWSERS=0

# PLAYWRIGHT_BROWSERS_PATH 必须落在 /app 下，
# backend/runtime.py 的 bundled_browsers_available() 只认 resource_dir()/ms-playwright
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai \
    PORT=5200 \
    APP_ENTRY=app_mp.py \
    PLAYWRIGHT_BROWSERS_PATH=/app/ms-playwright

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl tzdata \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo "$TZ" > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements-mp.txt ./
# 精简模式不装 playwright：它自带 driver + node，光 Python 包就 136MB，
# 而下载/抓取走的是 requests，缺了它只会让「风控兜底」这条路径返回错误（已被 except 捕获）
RUN if [ "$INSTALL_BROWSERS" = "1" ]; then \
        pip install -r requirements-mp.txt ; \
    else \
        echo "精简模式：跳过 playwright" \
        && grep -viE '^[[:space:]]*playwright' requirements-mp.txt > /tmp/req-lite.txt \
        && pip install -r /tmp/req-lite.txt \
        && rm -f /tmp/req-lite.txt ; \
    fi

# 浏览器：PDF 导出走 Chrome 命令行的 --print-to-pdf，无头兜底走 Playwright
# 目录名随架构变化（amd64: chrome-linux / arm64: chrome-linux-arm64），所以用 glob 定位，
# 并软链到 /usr/local/bin/chromium，供 downloader.find_chromium_executable() 从 PATH 找到
RUN if [ "$INSTALL_BROWSERS" = "1" ]; then \
        python -m playwright install --with-deps chromium \
        && chmod -R a+rX /app/ms-playwright \
        && CHROME_BIN="$(ls -d /app/ms-playwright/chromium-*/chrome-linux*/chrome | head -n 1)" \
        && ln -sf "$CHROME_BIN" /usr/local/bin/chromium \
        && echo "chromium -> $CHROME_BIN" ; \
    else \
        echo "skip chromium: PDF 导出与无头浏览器兜底将不可用" ; \
    fi

COPY app_mp.py ./
COPY backend ./backend
COPY frontend ./frontend

COPY deploy/docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh && mkdir -p /app/data

EXPOSE 5200

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/healthz" >/dev/null || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
