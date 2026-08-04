#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  printf '用法：%s <backup.sql.gz>\n' "$0" >&2
  exit 2
fi
backup="$1"
test -f "$backup"
gzip -t "$backup"

# 恢复会覆盖当前业务库。生产执行前应先停止 API/Worker 并额外保留一份新备份。
docker compose stop api worker
gzip -dc "$backup" | docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U wecom -d wecom_ai
docker compose up -d api worker
printf '恢复完成：%s\n' "$backup"
