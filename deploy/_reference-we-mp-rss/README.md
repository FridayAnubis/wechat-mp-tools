# WeRSS 部署 + RSS 优化方案

基于 `we-mp-rss` v1.5.2 的实际实现整理，两套东西：

- `docker/` —— 可直接 `docker compose up` 的生产编排（MySQL + Redis + 可选代理出口），以及一个单机 SQLite 版
- `rss-opt/` —— 订阅侧的代码级优化，含可替换源码、上线脚本、验证脚本

## 一、交付物

```
deploy/
├── README.md                      # 本文件
├── docker/
│   ├── docker-compose.yaml        # 生产：app + MySQL + Redis + 可选 singbox
│   ├── docker-compose.lite.yaml   # 单机：只有 app，数据全在 ./data
│   ├── .env.example               # 全部环境变量，含注释
│   ├── config.yaml                # 挂载进容器的完整配置（已补 rss.max_items 等新键）
│   ├── nginx/we-rss.conf          # 反代示例（含 WebSocket 与 gzip）
│   ├── singbox/run-singbox.sh     # 代理出口启动脚本
│   └── scripts/backup.sh          # 数据库 + data 目录备份
└── rss-opt/
    ├── files/apis/rss.py          # 优化后的订阅接口
    ├── files/core/rss.py          # 优化后的 RSS 生成与缓存
    ├── apply.sh                   # 覆盖源码（带备份与语法检查）
    ├── verify.sh                  # 线上验证 ETag / 304 / XML 合法性
    └── README.md                  # 优化细节与兼容性说明
```

## 二、5 分钟跑起来

```bash
cd deploy/docker
cp .env.example .env

# 必改 4 项：RSS_BASE_URL / PASSWORD / MYSQL_ROOT_PASSWORD / MYSQL_PASSWORD
# SECRET_KEY 用 openssl rand -hex 32 生成
vi .env

mkdir -p data
docker compose up -d
docker compose logs -f app
```

看到 `启动服务器` 后访问 `http://<ip>:8001`，用 `.env` 里的 `USERNAME` / `PASSWORD` 登录，进「公众号管理」扫码授权，再「添加订阅」。

单机版换成：

```bash
docker compose -f docker-compose.lite.yaml up -d
```

## 三、两种形态怎么选

| | 生产版（MySQL） | 单机版（SQLite） |
|---|---|---|
| 依赖 | MySQL 8.3 + Redis 7 | 无 |
| 内存 | 约 3–4 GB（app 2G + db 1G） | 约 2 GB |
| 并发写入 | 稳 | 采集与手动操作撞车时可能 `database is locked` |
| 备份 | `mysqldump` + `data` | 直接打包 `data` |
| 适用 | 长期跑、订阅多、多人用 | NAS、试用、单账号 |

两个版本都能再叠加 `--profile proxy` 启用 singbox 出口。**微信对机房 IP 有风控，云服务器上大概率需要开。**

## 四、四个必须知道的约束

**1. `config.yaml` 是挂载进去的，不是镜像里的那份。**
镜像内的 `config.yaml` 就是 `config.example.yaml`，全是 `${VAR:-default}` 占位符，运行时由 `core/config.py` 替换。要改 `rss.cache_ttl` 这类新增键，只能挂载自己的 `config.yaml`（已放在 `docker/config.yaml`）。注意：Web UI 里改配置会整份 `yaml.dump` 写回这个文件，**占位符会保留，但注释和排版会丢**，改完建议留一份原始副本。

**2. 只有 `/app/data` 需要持久化。**
里面是 SQLite 库、RSS 输出缓存、正文缓存、公众号头像、`lock.lock`。扫码二维码 `static/wx_qrcode.png` 不在 data 里，**重启容器要重新扫码**。

**3. `THREADS` 必须保持 1。**
登录态是进程内的 Playwright 单例，加文件锁 `data/lock.lock`。多 worker 会抢锁，表现为随机「请先扫码登录」。

**4. 反向代理后必须设 `RSS_BASE_URL`。**
不设会回退到 `request.base_url`，订阅里全是 `http://容器IP:8001/...`，阅读器在外部打不开。compose 里已经用 `${RSS_BASE_URL:?...}` 强制要求填写，没填直接起不来。

