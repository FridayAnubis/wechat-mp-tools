# Linux x86-64 / TrueNAS Docker 部署

镜像运行现有网页 UI 与 Flask API，监听容器内 `5200` 端口。内置 Python 3.12、Chromium、中文字体与 FFmpeg；使用 Waitress 单进程、多线程提供 Web 服务，同一进程内运行 RSS 调度器。不需要显示器、桌面环境、宿主机 Python、特权容器或 Docker socket。

## 构建与启动

需要支持 Docker 的 Linux x86-64 主机（TrueNAS SCALE 24.10+ 可使用 Apps 自定义 YAML；TrueNAS CORE 不支持此 Docker 部署）。在仓库根目录执行：

```bash
git switch 'fea(docker)'
docker build --platform linux/amd64 -t wechat-mp-tools:docker .
mkdir -p data
# data 必须允许运行容器的 UID/GID 写入；默认 1000:1000。
docker compose up -d
docker compose ps
docker compose logs --tail=100 -f
```

在浏览器打开 `http://NAS的IP:5200`。宿主机端口可通过 `WEB_PORT=15200 docker compose up -d` 更改；容器内端口始终为 5200。镜像健康检查同时确认 HTTP 服务与 RSS 调度线程存活，不依赖微信服务是否在线或是否登录。

构建时需要连接 Docker Hub、Debian 软件源和 PyPI。受限网络可使用 Docker 标准 `--build-arg HTTP_PROXY --build-arg HTTPS_PROXY --build-arg NO_PROXY` 传递当前代理；不要把代理密码写进 Dockerfile。镜像使用发行版 Chromium，无需额外下载 Playwright 浏览器。

如果构建代理使用组织自签 CA，可额外传入 `--secret id=build_ca,src=/path/to/trusted-ca-bundle.crt`（包含系统根证书与组织 CA 的 PEM bundle）。该文件仅在构建时挂载，不写入最终镜像；TLS 与软件包签名验证保持启用。普通网络无需此参数。

## GitHub Actions 构建与发布

