# 公众号 + RSS 部署

同一套代码、两种镜像形态，默认只装公众号，需要时改三行配置切全功能版。

- 新入口：`app_mp.py`（只注册 5 个蓝图）
- 精简依赖：`requirements-mp.txt`（6 个包，AST 扫描真实导入得出）
- 编排：`deploy/docker/`
- RSS 改造：`backend/rss_feed.py`，细节见 `deploy/rss/README.md`

## 一、先选形态

| | 公众号版（默认，推荐） | 全功能版 |
|---|---|---|
| 包含 | 公众号登录 / 文章 / 下载 / 导出 / RSS / 代理 | 再加 抖音 · 快手 · 小红书 · B 站 · 转码 |
| 镜像体积（解压） | **244 MB** | 未实测，另加 Chromium 1.5 GB + ffmpeg |
| 镜像体积（拉取） | 51 MB | — |
| 内存建议 | 512 MB | 2 GB |
| 第三方依赖 | 6 个 | 12 个（含 mitmproxy、scrapling、curl_cffi） |
| 浏览器 / ffmpeg | 都不装 | Chromium + ffmpeg |
| 切换方式 | 默认 | 改 `.env` 三行 |

### 体积构成（实测）

默认镜像 **244 MB**，代码只占 4.3 MB：

| 组成 | 精简（默认） | 开启浏览器后 |
|---|---|---|
| 基础镜像 + 系统工具 | ~215 MB | ~232 MB |
| Python 依赖 | **11 MB** | 157 MB（playwright 包自己 136 MB） |
| Chromium + 系统库 + 字体 | **0** | 1.52 GB |
| 应用代码 | 4.3 MB | 4.3 MB |
| **合计（解压 / 拉取）** | **244 MB / 51 MB** | **1.96 GB / 536 MB** |

Chromium 那 1.52 GB 的构成：浏览器二进制 680 MB（完整版 400 + headless shell 276）、系统库 277 MB、字体 91 MB。

### 精简版少了什么、影响什么

`INSTALL_BROWSERS=0`（默认）会跳过三样东西：Chromium、playwright 的 Python 包、以及 playwright 自带的 ffmpeg。**系统 ffmpeg 从来就没装过**——只有全功能版为了 B 站混流才装。

实测影响：

| 能力 | 精简版 | 说明 |
|---|---|---|
| 公众号文章抓取 | ✅ 正常 | 实测抓到 3.5 MB HTML，`js_content` 完整 |
| 图片 / 视频资源下载 | ✅ 正常 | 走 `requests` 流式 |
| RSS 订阅 | ✅ 正常 | 验证脚本全绿 |
| PDF 导出 | ❌ | `export_html_to_pdf()` 返回 `False`，不报错 |
| 风控兜底 | ❌ | 命中验证码时返回「浏览器兜底抓取异常」，不崩 |

需要 PDF 归档时再单独打开：

```ini
INSTALL_BROWSERS=1
```

**建议默认用公众号版**，三个理由：

1. **只有 RSS 需要 7×24 常驻。** 抖音/小红书那些是「点一下、拿文件」的交互式工具，跑在服务器上，下载完还得想办法把文件取回来，收益不高。
2. **依赖面直接决定长期稳定性。** 完整 `requirements.txt` 里为了绕开 mitmproxy 的 `typing-extensions` 冲突专门钉了 `typing-extensions==4.14.0`，这类版本锁在无人值守的服务器上最容易变成「某天重启就起不来」。公众号链路只用 6 个包，冲突面小得多。
3. **视频号的登录方式不适合容器。** 解析和下载本身是纯 HTTP（腾讯元宝接口 / 私有 Worker + 内置 ISAAC64 解密），容器里能跑；卡点在拿元宝 Cookie 那一步——它用 `headless=False` 开一个**可见浏览器**让你扫码，容器没有显示器。剩下的只有「手动粘 Cookie」（会过期）和「公共 Worker」（第三方，随时可能挂）两条降级路。与其留个半残入口，不如把边界划清楚。

但全功能版确实有用武之地（比如在 NAS 上批量下抖音），所以保留，一条配置切换。

## 二、两种形态怎么切

改 `deploy/docker/.env` 三行，重新构建：

```ini
DOCKERFILE=deploy/docker/Dockerfile.full
IMAGE=wechat-mp-tools-full:latest
MEMORY_LIMIT=2g
```

```bash
docker compose up -d --build
```

