#!/usr/bin/env bash
set -euo pipefail

: "${UPSTREAM_REPOSITORY:=x554960766/wechat-mp-tools}"

FORK_FILES=(
  ".github/scripts/sync-upstream.sh"
  ".github/workflows/sync-upstream-docker.yml"
  "deploy/docker/AUTOMATION.md"
)

if [[ "$(git branch --show-current)" != "main" ]]; then
  echo "::error::Must run on main."
  exit 1
fi

if [[ -n "$(git status --porcelain)" ]]; then
  echo "::error::Working tree is not clean."
  exit 1
fi

# 记录本次检出的 main HEAD，供推送时校验远端是否发生并发更新
old_head=$(git rev-parse HEAD)

# 获取上游最新 main
git fetch --no-tags \
  "https://github.com/${UPSTREAM_REPOSITORY}.git" \
  refs/heads/main

upstream_sha=$(git rev-parse FETCH_HEAD)

if git merge-base --is-ancestor "$upstream_sha" HEAD &&
   [[ "$(git rev-list --count "${upstream_sha}..HEAD")" -eq 1 ]]; then
  echo "Already synced: exactly 1 ahead commit."
  exit 0
fi

# 预先计算合并结果，不修改工作区
if ! merged_tree=$(git merge-tree --write-tree \
  "$old_head" "$upstream_sha"); then
  echo "::error::Upstream merge conflict. Aborting."
  exit 1
fi

# 检查合并结果中是否存在其他 Fork 独有修改
while IFS= read -r -d '' path; do
  case "$path" in
    ".github/scripts/sync-upstream.sh"|\
    ".github/workflows/sync-upstream-docker.yml"|\
    "deploy/docker/AUTOMATION.md")
      ;;
    *)
      echo "::error::Unexpected fork change: $path"
      echo "Refusing to discard custom changes."
      exit 1
      ;;
  esac
done < <(git diff --name-only -z \
  "$upstream_sha" "$merged_tree")

git config user.name "github-actions[bot]"
git config user.email \
  "41898282+github-actions[bot]@users.noreply.github.com"

# 重建历史：以最新上游作为唯一基础
git reset --hard "$upstream_sha"

git restore --source="$merged_tree" \
  --staged --worktree -- "${FORK_FILES[@]}"

if ! git diff --cached --quiet; then
  git commit -m "ci: sync upstream and publish Docker image [skip ci]"
fi

# 安全强制推送：仅当远端 HEAD 未变化时才覆盖
git push \
  --force-with-lease="refs/heads/main:$old_head" \
  origin HEAD:refs/heads/main

echo "Successfully synced with a single ahead commit."
