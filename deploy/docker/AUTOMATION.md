# 定时同步上游并发布 Docker Hub 轻量镜像

工作流：`.github/workflows/sync-upstream-docker.yml`，Actions 中名称为 **Sync upstream and publish lite image**。

## 首次配置

1. 将工作流、`.github/scripts/sync-upstream.sh` 及本文档提交到本仓库 `main`，并确保 `main` 是默认分支。在 fork 的 Actions 页面启用 GitHub Actions（fork 的定时任务默认不运行）。
2. 在 Docker Hub 创建镜像仓库，默认名为 `<Docker Hub 用户名>/wechat-mp-tools`。
3. 在 GitHub 仓库 **Settings → Secrets and variables → Actions → Secrets** 配置：

   | Secret | 内容 |
   | --- | --- |
   | `DOCKERHUB_USERNAME` | Docker Hub 登录用户名 |
   | `DOCKERHUB_TOKEN` | 对目标镜像仓库有写入权限的 Docker Hub access token |
   | `UPSTREAM_SYNC_TOKEN`（可选） | 仅默认 `GITHUB_TOKEN` 无法满足同步权限时使用，见下文 |

4. 如需自定义，在同一页面的 **Variables** 配置：

   | Variable | 默认值 | 说明 |
   | --- | --- | --- |
   | `DOCKERHUB_REPOSITORY` | `<DOCKERHUB_USERNAME>/wechat-mp-tools` | 完整小写镜像名，可指定组织命名空间；不含 registry 和标签 |
   | `UPSTREAM_REPOSITORY` | `x554960766/wechat-mp-tools` | 上游 `owner/repo`，固定同步其 `main` |

5. 确保仓库 Actions 策略允许该工作流申请 `contents: write`，且分支规则允许自动化账号向 `main` **强制推送**。本工作流会重写 fork 的自定义提交，使用 `--force-with-lease` 推送；仅允许普通推送不足以完成同步。若 `main` 受分支保护或 ruleset 限制，需要允许自动化账号执行强制推送或配置适用的规则绕过权限。首次配置后，在 Actions 中选择该工作流，点击 **Run workflow**，分支选择 `main`，验证同步和发布。

默认用仓库自动提供的 `GITHUB_TOKEN` 推送，无需额外 GitHub Secret。若上游更新了 `.github/workflows/` 内文件，默认令牌可能无权推送这些变更；此时可设置 `UPSTREAM_SYNC_TOKEN`，使用对本仓库具有 **Contents: Read and write** 和 **Workflows: Read and write** 权限的 fine-grained PAT。使用 classic PAT 时需要目标仓库访问权限及 `workflow` scope。令牌所属账号还必须满足分支规则；禁止强推、强制 PR、必需检查或组织策略都可能阻止推送。使用 PAT 本身不会绕过这些限制，需配置允许的规则或适用的绕过权限，否则同步会明确失败。

## 运行方式

- **每 6 小时**检查上游 `main`（cron 为 `0 */6 * * *`，UTC 每天 00:00、06:00、12:00、18:00，即北京时间每天 **02:00、08:00、14:00、20:00**；实际开始时间可能延迟）。
- 定时和手动运行先拉取上游，并通过 `git merge-tree --write-tree` 预计算合并结果。随后以最新上游提交为基础，将允许保留的自定义文件差异压成一条提交，再使用 `--force-with-lease` 更新本仓库 `main`，不会累计同步产生的 merge commit。
- 允许保留的 fork 自定义文件为 `.github/scripts/sync-upstream.sh`、`.github/workflows/sync-upstream-docker.yml`、`deploy/docker/AUTOMATION.md`。如果合并结果相对上游还存在其他文件的差异，脚本会报 `Unexpected fork change` 并停止，不会丢弃这些改动。需要调整保留范围时，必须同时更新脚本的 `FORK_FILES` 数组与路径检查白名单。
- 最新上游提交已经包含在本仓库历史中，且本仓库恰好只比上游多一条提交时，直接跳过历史重建。上游有更新时会重建自定义提交，**该提交的 SHA 会改变**；“只保留一条”指额外提交的数量，不是保留最初上传 Actions 的提交 SHA。若自定义文件已与上游完全一致，则不创建额外提交。
- 脚本要求在 `main` 上运行且工作区干净。合并冲突、额外的 fork 文件改动、分支规则拒绝或远端并发更新时停止，后续镜像发布任务不会运行。处理对应问题后再次运行即可。`--force-with-lease` 校验远端 `main` 仍等于本次检出的提交，避免覆盖本次运行期间其他人推送的提交。
- 人工向本仓库 `main` 推送也会发布镜像，该次运行不主动合并上游。
- 同步与发布在同一工作流中顺序执行，构建检出的 SHA 是同步后实际提交；不依赖自动 push 再触发另一个工作流。GitHub 默认令牌的 push 不会触发普通 push 工作流；重建的自定义提交带有 `[skip ci]`，避免可选 PAT 导致重复触发 push 工作流，本次同步工作流仍会继续发布镜像。
- 每次检查已发布的完整 SHA 标签；已存在则跳过构建。同步成功但镜像发布失败时，下次定时检查会自动重试；**Run workflow** 则始终重建，可用于凭据修复后重试或刷新基础镜像。
- 同一工作流串行运行，避免多个任务同时重写历史或覆盖镜像标签。GitHub 并发队列可能合并等待中的运行，下次检查仍会处理最新 `main`。

只使用 `deploy/docker/Dockerfile` 并显式设置 `INSTALL_BROWSERS=0`，发布公众号 + RSS 轻量版，不包含 Chromium、Playwright 和 ffmpeg；PDF 导出与浏览器风控兜底不可用。镜像支持 `linux/amd64`、`linux/arm64`，发布三个标签：

```text
<namespace>/wechat-mp-tools:latest
<namespace>/wechat-mp-tools:lite
<namespace>/wechat-mp-tools:sha-<完整的本仓库提交 SHA>
```

无需更新时不会重新构建；如长期没有代码更新，需要刷新基础镜像或依赖，请手动运行。公开仓库连续 60 天没有活动时 GitHub 可能自动禁用定时工作流，需在 Actions 中重新启用。

## 使用预构建镜像

在 `deploy/docker/.env` 设置（替换为实际镜像名）：

```dotenv
IMAGE=your-dockerhub-user/wechat-mp-tools:latest
```

在 `deploy/docker` 目录执行：

```bash
docker compose pull app
docker compose up -d --no-build app
```

需要固定版本或回滚时，将 `IMAGE` 改为对应的 `sha-<完整 SHA>` 标签，再执行上述命令。此工作流只发布镜像，服务器需要自行拉取并重建容器。

参考：[GitHub 工作流触发与定时规则](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows)、[GitHub 令牌触发限制](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow)、[受保护分支与强制推送规则](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches)、[Docker Hub 自动构建及发布示例](https://github.com/docker/build-push-action)。
