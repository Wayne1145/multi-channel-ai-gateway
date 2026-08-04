#!/usr/bin/env bash
set -euo pipefail

# 在 Compose 目录执行 PostgreSQL 逻辑备份；默认保存到 ./backups。
backup_dir="${1:-./backups}"
mkdir -p "$backup_dir"
chmod 700 "$backup_dir"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
out="$backup_dir/wecom_ai_${stamp}.sql.gz"

docker compose exec -T postgres pg_dump -U wecom -d wecom_ai --clean --if-exists \
  | gzip -9 > "$out"
chmod 600 "$out"
gzip -t "$out"
printf '备份完成：%s\n' "$out"