前端会自动识别：公众号版隐藏抖音等入口，全功能版全显示。

## 三、拆了哪些、留了哪些

| 模块 | 文件 | 公众号版 | 全功能版 |
|---|---|---|---|
| 微信读书扫码登录 | `backend/auth.py` | ✅ | ✅ |
| 公众号账号与列表 | `backend/accounts.py` | ✅ | ✅ |
| 文章列表 / 下载 / 导出 / RSS | `backend/articles.py` | ✅ | ✅ |
| 账号池 | `backend/account_pool.py` | ✅ | ✅ |
| 代理配置 | `backend/proxy.py` | ✅ | ✅ |
| RSS 定时抓取 | `backend/rss_scheduler.py` | ✅ | ✅ |
| 内容过滤 | `backend/content_filter.py` | ✅ | ✅ |
| 抖音 / 快手 / 小红书 / B 站 | `backend/{douyin,kuaishou,xiaohongshu,bilibili}*.py` | ⛔ 构建时删除 | ✅ |
| 转码 / 字幕擦除 | `backend/transcode.py`、`subtitle_remover/` | ⛔ 构建时删除 | ✅ |
| 微信视频号 | `backend/channels.py` + `mitm_proxy.py` | ⛔ 构建时删除 | ⚠️ 仅手动 Cookie |
| 客户端自动更新 | `backend/updater.py` | ⛔ 构建时删除 | ✅ |
| 打开文件夹 / 文件 | `backend/articles.py` 相关路由 | ⚠️ 降级为宿主机路径提示 | ✅ |

### 公众号版到底删掉了哪些文件

`deploy/docker/Dockerfile` 先用一个 `backend-src` 阶段裁剪，再 `COPY --from` 进最终镜像。

**不是 `RUN rm`**——那只会在上层标记删除，文件仍留在 `COPY` 那一层，解包镜像还能捞出来。用独立阶段才是真的没有。

最终镜像 `/app/backend` 只剩 **18 个 .py**：

```
__init__ account_pool account_pool_api accounts articles auth avatar_cache config
content_filter downloader proxy request_profile rss_feed rss_scheduler runtime
waterwall wechat_url weread_signer
```

删掉的 21 个：

```
channels mitm_proxy douyin douyin_auth douyin_login douyin_mstoken douyin_sign
kuaishou kuaishou_auth xiaohongshu xiaohongshu_login bilibili bilibili_login
bilibili_sign transcode updater weread_browser cred_redact proxy_worker
refresh_queue sign
```

删除是安全的：用 AST 解析过 import 图，从 `app_mp.py` 出发能到达的模块里，
没有任何一个 import 这些被删的模块（它们只互相引用）。删完实测健康检查、RSS、
历史、内容过滤、首页全部正常。

### 各平台到底用什么方式下载

这决定了「需要什么运行时」，也是镜像体积的来源：

| 平台 | 解析 | 文件下载 | 需要的运行时 |
|---|---|---|---|
| 公众号文章 | `requests` + `bs4` 解析 HTML | `requests` 流式（图片/视频资源） | 无（浏览器仅风控兜底） |
| 公众号 PDF | — | Chrome 命令行 `--print-to-pdf` | **Chromium** |
| 抖音 | `requests` + 自研 SecSDK 签名 | `requests` 流式 + `Range` 断点续传 | 无（登录扫码用浏览器） |
| 快手 | `requests` | `requests` 流式 + `Range` 断点续传 | 无 |
| 小红书 | `curl_cffi`（模拟 TLS 指纹） | `requests` 流式 | 无 |
| B 站 | `requests`（`BilibiliClient`） | `requests` + **ffmpeg** 音视频混流 | **ffmpeg** |
| 微信视频号 | 腾讯元宝接口 / 私有 Worker（纯 HTTP） | `requests` + 内置 ISAAC64 解密 | 无（但扫码登录需可见浏览器，容器里做不了） |

结论：**真正下载视频/图片走的是 HTTP 流式请求，不是浏览器**。Chromium 只在四个地方用到：PDF 导出、微信风控兜底、各平台扫码登录、需要渲染的页面解析。所以不装 Chromium 时，公众号抓取与 RSS 完全不受影响。

`app_mp.py` 与 `app.py` 共用 `backend/` 代码，不复制实现。

## 四、快速开始

```bash
cd deploy/docker
cp .env.example .env
mkdir -p data

docker compose up -d --build
docker compose logs -f
```

