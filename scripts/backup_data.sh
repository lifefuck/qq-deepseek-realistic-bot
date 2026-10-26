#!/usr/bin/env bash
# 备份运行数据。记忆、群配置、账单这些都不进 git，磁盘坏了就真没了。
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${QQBOT_BACKUP_DIR:-$HOME/backups/qq-deepseek-bot}"
STAMP="$(date +%Y%m%d-%H%M)"
mkdir -p "$OUT"

TARGET="$OUT/qqbot-$STAMP.tar.gz"
TMP="$TARGET.part"

# 先写 .part，确认压缩包能完整读出来再改名。
# 中途失败时留下一份坏包，会按时间顺序把昨天的好备份挤掉，这才是最亏的。
trap 'rm -f "$TMP"' EXIT

tar --ignore-failed-read -czf "$TMP" -C "$DIR" \
  user_memories.json group_configs.json token_usage.json cross_group_topics.json \
  meme-pack/memes_catalog.json

gzip -t "$TMP"
if ! tar -tzf "$TMP" >/dev/null 2>&1; then
  echo "$(date '+%F %T') backup 校验失败，保留上一份，不覆盖" >&2
  exit 1
fi

mv -f "$TMP" "$TARGET"
trap - EXIT

# 只留最近 7 份
ls -1t "$OUT"/qqbot-*.tar.gz 2>/dev/null | tail -n +8 | xargs -r rm -f

echo "$(date '+%F %T') backup ok: $TARGET"
