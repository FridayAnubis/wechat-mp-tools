#!/usr/bin/env bash
# 验证 RSS 优化是否生效
#
#   FEED=MP_WXS_1234567890 BASE=https://rss.example.com ./verify.sh
#   RSS_ACCESS_TOKEN=xxx ./verify.sh
#
# 检查项：状态码 / XML 合法性 / ETag 304 / 缓存加速 / 双斜杠 / 压缩

set -uo pipefail

BASE="${BASE:-http://127.0.0.1:8001}"
FEED="${FEED:-}"
TOKEN="${RSS_ACCESS_TOKEN:-}"
Q=""
[ -n "$TOKEN" ] && Q="?token=$TOKEN"

if [ -z "$FEED" ]; then
    echo "用法：FEED=<公众号ID> BASE=<站点地址> $0"
    echo "公众号ID 可在 Web UI 的订阅地址里看到，形如 MP_WXS_1234567890"
    exit 1
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
URL="$BASE/feed/$FEED.xml$Q"
fail=0
ok()   { printf '  ✓ %s\n' "$*"; }
bad()  { printf '  ✗ %s\n' "$*"; fail=$((fail+1)); }

echo "目标：$URL"

read -r code t1 < <(curl -sS -o "$TMP/a.xml" -D "$TMP/h1" -w '%{http_code} %{time_total}' "$URL" || echo "000 0")
[ "$code" = "200" ] && ok "首次请求 200（${t1}s）" || bad "首次请求返回 $code"

if python3 -c "import xml.etree.ElementTree as ET,sys; ET.parse('$TMP/a.xml')" 2>/dev/null; then
    ok "XML 可解析"
else
    bad "XML 解析失败（可能是 CDATA 或编码问题）"
fi

etag="$(grep -i '^etag:' "$TMP/h1" | tr -d '\r' | cut -d' ' -f2-)"
if [ -n "$etag" ]; then
    ok "ETag: $etag"
    code304="$(curl -sS -o /dev/null -w '%{http_code}' -H "If-None-Match: $etag" "$URL")"
    [ "$code304" = "304" ] && ok "带 If-None-Match 返回 304（阅读器不再重复下载）" \
                           || bad "带 If-None-Match 返回 $code304，期望 304"
else
    bad "响应缺少 ETag 头（补丁可能没生效）"
fi

cc="$(grep -i '^cache-control:' "$TMP/h1" | tr -d '\r' | cut -d' ' -f2-)"
[ -n "$cc" ] && ok "Cache-Control: $cc" || bad "缺少 Cache-Control"

read -r code2 t2 < <(curl -sS -o /dev/null -w '%{http_code} %{time_total}' "$URL")
[ "$code2" = "200" ] && ok "二次请求 200（${t2}s）" || bad "二次请求返回 $code2"
python3 - "$t1" "$t2" <<'PY'
import sys
t1, t2 = float(sys.argv[1]), float(sys.argv[2])
if t1 > 0 and t2 < t1:
    print("  ✓ 缓存生效：%.3fs -> %.3fs（%.0f%% 提速）" % (t1, t2, (1 - t2 / t1) * 100))
else:
    print("  · 两次耗时接近（%0.3fs / %0.3fs），数据量小时属正常" % (t1, t2))
PY

if grep -q '//views/article/' "$TMP/a.xml"; then
    bad "链接里仍有 //views/article 双斜杠"
else
    ok "链接无双斜杠"
fi

if grep -qi '^content-encoding: gzip' "$TMP/h1"; then
    ok "响应已压缩"
else
    echo "  · 未压缩（Nginx 未开 gzip 或直连 8001 属正常）"
fi

echo
if [ "$fail" -eq 0 ]; then
    echo "全部检查通过"
else
    echo "有 $fail 项未通过，请对照 README 的排查表"
    exit 1
fi
