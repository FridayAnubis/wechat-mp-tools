FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    WECHAT_MP_HEADLESS=1 \
    CHROME_PATH=/usr/bin/chromium \
    XDG_CONFIG_HOME=/app/data/.config \
    XDG_CACHE_HOME=/app/data/.cache \
    TZ=Asia/Shanghai

RUN --mount=type=secret,id=build_ca,mode=0444 \
    if [ -f /run/secrets/build_ca ]; then \
        echo 'Acquire::https::CaInfo "/run/secrets/build_ca";' > /etc/apt/apt.conf.d/99build-ca; \
    fi \
    && sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update --error-on=any && apt-get install -y --no-install-recommends \
        chromium ffmpeg fonts-noto-cjk ca-certificates tzdata tini \
    && rm -rf /var/lib/apt/lists/* /etc/apt/apt.conf.d/99build-ca \
    && groupadd --gid 1000 app \
    && useradd --uid 1000 --gid app --create-home app

WORKDIR /app
COPY requirements.txt ./
COPY docker/requirements.txt ./docker/requirements.txt
RUN --mount=type=secret,id=build_ca,mode=0444 \
    if [ -f /run/secrets/build_ca ]; then export PIP_CERT=/run/secrets/build_ca; fi \
    && pip install -r docker/requirements.txt && pip check
COPY . .
RUN chmod -R a+rX /app && mkdir -p /app/data && chown app:app /app/data

USER 1000:1000
EXPOSE 5200
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "docker/healthcheck.py"]
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "docker/serve.py"]
