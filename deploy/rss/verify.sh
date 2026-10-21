#!/usr/bin/env bash
# 验证本地项目的 RSS 输出
#
#   ./verify.sh                                   # 默认 http://127.0.0.1:5200
#   BASE=https://mp.example.com ./verify.sh
#   ACCOUNT=新华社 ./verify.sh                     # 顺便验单账号订阅
#   RSS_TOKEN=xxx ./verify.sh                     # 带令牌校验

set -uo pipefail

BASE="${BASE:-http://127.0.0.1:5200}"
ACCOUNT="${ACCOUNT:-}"
TOKEN="${RSS_TOKEN:-}"
Q=""
[ -n "$TOKEN" ] && Q="?token=$TOKEN"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
URL="$BASE/api/articles/rss$Q"

# 本机地址绕过系统代理，避免 HTTP_PROXY 把请求转走
NP=()
case "$BASE" in
    *127.0.0.1*|*localhost*) NP=(--noproxy '*') ;;
esac
fail=0
ok()  { printf '  ✓ %s\n' "$*"; }
bad() { printf '  ✗ %s\n' "$*"; fail=$((fail+1)); }

echo "目标：$URL"

read -r code t1 < <(curl "${NP[@]}" -sS -o "$TMP/a.xml" -D "$TMP/h1" -w '%{http_code} %{time_total}' "$URL" || echo "000 0")
[ "$code" = "200" ] && ok "首次请求 200（${t1}s）" || { bad "首次请求返回 $code"; head -c 200 "$TMP/a.xml"; echo; }

python3 - "$TMP/a.xml" <<'PY' || fail=$((fail+1))
import sys, xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
try:
    root = ET.parse(sys.argv[1]).getroot()
except Exception as e:
    print("  ✗ XML 解析失败：%s" % e); sys.exit(1)
items = root.findall("./channel/item")
print("  ✓ XML 合法，item 数：%d" % len(items))
if not items:
    print("  · feed 为空（还没有下载记录或订阅数据）"); sys.exit(0)
dates = [parsedate_to_datetime(i.findtext("pubDate")) for i in items if i.findtext("pubDate")]
if dates == sorted(dates, reverse=True):
    print("  ✓ 按发布时间倒序")
else:
    print("  ✗ 顺序不是倒序"); sys.exit(1)
missing = [i for i in items if not (i.findtext("link") or "").strip()]
if missing:
    print("  ✗ 有 %d 条缺 link" % len(missing)); sys.exit(1)
print("  ✓ 每条都有 link")
PY

etag="$(grep -i '^etag:' "$TMP/h1" | tr -d '\r' | cut -d' ' -f2-)"
if [ -n "$etag" ]; then
    ok "ETag: $etag"
    c304="$(curl "${NP[@]}" -sS -o /dev/null -w '%{http_code}' -H "If-None-Match: $etag" "$URL")"
    [ "$c304" = "304" ] && ok "If-None-Match 命中 304" || bad "期望 304，实际 $c304"
else
    bad "缺少 ETag（补丁未生效？）"
fi

cc="$(grep -i '^cache-control:' "$TMP/h1" | tr -d '\r' | cut -d' ' -f2-)"
[ -n "$cc" ] && ok "Cache-Control: $cc" || bad "缺少 Cache-Control"

read -r code2 t2 < <(curl "${NP[@]}" -sS -o /dev/null -w '%{http_code} %{time_total}' "$URL")
[ "$code2" = "200" ] && ok "二次请求 200（${t2}s）" || bad "二次请求返回 $code2"
python3 - "$t1" "$t2" <<'PY'
import sys
t1, t2 = float(sys.argv[1]), float(sys.argv[2])
if t1 > 0 and t2 < t1 * 0.8:
    print("  ✓ 缓存生效：%.3fs -> %.3fs（省 %.0f%%）" % (t1, t2, (1 - t2 / t1) * 100))
else:
    print("  · 两次耗时接近（%.3fs / %.3fs），数据量小或缓存已热" % (t1, t2))
PY

grep -q '//api/articles' "$TMP/a.xml" && bad "链接里出现 //api/articles 双斜杠" || ok "链接无双斜杠"

if grep -qi '^content-encoding: gzip' "$TMP/h1"; then
    ok "响应已压缩"
else
    echo "  · 未压缩（直连 5200 或 Nginx 未开 gzip 属正常）"
fi

if [ -n "$ACCOUNT" ]; then
    enc="$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))" "$ACCOUNT")"
    code3="$(curl "${NP[@]}" -sS -o "$TMP/b.xml" -w '%{http_code}' "$BASE/api/articles/rss/$enc$Q")"
    [ "$code3" = "200" ] && ok "单账号订阅 200" || bad "单账号订阅返回 $code3"
    if python3 - "$TMP/b.xml" <<'PY'
import sys, xml.etree.ElementTree as ET
r = ET.parse(sys.argv[1]).getroot()
acc = {i.findtext("link") for i in r.findall("./channel/item")}
print("  ✓ 单账号 feed 解析正常，item 数：%d" % len(acc))
PY
    then :; else fail=$((fail+1)); fi
fi

if [ -n "$TOKEN" ]; then
    c401="$(curl "${NP[@]}" -sS -o /dev/null -w '%{http_code}' "$BASE/api/articles/rss")"
    [ "$c401" = "401" ] && ok "无令牌被拒 401" || bad "无令牌期望 401，实际 $c401"
fi

echo
if [ "$fail" -eq 0 ]; then
    echo "全部检查通过"
else
    echo "有 $fail 项未通过"
    exit 1
fi
