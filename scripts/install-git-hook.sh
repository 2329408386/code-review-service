#!/usr/bin/env bash
# 为 Git 仓库安装 post-commit 钩子：每次提交后，把本次提交（父提交..本提交）交给检视平台检视，
# 结果在 Web 控制台查看，不会回写 CodeHub。钩子在后台执行，服务未启动或请求失败都不影响提交。
#
# 用法：scripts/install-git-hook.sh [仓库路径，默认当前仓库]
# 平台地址默认 http://127.0.0.1:8083，可在提交时用环境变量 CODE_REVIEW_URL 覆盖。
set -euo pipefail
repo="$(git -C "${1:-.}" rev-parse --show-toplevel)"
hooks_dir="$(git -C "$repo" rev-parse --absolute-git-dir)/hooks"
hook="$hooks_dir/post-commit"
marker="# xkl-code-review-hook"

if [ -e "$hook" ] && ! grep -q "$marker" "$hook"; then
  echo "已存在其它 post-commit 钩子：$hook，请手动合并以下内容：" >&2
  echo "  curl -s -m 5 -o /dev/null -X POST \"\${CODE_REVIEW_URL:-http://127.0.0.1:8083}/api/local-reviews\" -H 'Content-Type: application/json' -d \"{\\\"path\\\": \\\"\$(git rev-parse --show-toplevel)\\\", \\\"sha\\\": \\\"\$(git rev-parse HEAD)\\\"}\" &" >&2
  exit 1
fi

mkdir -p "$hooks_dir"
cat > "$hook" <<'HOOK'
#!/bin/sh
# xkl-code-review-hook：提交后把本次变更交给检视平台（后台执行，失败不影响提交）
url="${CODE_REVIEW_URL:-http://127.0.0.1:8083}"
body=$(printf '{"path": "%s", "sha": "%s"}' "$(git rev-parse --show-toplevel)" "$(git rev-parse HEAD)")
curl -s -m 5 -o /dev/null -X POST "$url/api/local-reviews" -H 'Content-Type: application/json' -d "$body" >/dev/null 2>&1 &
exit 0
HOOK
chmod +x "$hook"
echo "已安装：$hook（平台地址 \${CODE_REVIEW_URL:-http://127.0.0.1:8083}）"