## 五、反向代理

`nginx/we-rss.conf` 已包含三个容易漏的点：

- `X-Forwarded-Proto`：不传会让 `request.base_url` 算成 http，RSS 链接协议错
- `Upgrade` / `Connection`：Web UI 的扫码登录状态走 WebSocket，不传会一直显示「等待扫码」
- `gzip_types application/xml`：RSS 是纯文本，压缩率很高

启用后把 `WERSS_BIND` 改成 `127.0.0.1`，别让 8001 直接暴露公网。

## 六、备份与恢复

```bash
./scripts/backup.sh              # MySQL 模式
MODE=sqlite ./scripts/backup.sh  # 单机版
KEEP_DAYS=0 ./scripts/backup.sh  # 不自动清理
```

产物在 `backups/we-rss-<时间戳>/`：`db.sql.gz` + `data.tar.gz` + `env.bak`（权限 600）+ `meta.txt`。脚本只写和清理 `backups/` 内部。

恢复：

```bash
set -a; . ./.env; set +a          # 让下面的 $MYSQL_* 生效

docker compose down

# 数据库
docker compose up -d db
sleep 10
gunzip -c backups/we-rss-xxx/db.sql.gz | \
  docker compose exec -T db mysql -uroot -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE"

# 文件
tar -xzf backups/we-rss-xxx/data.tar.gz -C .

docker compose up -d
```

单机版跳过数据库那步，`data.tar.gz` 里已经含 `we_mp_rss.db`。

## 七、升级

```bash
./scripts/backup.sh
docker compose pull app
docker compose up -d
```

关于版本锁定，有个现实问题：**ghcr 上只有 `latest` / `1.3.8` / `1.4.5` / `beta` 四个 tag，没有 1.5.x**。所以：

- 用 `latest` 等于每次 `pull` 都可能跳到新版本
- 想真正锁版本，只能自己 `docker build` 后打 tag（见 `rss-opt/README.md` 的 B 方案）
- 上游 `-init True` 每次启动都会跑 `data_sync.py` 同步表结构，**跨大版本升级前务必先备份**

## 八、排障

| 现象 | 原因 | 处理 |
|---|---|---|
| 页面 404 / 空白 | 前端静态资源缺失 | 确认镜像内 `static/index.html` 存在；自建镜像别漏 `COPY` |
| 扫码后仍提示未登录 | `data` 未持久化，或 `THREADS>1` 抢锁 | 检查挂载与 `THREADS=1` |
| 采集报「当前环境异常」 | 出口 IP 被微信风控 | `PROXY_ENABLED=True` + `--profile proxy` |
| 采集报 `ret=200013` | 频率控制 | 调大 `SPAN_INTERVAL`，调小 `MAX_PAGE` |
| 订阅链接指向内网 IP | `RSS_BASE_URL` 未设或带尾斜杠 | 设为公网地址，结尾不带 `/` |
| 文章时间差 8 小时 | `TZ` 没生效 | 确认 `TZ=Asia/Shanghai`，重启容器 |
| 订阅长期不更新 | 定时任务没开 / 登录态失效 | 日志出现「未开启定时任务」说明 `ENABLE_JOB` 或 `-job True` 缺一 |
| 内存持续上涨 | Playwright 浏览器实例未释放 | 限制 `APP_MEMORY_LIMIT`，或每天定时重启 |
| 数据库连不上 | 密码含 `@ : /` 破坏连接串 | 密码只用字母数字和 `_ -` |
| healthcheck 一直 unhealthy | 首次启动建表慢 | 已给 120s `start_period`；仍失败看 `docker compose logs app` |

## 九、和 RSS 优化的关系

部署和优化是解耦的：先按上面把服务跑起来，再决定要不要打 `rss-opt` 补丁。

补丁解决的是「读接口每次轮询都重建 + 缓存不失效 + 失败直接 500」这三件事，改完后的效果：

- 阅读器轮询命中 304，几乎不消耗 CPU
- 采集失败时继续吐旧缓存，而不是整站订阅变空
- 修掉容器 `LANG=zh_CN` 时 `pubDate` 变中文导致客户端拒收的问题

细节和上线步骤见 `rss-opt/README.md`。