仓库中的 [docker.yml](../.github/workflows/docker.yml) 参考 [we-feed 的发布工作流](https://github.com/FridayAnubis/we-feed/blob/main/.github/workflows/docker-publish.yaml)，通过 GitHub Actions 构建并发布到 **GitHub Container Registry（GHCR）**。镜像地址自动取当前仓库名称并转为小写，本仓库为 `ghcr.io/fridayanubis/wechat-mp-tools`。

| 触发方式 | 发布标签 |
| --- | --- |
| 推送 `main` | `latest`、`main`、`sha-完整提交SHA` |
| 推送 `fea(docker)` | `docker`、规范化的分支名、`sha-完整提交SHA` |
| 推送版本标签，例如 `v2.2.4` | `2.2.4`、`sha-完整提交SHA`（不修改 `latest`） |
| 向 `main` 提交 PR | 仅构建和测试，不登录或推送 GHCR |
| Actions 中手动运行 | 根据所选分支/标签发布，并可生成离线镜像包 |

工作流只构建 `linux/amd64`，使用 Buildx 缓存加速。镜像先加载到 runner，执行网页、文章下载、PDF、RSS 与容器重建持久化测试，全部通过后推送**同一份镜像**，不进行第二次构建。

使用方法：

1. 将本分支代码和 workflow 提交、推送到 GitHub，自动触发构建。无需新建 PAT 或额外 Secret，发布使用内置 `GITHUB_TOKEN` 和 workflow 的 `packages: write` 权限。
2. 在 Actions 中查看 **Build and publish Linux amd64 Docker image**。如需手动运行，默认分支需要已包含该 workflow，然后选择 **Run workflow** 与目标分支。
3. 首次发布成功后，在 GitHub 的 Packages 页面检查包的可见性。希望 NAS 匿名拉取时，将包设置为 Public；Private 包需要在 NAS 上配置具有 `read:packages` 权限的凭据。组织策略或已有同名包可能要求在包设置中授予当前仓库 Actions 写入权限。
4. TrueNAS 部署本功能分支时使用 `ghcr.io/fridayanubis/wechat-mp-tools:docker`；合并并成功构建 `main` 后可使用 `:latest`。移除下方示例中的 `pull_policy: never`，按需拉取新镜像并重建应用。

```bash
# 对应分支成功发布后：
docker pull ghcr.io/fridayanubis/wechat-mp-tools:docker
```

手动运行时默认勾选 `export_archive`，完成后可在该次运行的 Artifacts 下载 `wechat-mp-tools-linux-amd64`，包含 `.tar.gz` 镜像和 SHA-256 校验文件，保留 7 天。离线包导入后的镜像名仍是 `wechat-mp-tools:docker`。取消勾选仅关闭离线包导出，GHCR 发布仍执行。工作流文件写入仓库不代表镜像已发布，首次使用需等待 GitHub 上的运行成功。

## TrueNAS Apps 自定义 YAML

先创建 Dataset，例如 `/mnt/tank/apps/wechat-mp-tools`，在 TrueNAS 权限设置中授予容器 UID/GID **1000:1000** 读、写和目录遍历权限。若已有指定服务用户，可以改用它的数字 UID/GID；不要让容器启动脚本递归更改整个 Dataset 的所有者。

镜像需存在于 TrueNAS 的 Docker 引擎中，可在构建机器导出后传到 NAS：

```bash
docker save wechat-mp-tools:docker | gzip > wechat-mp-tools-linux-amd64.tar.gz
# 将压缩包传到 NAS 后，在其 shell 中执行：
gzip -dc wechat-mp-tools-linux-amd64.tar.gz | docker load
```

也可以推送到自己的镜像仓库，并将下方 `image` 改成相应地址。不要填写一个尚未发布的 GHCR 镜像地址。

在 TrueNAS 的 Apps → 自定义应用 → 通过 YAML 安装中填写（菜单名称可能随版本不同）：

```yaml
services:
  wechat-mp-tools:
    image: wechat-mp-tools:docker
    pull_policy: never # 使用 docker load 的本地镜像；使用远程仓库时移除此行
    platform: linux/amd64
    restart: unless-stopped
    user: "1000:1000"
    ports:
      - "5200:5200"
    environment:
      TZ: Asia/Shanghai
      # 反向代理时设置为阅读器可访问的完整来源地址，不带路径：
      # WECHAT_MP_PUBLIC_URL: https://wechat.example.com
    volumes:
      - /mnt/tank/apps/wechat-mp-tools:/app/data
    shm_size: 256mb
    stop_grace_period: 60s
```

建议起步分配 2 个 CPU、2 GB 内存，批量 PDF/视频转码时增加内存。实例数量保持 **1**，不要启动多个副本共用数据目录；现有任务队列和 JSON 存储不是分布式架构。端口占用时只改映射左侧的宿主机端口。

## 公众号抓取与 RSS

1. 打开网页的微信读书账号池，通过页面二维码使用微信扫码授权。登录信息保存在数据卷中。无需在容器里安装 PC 微信。
2. 添加公众号：粘贴公众号文章链接，或使用已登录账号搜索/同步公众号。进入公众号详情页执行文章获取、单篇或批量下载。
3. 点击公众号的 **RSS**，启用自动订阅并设置抓取间隔。调度器在容器运行期间后台抓取，关闭浏览器不影响执行。首次自动抓取按订阅时间间隔调度，不保证启用后立即抓取；可先手动下载以验证 Feed。
4. 将页面提供的 RSS 链接加入阅读器：
   - 全部公众号：`http://NAS的IP:5200/api/articles/rss`
   - 指定公众号：`http://NAS的IP:5200/api/articles/rss/公众号名称`（名称需 URL 编码，建议直接复制 UI 中的链接）
5. 设置中的抓取时段按 `TZ` 时区运行。账号过期或微信要求验证时，需要回到网页重新授权或完成验证；这些外部限制不能通过 Docker 消除。

阅读器与浏览器都必须能够访问 NAS 地址。Feed 合并手动下载和 RSS 自动抓取记录，优先链接到本机离线文章。通过 HTTPS 反向代理部署时设置 `WECHAT_MP_PUBLIC_URL=https://wechat.example.com`，避免 Feed 产生内部 HTTP 地址。仅支持根路径部署，不支持 `/wechat/` 子路径。该变量不改变监听端口，也不负责签发证书。

本地开发的必要外网目标主要包括 `i.weread.qq.com`、`weread.qq.com`、`open.weixin.qq.com`、`long.open.weixin.qq.com`、`mp.weixin.qq.com`，以及图片/音视频 CDN（例如 `mmbiz.qpic.cn`、`mpvideo.qpic.cn`）；实际媒体可能使用其他腾讯 CDN。上传网关或其他平台的访问按启用功能配置。

## 持久化与升级

`/app/data` 包含账号凭据、设置、订阅计划、RSS 缓存、下载历史、浏览器 Profile 和默认下载的文章/媒体。整个目录都需要保留并限制访问权限。默认下载目录为 `/app/data/articles_full`；网页中更改下载路径时必须使用**容器内**路径，并为其添加额外卷映射，否则重建容器后文件会丢失。

升级前停止容器并备份 Dataset（TrueNAS 快照也可），构建/导入新镜像后重建应用，继续挂载同一 Dataset。不要使用会删除数据卷的命令。现有桌面数据可以复制到 Dataset；旧设置和历史记录可能包含 Windows/macOS 绝对路径，应在设置中改为容器路径，旧历史的本地文章链接可能需要重新下载修复。新安装后再在容器中下载最稳妥。

## 功能边界

- 公众号网页扫码、列表与文章下载、HTML/Markdown/PDF、RSS 定时抓取和 Feed 是此镜像的主要工作流。
- 其他平台解析、已配置凭据的下载及 FFmpeg 转码保留原有实现。需要打开**服务器上的交互浏览器**登录的功能（如部分抖音/小红书登录）在无桌面容器中无法显示登录窗口，应使用项目已有的手动凭据方式。
- 操作宿主 PC 微信窗口、系统代理/证书安装、“打开本地文件夹”、桌面自动更新，以及依赖 GPU/额外模型的字幕移除不属于此 Web 容器的支持范围。升级使用新镜像。
- 当前项目没有 Web 管理员登录隔离；页面可操作账号、下载和文件。仅向受信任局域网开放；需要外部访问时用 VPN 或带认证的反向代理。RSS 阅读器需采用相应的认证访问方式。Chromium 在容器模式使用 `--no-sandbox`，容器以非 root 用户运行，勿使用 privileged 模式。

## 验证与排错

```bash
# 构建后执行无真实账号的端到端检查，自动创建并清理独立容器及数据卷：
bash scripts/test_docker.sh wechat-mp-tools:docker

# 检查正式实例：
docker compose exec wechat-mp-tools python docker/healthcheck.py
docker compose logs --tail=200
```

端到端检查只模拟上游公众号文章列表，实际执行测试文章 HTTP 下载、Chromium 页面渲染、持久化 Profile、HTML/Markdown/PDF 导出、RSS 去重与离线链接读取，再销毁并重建容器验证订阅和文件持久化。它不验证真实微信登录或接口授权。

启动出现 `Cannot write /app/data`：修正 Dataset ACL 或容器 `user` 的 UID/GID。RSS 空白：确认存在下载成功的文章，检查订阅开关、下次抓取时间、时区及账号状态。PDF 失败：查看服务日志中的浏览器错误，确认内存充足，使用本项目构建的 Chromium 镜像。不要将宿主机浏览器配置目录或开发用虚拟环境挂入镜像。
