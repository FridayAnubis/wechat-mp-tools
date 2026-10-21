#!/usr/bin/env bash
# 备份容器部署的数据目录
#
#   ./backup.sh                 # 只备配置与索引（data/*.json、data/*.db 等）
#   BACKUP_ARTICLES=1 ./backup.sh   # 连已下载文章一起备（可能很大）
#   KEEP_DAYS=0 ./backup.sh     # 不自动清理旧备份
#
# 只写入和清理 $BACKUP_DIR 内部。

set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="$BASE_DIR/data"
BACKUP_DIR="${BACKUP_DIR:-$BASE_DIR/backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"
WITH_ARTICLES="${BACKUP_ARTICLES:-0}"
STAMP="$(date +%Y%m%d-%H%M%S)"
TARGET="$BACKUP_DIR/mptools-$STAMP"

log() { printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*"; }

if [ ! -d "$DATA_DIR" ]; then
    echo "✗ 找不到数据目录：$DATA_DIR"
    exit 1
fi

mkdir -p "$TARGET"

EXCLUDES=(--exclude='data/articles_full' --exclude='data/pdf' --exclude='data/markdown')
if [ "$WITH_ARTICLES" = "1" ]; then
    EXCLUDES=()
    log "包含已下载文章，包会比较大"
fi

log "打包 $DATA_DIR ..."
tar -czf "$TARGET/data.tar.gz" "${EXCLUDES[@]}" -C "$BASE_DIR" data

# 单独存一份设置，方便直接看/对比
if [ -f "$DATA_DIR/app_settings.json" ]; then
    cp -f "$DATA_DIR/app_settings.json" "$TARGET/app_settings.json"
fi

{
    echo "time=$STAMP"
    echo "with_articles=$WITH_ARTICLES"
    echo "host=$(hostname)"
} > "$TARGET/meta.txt"

du -sh "$TARGET" 2>/dev/null || true

if [ "$KEEP_DAYS" -gt 0 ] 2>/dev/null; then
    log "清理 $KEEP_DAYS 天前的备份（仅限 $BACKUP_DIR）..."
    find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -type d -name 'mptools-*' \
        -mtime +"$KEEP_DAYS" -print -exec rm -rf {} + || true
fi

log "完成：$TARGET"
log "恢复：docker compose down && tar -xzf $TARGET/data.tar.gz -C $BASE_DIR && docker compose up -d"
