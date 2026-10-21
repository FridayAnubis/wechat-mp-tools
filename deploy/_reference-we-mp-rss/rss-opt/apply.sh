#!/usr/bin/env bash
# 把优化版 apis/rss.py 与 core/rss.py 覆盖进 we-mp-rss 源码，并做语法检查
#
#   ./apply.sh                 # 默认改 ../../we-mp-rss
#   ./apply.sh /path/to/repo   # 指定其他路径
#
# 覆盖前会把原文件备份到 <repo>/.rss-opt-backup/<时间戳>/，回滚直接拷回去。

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${1:-$HERE/../../we-mp-rss}"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="$TARGET/.rss-opt-backup/$STAMP"

if [ ! -f "$TARGET/apis/rss.py" ] || [ ! -f "$TARGET/core/rss.py" ]; then
    echo "✗ $TARGET 看起来不是 we-mp-rss 源码目录（找不到 apis/rss.py 或 core/rss.py）"
    exit 1
fi

echo "目标仓库：$TARGET"
mkdir -p "$BACKUP"
cp "$TARGET/apis/rss.py" "$BACKUP/apis__rss.py"
cp "$TARGET/core/rss.py" "$BACKUP/core__rss.py"
echo "原文件已备份到：$BACKUP"

cp "$HERE/files/apis/rss.py" "$TARGET/apis/rss.py"
cp "$HERE/files/core/rss.py" "$TARGET/core/rss.py"
echo "已覆盖 apis/rss.py、core/rss.py"

PY="${PYTHON:-python3}"
if command -v "$PY" >/dev/null 2>&1; then
    ( cd "$TARGET" && "$PY" -m py_compile apis/rss.py core/rss.py ) \
        && echo "✓ 语法检查通过" \
        || { echo "✗ 语法检查失败，正在回滚"; cp "$BACKUP/apis__rss.py" "$TARGET/apis/rss.py"; cp "$BACKUP/core__rss.py" "$TARGET/core/rss.py"; exit 1; }
else
    echo "! 未找到 $PY，跳过语法检查"
fi

cat <<'TIP'

下一步：
  1. 在 deploy/docker/config.yaml 里确认已包含
       rss.max_items / rss.cache_ttl / rss.access_token
  2. 重启服务
       docker compose restart app        # 用官方镜像时需重新构建镜像才能生效
       # 官方镜像不含改动，必须二选一：
       #   a) 自己构建：docker build -t we-mp-rss:opt . && 改 .env 里的 WERSS_IMAGE
       #   b) 把两个文件挂进容器（见 README「不重建镜像的挂载法」）
  3. 跑 ./verify.sh 验证

回滚：
  cp <备份目录>/apis__rss.py <repo>/apis/rss.py
  cp <备份目录>/core__rss.py <repo>/core/rss.py
TIP
