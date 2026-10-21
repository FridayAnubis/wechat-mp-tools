# RSS 订阅优化

针对本项目**已有**的 RSS 实现做的改造。原来在 `backend/articles.py` 的 `get_rss()` 里内联拼 XML，现在抽到 `backend/rss_feed.py`，路由和 URL 完全不变，前端不用改。

改造后的 RSS 是独立服务（`app_mp.py`）的核心输出：订阅源不依赖桌面端在线，容器里由 `rss_scheduler` 定时增量抓取，阅读器直接拉 `/api/articles/rss`。

## 一、原实现的四个问题

| # | 问题 | 位置 | 后果 |
|---|---|---|---|
| 1 | 全局订阅只遍历 `rss_subscriptions.json` | `get_rss` 合并逻辑 | **订阅列表为空时，`rss_articles.json` 里的文章全部丢失**。本机实测：383 篇只剩 14 篇 |
| 2 | 每个订阅都把 `rss_articles.json` 整份重新解析 | `rss_scheduler.get_articles()` 被循环调用 | N 个订阅 = N 次全量 JSON 解析，成本随订阅数线性放大 |
| 3 | 没有条数上限、没有排序 | `for item in items` | 一次请求把全部历史 + 全部订阅文章塞进一个 feed；顺序是写入顺序，阅读器里时间乱跳 |
| 4 | 每次请求都读全部 `content.txt` | 循环内 `read_text` | 纯磁盘 IO，条数越多越慢；无缓存、无 ETag，阅读器每次轮询都全量重算 |

另外两个小问题：CDATA 里把 `]]>` 替换成 `]]&gt;`（CDATA 内不做实体解析，这会把字面量 `]]&gt;` 写进正文）；`request.host_url` 在反代/容器后生成的是内网地址。

## 二、改动内容

### 新增 `backend/rss_feed.py`

- **一次加载**：`collect_items()` 只读一次 `rss_articles.json` 和 `download_history.json`，按公众号建索引，不再按订阅循环解析。
- **全局口径统一**：全局 feed 直接取 `rss_articles.json` 里所有账号的文章，与 `/rss/<account>` 的行为一致（原来两者口径不同）。
- **排序 + 截断**：按 `publish_time` 倒序，`limit`（默认 50，最大 500）/ `offset` 分页。
- **窗口内才读正文**：只有进入当前窗口的条目才去读 `content.txt`，单篇上限 300KB。
- **进程内缓存 + ETag**：缓存键含两个文件的 `mtime`，数据一变自动失效；命中时直接返回上次拼好的 XML。
- **304**：`If-None-Match` 命中直接返回 304，阅读器轮询几乎零成本。
- **CDATA 正确转义**：`]]>` → `]]]]><![CDATA[>`。
- **反代友好**：优先用 `rss_base_url`，其次 `X-Forwarded-Proto` / `X-Forwarded-Host`。
- **可选令牌**：`rss_token` 非空时要求 `?token=xxx` 或 `Authorization: Bearer xxx`。

### 修改 `backend/articles.py`

`get_rss()` 从 140 行缩成 3 行，委托给 `rss_feed.rss_response()`。路由装饰器、URL、返回类型都没动：

```python
@articles_bp.route("/rss", methods=["GET"])
@articles_bp.route("/rss/<account>", methods=["GET"])
def get_rss(account=None):
    from backend.rss_feed import rss_response
    return rss_response(request, account)
```

### 修改 `backend/config.py`

`DEFAULT_SETTINGS` 增加四项，保证老配置文件也会补上默认值：

```python
"rss_max_items": 50,
"rss_cache_ttl": 300,
"rss_token": "",
"rss_base_url": "",
```

## 三、实测

本机真实数据（3 个公众号 / 383 篇文章 / 79 条下载历史）：

| 场景 | 旧实现 | 新实现 |
|---|---|---|
| 全局 feed 条数 | **14 条**（订阅列表为空，372 篇丢失） | 383 条可取，默认输出 50 |
| 全局 feed 耗时 | 39.5 ms | 38.2 ms（冷启）/ 6 ms（缓存命中） |
| 单账号 feed 耗时 | 5.8 ms（130 条，无上限） | 8.4 ms（50 条，含正文） |
| 轮询（带 ETag） | 每次全量重建 | 304，无 body |

当前数据量小，绝对耗时差距不明显；结构性差异在数据增长后放大：

- 旧实现每次请求读 **全部** `content.txt`，成本 O(全部文章)
- 新实现只读窗口内的 `limit` 篇，且 5 分钟内命中缓存是 O(1)

## 四、配置

优先级：`app_settings.json` → 环境变量 → 代码默认值。容器部署用环境变量最省事。

| 键 | 环境变量 | 默认 | 说明 |
|---|---|---|---|
| `rss_max_items` | `RSS_MAX_ITEMS` | 50 | 单次输出上限，最大 500 |
| `rss_cache_ttl` | `RSS_CACHE_TTL` | 300 | 缓存秒数，0 = 不缓存 |
| `rss_token` | `RSS_TOKEN` | 空 | 空 = 公开可读 |
| `rss_base_url` | `RSS_BASE_URL` | 空 | 如 `https://rss.example.com/`，反代时必填 |

改桌面版：编辑 `data/app_settings.json`，加进去即可，重启生效。

## 五、接口

| 路径 | 说明 |
|---|---|
| `GET /api/articles/rss` | 全部公众号 |
| `GET /api/articles/rss/<公众号名>` | 单账号，名字需 URL 编码 |
| `?limit=100` | 条数，默认 50，上限 500 |
| `?offset=100` | 翻页 |
| `?full=0` | 只输出摘要，不读正文（体积小很多） |
| `?token=xxx` | 配置了 `rss_token` 时必填 |

响应头：`ETag`、`Cache-Control: public, max-age=<ttl>`、`Last-Modified`。

## 六、验证

```bash
BASE=http://127.0.0.1:5200 ACCOUNT=新华社 ./verify.sh
```

检查：200、XML 合法、倒序、每条有 link、ETag、304、Cache-Control、缓存提速、双斜杠、单账号、令牌 401。

## 七、行为变化与回滚

**有变化**：

| 项 | 旧 | 新 |
|---|---|---|
| 全局 feed 数据来源 | 订阅列表 + 下载历史 | `rss_articles.json` 全部账号 + 下载历史 |
| 默认条数 | 无限 | 50（可用 `?limit=` 调大） |
| 排序 | 写入顺序 | 发布时间倒序 |
| 已退订账号 | 全局 feed 中不出现 | 仍会出现（`rss_articles.json` 里还有缓存） |

最后一条需要留意：`unsubscribe` 只删订阅记录，不清 `rss_articles.json`。如果退订后不希望文章继续出现在全局 feed，需要同时清掉该账号的缓存条目——这是原有行为，本次没有改动。

**回滚**：`git checkout backend/articles.py backend/config.py && rm backend/rss_feed.py`。

## 八、后续可做

- 把 `rss_articles.json` 换成 SQLite。现在是「整份 JSON 读改写」，文章多了之后写入会成为瓶颈（`rss_scheduler` 已经有 `_history_lock` 兜并发，但仍是全量重写）。
- 设置页加上 `rss_token` / `rss_base_url` 的输入框，现在只能改 JSON 或环境变量。
- 给 `content.txt` 缺失的文章补一个「已删除/已屏蔽」标记，避免每次都去 stat 一遍。
