# 历史回溯实施与验证记录

日期：2026-10-21。分支：`feat(gethistory)`。
基线提交：`35d3ef3c6359676f7fd81904d7ad8871038f2f1d`。

## 当前完成范围

目前仅完成 G0 的验证准备，尚未完成整份历史回溯任务书。

- `backend/history_paging.py` 提供独立的章节分页结果，区分 `ok`、`end`、`temporary_error`、`auth_error`、`blocked`。
- 历史路径只请求 `mp/chapters`，不合并 `mp/cover`，不降级到 `article/list`。游标按原始章节数量推进，重复标题不会合并，空列表、格式异常及失败不会被当作末页。
- `end` 仅表示接口明确返回的非空末页，不代表已获得公众号全部历史。缺失 `hasMore` 时保持未知。
- 账号池增加显式的 `strict` 借用选项；历史验证不借出冷却账号，不自动续期或激活会话。原调用默认行为保持兼容。
- `scripts/probe_history_pagination.py` 串行探测第一页与下一页，可追加深层偏移、重复两轮。遇错误即停止，报告不保存凭据、原始正文、文章链接或完整文章 ID。
- 原有文章列表 API、RSS 最新同步、下载器、前端和部署入口尚未更改。没有加入新的鉴权系统、外部服务或运行时依赖。

## G0 状态：缺少真实验证环境

任务书第 2 节明确将真实历史分页列为阻塞项，要求在大规模实现前验证至少一个已授权公众号能返回第一页以外的真实旧文章。

本地 `data/` 仅有 `proxy_config.json`，没有 `account_pool.json`、登录会话或收藏公众号。本机也没有 Docker CLI，因此无法验证目标容器。预检报告见 [history_pagination_probe.json](history_pagination_probe.json)：`g0_passed=false`、`missing_account_pool`，实际上游请求数为 **0**。

这不是上游接口失败或不支持历史的结论。需要已登录环境的数据目录和一个有较多历史的已授权公众号标识，才能继续验证。不要将模拟响应测试当作上游能力证明。

## 真实环境验证方法

在使用同一客户端配置、已有有效 `account_pool.json` 的环境中运行。先备份目标数据目录，在原采集任务空闲时执行；脚本会更新账号池的使用和冷却状态。凭据直接从文件读取，不需要复制到命令行。

```bash
python scripts/probe_history_pagination.py \
  --data-dir /path/to/data \
  --fakeid MP_WXS_example \
  --count 10 --interval 10 --rounds 2 \
  --offsets 50 200 500 \
  --output /path/to/history_pagination_probe.json
```

将路径和公众号标识换成实际值。默认探测偏移 0 和按原始返回条数计算出的下一页；`--offsets` 是额外探测点。`--count` 支持 5–30，可在前一轮成功后另用不同值复核。请求间隔至少 10 秒，并服从配置中更长的 `request_delay`。

报告保留 HTTP 状态、数字错误码、原始条数、`hasMore`、时间范围、文章标识摘要、耗时和游标，可对比两轮中的重复及漂移。G0 通过需要发现标识不同且发布时间早于第一页的文章；若时间字段缺失，脚本不会自动判定通过，应根据实际响应格式继续诊断。

HTTP 401、403、429、499、`-2041`、验证码、超时、格式异常或空页均停止后续探测。退出码 0 表示 G0 有证据且本轮未遇错误；退出码 2 表示条件不足或探测中断。即使 G0 通过，也不能据此承诺上游历史总量或 500 条之后始终可读。

## 已运行测试

Python 3.11 隔离环境：`/tmp/wechat-mp-gethistory-venv`。

- 新增 36 项测试通过：异常分类、原始偏移、同名文章、URL 归一化、严格冷却、禁用续期、旧账号池行为、原文章列表 API 契约、脱敏报告及 G0 证据判断。
- 全量 `python -m pytest -q`：127 通过、5 失败。
- 5 项失败已在基线提交的独立副本上复现：README 缺少 Intel / ARM64 架构选择说明的 3 项断言；本机无 Chrome / Edge 导致 PDF 导出的 2 项断言。本次未修改这些无关内容。
- `git diff --check` 通过。

实施前本地数据已备份到 `/tmp/wechat-mp-gethistory-backup-35d3ef3/data`。此临时副本不是部署环境的长期备份。

## 待 G0 通过后实施

无固定篇数上限的 SQLite 索引、旧数据迁移、统一请求调度与历史写入协调、可恢复 Worker、管理 API、“获取历史”按钮、RSS 回溯隔离、下载历史分页，以及 Docker 持久化验收均尚未实施。当前 200 篇 RSS 缓存截断仍存在，不能将本分支称为已完成无上限归档。

本阶段尚未迁移 JSON 或文章文件，也没有新建 `backfill.db`；回退代码即可恢复基线功能。数据库迁移与回滚方案需要随后续实际实现补充。