访问 `http://<服务器IP>:5200`。默认只绑 `127.0.0.1`，局域网直连把 `BIND_ADDR` 改成 `0.0.0.0`。

默认构建的就是 244 MB 精简版（无 Chromium / 无 ffmpeg / 无 playwright）。需要 PDF 导出时：

```bash
# .env 里 INSTALL_BROWSERS=1，然后
docker compose up -d --build
```

## 五、接口

| 路径 | 说明 |
|---|---|
| `GET /healthz` | 健康检查，返回版本与形态 |
| `GET /api/capabilities` | 启用/禁用的能力清单（前端据此收窄导航） |
| `GET /api/articles/rss` | 全部公众号订阅源 |
| `GET /api/articles/rss/<公众号名>` | 单账号订阅源 |
| `GET /api/settings` | 应用设置 |
| `GET/POST /api/articles/content-filter` | 内容过滤规则读写 |
| `POST /api/articles/content-filter/apply-existing` | 对已下载文章重跑过滤 |

RSS 支持 `?limit=` `?offset=` `?full=0` `?token=`，响应带 `ETag` / `Cache-Control`，命中 `If-None-Match` 返回 304。

公众号版未打包的接口（如 `/api/douyin/*`）返回：

```json
{"error": "当前部署仅包含公众号与 RSS 功能，该接口未启用", "path": "/api/douyin/user"}
```

## 六、前端会自动收窄

`frontend/js/mp-mode.js` 在页面加载后请求 `/api/capabilities`：

- `mode === 'mp-only'` 时隐藏未打包模块的导航项，并收起空掉的分组
- 直接输地址访问 `#dy_user` 这类页面会被重定向回文章页
- 桌面版没有这个接口（返回的是 index.html，content-type 不是 json），脚本自动跳过，**桌面行为完全不变**

### 设置页也会收窄

带 `data-mp-hide` 标记的卡片只在公众号版隐藏，实现方式是注入一条
`body.mp-only [data-mp-hide]{display:none}` 的 CSS 规则，而不是遍历 DOM——
设置页是路由懒渲染的，可能晚于 capabilities 返回才出现，CSS 能覆盖到后渲染的节点。

| 被隐藏的卡片 | 原因 |
|---|---|
| 🟢 视频号服务配置 | `channels` 模块在构建时已从镜像删除，Worker 域名填了也没人读 |
| 📖 微信读书中转服务配置 | 全仓没有后端代码读取该设置（直连官方接口） |
| 📺 Bilibili 下载配置 | `bilibili` 模块没打包 |

保留的三张（下载与存储 / 抓取与防封 / RSS 订阅时间段）在公众号版都是真实生效的，
其中 RSS 定时采集由 `app_mp.py` 里的 `rss_scheduler.start()` 驱动。

## 七、安全：应用本身没有登录

`backend/auth.py` 是「微信读书扫码登录」，不是 Web 访问控制。**整个服务没有任何鉴权**——原来只跑在本机 `127.0.0.1`，所以没问题；一旦对外，等于把抓取能力、账号 cookie、下载历史全部公开。

必须二选一：

1. **Nginx Basic Auth**（`nginx/wechat-mp-tools.conf` 已写好）：整站加密码，只放行 `/api/articles/rss` 给阅读器。
2. **只绑 `127.0.0.1`**，走 SSH 隧道或 Tailscale。

RSS 路径单独用 `RSS_TOKEN` 鉴权，阅读器地址填 `https://你的域名/api/articles/rss?token=xxx`。

## 八、数据与持久化

只有 `/app/data` 需要持久化：

| 文件 | 内容 |
|---|---|
| `app_settings.json` | 应用设置（下载目录、RSS 参数等） |
| `accounts.json` / `account_pool.json` | 公众号账号与凭证池 |
| `download_history.json` | 下载历史（RSS 数据源之一） |
| `rss_subscriptions.json` / `rss_articles.json` | RSS 订阅与抓取缓存 |
| `articles_full/` | 已下载文章（HTML / Markdown / PDF / 图片） |

`data/` 里有 cookie 和 token，根目录 `.dockerignore` 已把整个目录排除，不会被 `COPY` 进镜像。

想把文章落到独立磁盘：把设置里的 `download_dir` 改成 `/downloads`，再启用 compose 里注释掉的挂载。

## 九、容器里的「打开」与路径

桌面版这三个按钮调的是系统程序（`open` / `explorer` / `xdg-open`），容器里没有桌面，直接调会报
`[Errno 2] No such file or directory: 'xdg-open'`。现在改成按环境自动降级：

