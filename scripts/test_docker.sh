#!/usr/bin/env bash
set -euo pipefail
image=${1:-wechat-mp-tools:docker}
name="wechat-mp-smoke-$$"
volume="${name}-data"
cleanup() {
    result=$?
    if [ "$result" -ne 0 ]; then
        docker logs "$name" >&2 || true
    fi
    docker rm -f "$name" >/dev/null 2>&1 || true
    docker volume rm "$volume" >/dev/null 2>&1 || true
}
trap cleanup EXIT
docker volume create "$volume" >/dev/null
start() {
    docker run -d --name "$name" --platform linux/amd64 --shm-size=256m \
        -e WECHAT_MP_PUBLIC_URL=https://rss.example.test \
        -v "$volume:/app/data" "$image" >/dev/null
    for attempt in $(seq 1 60); do
        if docker exec "$name" python docker/healthcheck.py >/dev/null 2>&1; then
            return
        fi
        sleep 2
    done
    docker logs "$name"
    return 1
}
start
docker exec "$name" python scripts/docker_smoke.py
docker stop -t 15 "$name" >/dev/null
docker rm "$name" >/dev/null
# Recreate rather than merely restart: only the mounted volume can preserve data.
start
docker exec "$name" python scripts/docker_smoke.py --verify
