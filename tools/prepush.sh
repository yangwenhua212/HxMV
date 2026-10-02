#!/usr/bin/env bash
# 推送到公开仓库前的闸门：不过就推不出去。
#
# 为什么要有它：这个仓库是**开源产品**，不是私人库。我自己就犯过一次——扫描报出注释里的
# 个人指代，但命令没拿它拦下推送，结果进了公开历史，只能重写历史强推。所以：
# 扫描结果必须是**闸门**（exit 1 就推不出去），而不是一句提示。
#
# 私人词（昵称、真名之类）**不进仓库**：写在 .private-words（已 gitignore，一行一个词），
# 仓库里只留通用规则（本机绝对路径、作者实例域名、作者邮箱前缀）。
#
# 用法：
#   bash tools/prepush.sh              # 检查工作区 + 准备推送的提交（相对 origin/main）
#   bash tools/prepush.sh origin/main  # 指定比较基线
set -uo pipefail
cd "$(dirname "$0")/.."

BASE="${1:-origin/main}"
BAD='/home/admin|/Users/|eraherm\.com|yangwenhua212@'
if [ -f .private-words ]; then
  extra="$(grep -vE '^\s*(#|$)' .private-words | paste -sd'|' -)"
  [ -n "$extra" ] && BAD="$BAD|$extra"
  echo "（已加载 .private-words：$(grep -cvE '^\s*(#|$)' .private-words) 个私人词）"
fi
fail=0

echo
echo "① 密钥扫描"
python3 tools/check_secrets.py || fail=1

echo
echo "② 工作区里的个人指代 / 实例地址 / 本机路径"
# 排除 LICENSE（版权行要留）、本脚本自己（必须写下规则）、.private-words（词表本身，且已 gitignore）
if grep -rInE "$BAD" --exclude-dir=.git . 2>/dev/null \
   | grep -vE '^\./LICENSE|^\./tools/prepush\.sh|^\./\.private-words'; then
  echo "   ✗ 上面这些要清掉（测试里也别写死本机绝对路径，用 __file__ 相对定位）"
  fail=1
else
  echo "   ✓ 干净"
fi

echo
echo "③ 准备推送的提交：信息 + 改动"
if git rev-parse --verify -q "$BASE" >/dev/null; then
  if git log --format='%h %s%n%b' "$BASE"..HEAD | grep -nE "$BAD"; then
    echo "   ✗ 提交信息里有敏感词 → 先 rebase 改信息"
    fail=1
  else
    echo "   ✓ 提交信息干净"
  fi
  # 本脚本自身会包含规则字符串，比对改动时排除它
  if git log -p "$BASE"..HEAD -- . ':(exclude)tools/prepush.sh' | grep -nE "^\+.*($BAD)"; then
    echo "   ✗ 改动内容里有敏感词 → 改掉再提交"
    fail=1
  else
    echo "   ✓ 改动内容干净"
  fi
else
  echo "   - 找不到基线 $BASE，跳过本节"
fi

echo
if [ "$fail" != 0 ]; then
  echo "❌ 闸门未通过：这次别推。"
  exit 1
fi
echo "✅ 闸门通过，可以推送。"