| 按钮 | 桌面版 | 容器版 |
|---|---|---|
| 打开 MD / PDF / 文章 | 系统默认程序 | 返回 `serve-file` 链接，浏览器新标签页打开 |
| 打开文件夹 / 所在目录 | 文件管理器 | 返回 501 + 宿主机路径提示，如「请在宿主机打开：./data/articles_full/潇湘晨报」 |

判断依据是 `backend/runtime.desktop_available()`：macOS/Windows 直接可用；Linux 看 `DISPLAY`/`xdg-open`；有 `/.dockerenv` 视为容器。可用 `WECHAT_MP_DESKTOP=1` 强制覆盖。

提示里的路径前缀由 `HOST_DATA_DIR` 决定，默认 `./data`（相对 compose 目录）。

### 迁移旧数据时的路径

`download_history.json` 里存的是**绝对路径**，从桌面版搬到容器后会全部失效。现在的处理：

1. 把整个 `data/` 拷到 `deploy/docker/data/`
2. `.env` 里设 `DOWNLOAD_DIR=/app/data/articles_full`（可选，用于兜底下载目录）
3. 历史里的 `/Users/xxx/.../articles_full/公众号/文章` 会按 `articles_full` 之后的部分**自动重定位**到当前下载目录——前提是文件确实拷过来了
4. 界面显示宿主机路径（`./data/...`），点「打开」走浏览器

没有拷贝文件、只拷了 JSON 时，路径保持原样不报错，只是点开会提示文件不存在。

## 十、反向代理

`nginx/wechat-mp-tools.conf` 的关键点：

- `X-Forwarded-Proto` / `X-Forwarded-Host`：RSS 链接靠它生成，不传会变成 `http://内网IP/...`
- `proxy_read_timeout 300s`：抓取接口跑得久，默认 60s 会掐断
- `gzip_types application/xml`：RSS 是纯文本，压缩率很高
- Basic Auth 只保护 UI，RSS 路径 `auth_basic off`

配好反代后建议在 `.env` 里显式写 `RSS_BASE_URL=https://你的域名/`。

## 十一、备份与升级

```bash
./scripts/backup.sh                    # 只备配置与索引
BACKUP_ARTICLES=1 ./scripts/backup.sh  # 连已下载文章一起备
```

升级：

```bash
./scripts/backup.sh
git pull
docker compose up -d --build
```

## 十二、排障

| 现象 | 原因 | 处理 |
|---|---|---|
| 页面 502 / 起不来 | 初始化慢或端口冲突 | `docker compose logs -f`，确认 5200 未被占用 |
| `/app/data` 不可写 | 挂载目录属主不符 | `sudo chown -R 1000:1000 ./data`，或保持默认 root 运行 |
| 导航里还有抖音入口 | 前端缓存了旧 JS | 强刷（Cmd/Ctrl+Shift+R）；确认镜像里 `mp-mode.js` 存在 |
| 点「打开文件夹」报 `xdg-open` 不存在 | 旧版本未做降级 | 升级后会自动返回宿主机路径提示，不再报错 |
| 历史里的路径点不开 | 只拷了 JSON、没拷文章文件 | 把 `articles_full/` 一起拷过来，路径会自动重定位 |
| PDF 导出没有 .pdf | 默认精简镜像不含 Chromium | `.env` 里 `INSTALL_BROWSERS=1` 重建；日志看 `[entrypoint] Chrome:` 是否有输出 |
| 点抖音页面报错 | 公众号版未打包（设计如此） | 切全功能版，或用桌面版 |
| RSS 链接指向内网 IP | 没设 `RSS_BASE_URL`，反代没传 `X-Forwarded-*` | 二选一补上 |
| RSS 条数被截断 | `.env` 里设了 `RSS_MAX_ITEMS` | 改成 `0` 恢复全部，或用 `?limit=` 单次指定 |
| RSS 内容不更新 | 缓存 5 分钟 | 调小 `RSS_CACHE_TTL` |
| 时间差 8 小时 | 时区 | 确认 `TZ=Asia/Shanghai`，重启容器 |

## 十三、这次改了哪些代码

