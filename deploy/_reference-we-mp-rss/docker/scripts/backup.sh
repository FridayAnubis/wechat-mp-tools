#!/usr/bin/env bash
# WeRSS 备份脚本：导出数据库 + 打包 data 目录（跳过可重建的缓存）
#
#   ./backup.sh              # 默认 MySQL 模式，保留 14 天
#   MODE=sqlite ./backup.sh  # 单机版
#   KEEP_DAYS=0 ./backup.sh  # 不自动清理旧备份
#
# 只会写入并清理 $BACKUP_DIR 内部，不触碰其他目录。

set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="${BACKUP_DIR:-$BASE_DIR/backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"
MODE="${MODE:-mysql}"
STAMP="$(date +%Y%m%d-%H%M%S)"
TARGET="$BACKUP_DIR/we-rss-$STAMP"

cd "$BASE_DIR"
mkdir -p "$TARGET"

log() { printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*"; }

if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
fi

if [ "$MODE" = "mysql" ]; then
    log "导出 MySQL ..."
    docker compose exec -T db \
        mysqldump -u root -p"${MYSQL_ROOT_PASSWORD}" \
        --single-transaction --quick --routines --events \
        --default-character-set=utf8mb4 \
        "${MYSQL_DATABASE:-we_mp_rss}" | gzip > "$TARGET/db.sql.gz"
    log "数据库已导出：$TARGET/db.sql.gz"
else
    log "SQLite 模式：数据库随 data 目录一起打包"
fi

log "打包 data（跳过 cache/pdf/markdown）..."
tar -czf "$TARGET/data.tar.gz" \
    --exclude='data/cache' \
    --exclude='data/pdf' \
    --exclude='data/markdown' \
    -C "$BASE_DIR" data 2>/dev/null || true

cp -f .env "$TARGET/env.bak" 2>/dev/null || true
chmod 600 "$TARGET/env.bak" 2>/dev/null || true

{
    echo "time=$STAMP"
    echo "mode=$MODE"
    echo "image=$(grep -E '^WERSS_IMAGE=' .env 2>/dev/null | cut -d= -f2-)"
    echo "rss_base_url=$(grep -E '^RSS_BASE_URL=' .env 2>/dev/null | cut -d= -f2-)"
} > "$TARGET/meta.txt"

du -sh "$TARGET" 2>/dev/null || true

if [ "$KEEP_DAYS" -gt 0 ] 2>/dev/null; then
    log "清理 $KEEP_DAYS 天前的备份（仅限 $BACKUP_DIR）..."
    find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -type d -name 'we-rss-*' \
        -mtime +"$KEEP_DAYS" -print -exec rm -rf {} + || true
fi

log "备份完成：$TARGET"
