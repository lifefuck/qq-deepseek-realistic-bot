#!/usr/bin/env bash
# 备份运行数据。记忆、群配置、账单这些都不进 git，磁盘坏了就真没了。
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${QQBOT_BACKUP_DIR:-$HOME/backups/qq-deepseek-bot}"
STAMP="$(date +%Y%m%d-%H%M)"
mkdir -p "$OUT"

tar --ignore-failed-read -czf "$OUT/qqbot-$STAMP.tar.gz" -C "$DIR" \
  user_memories.json group_configs.json token_usage.json cross_group_topics.json \
  meme-pack/memes_catalog.json

# 只留最近 7 份
ls -1t "$OUT"/qqbot-*.tar.gz 2>/dev/null | tail -n +8 | xargs -r rm -f

echo "$(date '+%F %T') backup ok: $OUT/qqbot-$STAMP.tar.gz"