| 文件 | 改动 |
|---|---|
| `app_mp.py` | 新增，公众号版入口（5 个蓝图 + healthz + capabilities + 501 兜底） |
| `requirements-mp.txt` | 新增，6 个依赖 |
| `backend/rss_feed.py` | 新增，RSS 生成与缓存 |
| `backend/articles.py` | `get_rss()` 改为委托调用（-137 行）；open-* 三接口容器下降级；历史路径重定位 |
| `backend/runtime.py` | 新增 `desktop_available()` / `host_path_hint()` |
| `backend/config.py` | 新增 4 个 RSS 配置项；`DOWNLOAD_DIR` 兜底旧机器的下载路径 |
| `backend/downloader.py` | PDF 导出在 root 下补 `--no-sandbox` |
| `frontend/js/mp-mode.js` | 新增，精简模式收窄导航 |
| `frontend/js/router.js` | 增加未启用页面的路由拦截（4 行） |
| `frontend/js/components/history.js` | 打开文件改为新标签页；显示宿主机路径 |
| `frontend/index.html` | 引入 `mp-mode.js` |
| `app.py` | 新增 `/healthz`（两种形态共用探针） |
| `.dockerignore` | 新增，挡住 `data/`、`venv312/` 等 |
| `deploy/docker/` | Dockerfile（公众号版）、Dockerfile.full（全功能版）、compose、entrypoint、nginx、backup.sh |

## 十四、内容过滤（屏蔽图片小节标题）

部分公众号喜欢用图片当小节标题，正文 HTML 里塞满横幅图，RSS 和本地阅读都很碎。

`backend/content_filter.py` 在**正文抓取之后、落盘之前**做过滤，四个产物一起干净：
`.html`、`content.txt`（RSS 正文）、`.md`、PDF。`_raw.html` 和 `data.json` 保留原始正文，方便回头重跑。

### 界面上怎么改

「网址下载」页面下方有一张**内容过滤**卡片：开关、宽高比阈值、最小宽度、图片关键词、文本关键词都能直接改，保存后立即生效（不需要重启，也不需要改 JSON）。

旁边的「应用到已下载文章」会先做一次预演，告诉你扫描多少篇、会改写多少篇，确认后才真正改写（原文备份为 `.html.bak`）。

### 识别规则

配置在 `data/content_filter.json`，改完即生效（按 mtime 缓存，不用重启）：

| 规则 | 作用 | 默认值 |
|---|---|---|
| `remove_selectors` | CSS 选择器精确摘除 | 二维码、赞赏、广告区块 |
| `remove_text_keywords` | 短文本块（≤200 字）命中即删 | 点击上方蓝字 / 扫码关注 等 |
| `remove_image_keywords` | 图片 alt/title/src 命中即删 | 分割线 / 小节标题 / 二维码 |
| `banner_images` | 宽高比 ≤ `max_ratio` 且宽 ≥ `min_width` 的图 | 0.22 / 500px，开启 |
| `remove_empty_blocks` | 过滤后变空的段落一并清掉 | 开启 |
| `enabled` | 总开关 | 开启 |

横幅图那条是核心：微信编辑器会给图片写入 `data-ratio`（高/宽）和 `data-w`，
当小节标题的图通常「很宽很扁」（0.1~0.2），正文配图不会这么极端。实测真实文章里
2 张正文配图全部保留，只清掉了空段落。

### 接口

```bash
# 查看规则
curl http://localhost:5200/api/articles/content-filter

# 改规则（合并写入，只传要改的字段）
curl -X POST http://localhost:5200/api/articles/content-filter \
  -H 'Content-Type: application/json' \
  -d '{"rules":{"banner_images":{"max_ratio":0.18},"remove_image_keywords":["分割线","二维码","小节"]}}'

# 对已下载文章重跑（先 dry-run 看会影响多少篇）
curl -X POST http://localhost:5200/api/articles/content-filter/apply-existing \
  -H 'Content-Type: application/json' -d '{}'

# 确认后真正改写
curl -X POST http://localhost:5200/api/articles/content-filter/apply-existing \
  -H 'Content-Type: application/json' -d '{"dry_run":false}'
```

重跑只动「清洗后」的 `<标题>.html`，改写前备份成 `.html.bak`，并重算 `content.txt`。
`_raw.html` 不动。html 没变化但想重算正文时加 `"force": true`。

RSS 输出缓存 5 分钟（`RSS_CACHE_TTL`），重跑后最多等一个周期就能看到效果。

### 依赖

过滤用 `beautifulsoup4` 解析 HTML（正则改 HTML 太容易出错）。它已加入
`requirements.txt` 和 `requirements-mp.txt`；没装时会打印警告并跳过过滤，不影响抓取。
