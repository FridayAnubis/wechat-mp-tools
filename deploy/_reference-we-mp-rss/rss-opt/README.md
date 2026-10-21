# RSS 优化方案

针对 `we-mp-rss` v1.5.2 的订阅侧（`apis/rss.py` + `core/rss.py`）做的定点优化。接口路径、参数、返回格式全部保持不变，属于可直接替换的实现。

## 一、上游存在的六个问题

| # | 问题 | 位置 | 后果 |
|---|---|---|---|
| 1 | 读接口 `is_update` 默认 `True` | `apis/rss.py` 的 `rss()` / `get_mp_articles_source()` | 阅读器每轮询一次就 JOIN 查询 + 逐篇重写正文缓存 + 序列化 XML，缓存等于没有 |
| 2 | 缓存无 TTL，键里不含 `ctype`/`template` | `core/rss.py` `__init__` | 采集一旦失效，订阅永久停在旧内容；同一 feed 换格式会读到上一次的产物 |
| 3 | 异常分支返回未定义变量 | `get_mp_articles_source` 末尾 `return Response(content=rss_xml)` | 首次生成就抛错时 `rss_xml` 未赋值 → `UnboundLocalError` → 500，且掩盖真实原因 |
| 4 | `%a %b` 依赖 locale | `datetime_to_rfc822` | 容器 `LANG=zh_CN` 时输出「周三, 21 十月 2026」，RSS 客户端直接判为非法时间 |
| 5 | CDATA 先拼字符串再交给 ElementTree | `generate_rss` | 输出成 `&lt;![CDATA[...]]&gt;`，等于没包，正文里的 HTML 被当普通文本 |
| 6 | `rss_domain` 末尾已补 `/`，拼接又加 `/` | `apis/rss.py` | `https://x.com//views/article/xxx`，部分客户端不会自动纠正 |

另外还有三处顺手修掉的：Atom 的 `updated` 用了 RFC822（规范要求 RFC3339）、`author` 直接塞文本（规范要求 `<name>`）、每次请求都执行却从未使用的 `COUNT(*)`。

## 二、改动内容

### `core/rss.py`

- **时间格式化**：`to_rfc822()` / `to_rfc3339()` 用显式英文星期与月份表，不再受 locale 影响；输入兼容 datetime、ISO 串、`YYYY-MM-DD HH:MM:SS`。
- **CDATA**：改成「先写占位符 → 序列化后替换成真 CDATA」，并把正文里的 `]]>` 转义为 `]]]]><![CDATA[>`，保证输出一定是合法 XML。
- **缓存**：新增 `cache_mtime()` / `is_cache_fresh(max_age)` / `get_cache(max_age)`；写入改为 `.tmp` + `os.replace()` 原子替换，避免并发读到半截文件。
- **正文缓存**：新增 `cache_content_if_needed()`，只在缺失或正文为空时落盘。上游是每次生成都把全部文章正文重写一遍。
- **路径安全**：`startswith` 换成 `os.path.commonpath()`，堵住 `data/cache/rss-evil` 这类前缀绕过。
- **新增 `md` / `txt` 输出**：上游 `.md` 实际返回 Atom XML；现在返回真正的 Markdown / 纯文本。

### `apis/rss.py`

- **默认走缓存**：`/feed/{id}.{ext}`、`/rss/{id}`、`/api`、`/tag`、`/search` 的 `is_update` 默认改为 `False`；强制刷新仍走 `/rss/{id}/fresh`、`/rss/fresh` 或显式 `?is_update=true`。
- **HTTP 缓存**：统一出口加 `ETag`、`Last-Modified`、`Cache-Control: public, max-age=<ttl>`，命中 `If-None-Match` 直接 304。阅读器轮询成本从「一次查询 + 一次序列化」降到「一次 304」。
- **stale-if-error**：生成失败时先返回旧缓存（`max-age=60`），只有连旧缓存都没有才 503。上游在这种情况下直接 500。
- **URL 归一**：`_rss_base_url()` 统一 `rstrip("/")`，`_abs_url()` 处理相对/绝对两种封面地址，修掉双斜杠和 `base + None`。
- **可选鉴权**：新增 `rss.access_token`，配置后 `/rss/*`、`/feed/*` 需要 `?token=xxx` 或 `Authorization: Bearer xxx`；留空则完全保持上游的公开行为，不破坏现有订阅。
- **去掉无用 COUNT**：`total = query.count()` 的结果从未被使用，每次请求白跑一次全表计数。

## 三、新增配置项

写在 `deploy/docker/config.yaml` 的 `rss:` 段，也可用环境变量覆盖：

```yaml
rss:
  max_items: ${RSS_MAX_ITEMS:-100}      # limit 上限；上游缺此项时会退化成 page_size=30，请求 100 条实际只给 30
  cache_ttl: ${RSS_CACHE_TTL:-300}      # 输出缓存有效期（秒），0 = 永不过期、只靠采集后失效
  access_token: ${RSS_ACCESS_TOKEN:-}   # 留空 = 不鉴权
```

> `max_items` 是上游一个很隐蔽的坑：`clamp_rss_limit()` 读的是 `rss.max_items`，而 `config.example.yaml` 里没有这个键，于是回退到 `page_size`（默认 30），**传 `limit=100` 也只会拿到 30 条**。

## 四、上线方式

### A. 挂载文件（推荐，不用重建镜像）

在 `deploy/docker/docker-compose.yaml` 里取消这两行注释，然后 `docker compose up -d`：

```yaml
volumes:
  - ../rss-opt/files/apis/rss.py:/app/apis/rss.py:ro
  - ../rss-opt/files/core/rss.py:/app/core/rss.py:ro
```

### B. 覆盖源码后重建

```bash
./apply.sh                       # 备份原文件并覆盖
cd ../we-mp-rss && docker build -t we-mp-rss:opt .
# 再把 .env 里的 WERSS_IMAGE 改成 we-mp-rss:opt
```

`apply.sh` 会把原文件备份到 `<repo>/.rss-opt-backup/<时间戳>/`，回滚就是拷回去。

## 五、验证

```bash
FEED=MP_WXS_1234567890 BASE=https://rss.example.com ./verify.sh
```

会检查：200、XML 可解析、ETag + 304、二次请求耗时、双斜杠、gzip。全部通过说明补丁生效。

手工看两个关键点：

```bash
curl -sI https://rss.example.com/feed/MP_WXS_xxx.xml | grep -Ei 'etag|cache-control'
curl -s  https://rss.example.com/feed/MP_WXS_xxx.xml | head -c 400
```

## 六、兼容性与取舍

**不变**：所有路由、查询参数、`ext` 取值、JSON 字段。

**有变化**：

| 项 | 上游 | 现在 |
|---|---|---|
| `is_update` 默认值 | `True` | `False` |
| `.md` 返回 | Atom XML | Markdown 文本 |
| RSS item | 含非标准 `<id>` | 只保留 `<guid>` |
| Atom `updated` | RFC822 | RFC3339 |
| Atom `author` | 文本节点 | `<author><name>` |
| 生成失败 | 500 | 旧缓存 60s，无缓存才 503 |

**还没做（按需再说）**：

- 缓存仍按 `limit/offset` 分文件，`offset` 组合多时文件会变多。更彻底的做法是「每个 feed 只缓存一份全量 JSON，请求时内存切片」，改动量更大。
- `data/cache/content/` 没有清理策略，会随文章数线性增长，需要配合定时任务或采集时顺带清理。
- 没加分布式锁，多副本部署会互相覆盖缓存文件。要横向扩就得把缓存换成 Redis。

## 七、文件清单

```
rss-opt/
├── files/apis/rss.py    # 替换 we-mp-rss/apis/rss.py
├── files/core/rss.py    # 替换 we-mp-rss/core/rss.py
├── apply.sh             # 覆盖 + 备份 + 语法检查
├── verify.sh            # 线上验证
└── README.md
```
