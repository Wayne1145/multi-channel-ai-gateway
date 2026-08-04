#!/usr/bin/env bash
set -euo pipefail

# 对本机 Compose 部署执行无破坏验收。
docker compose ps
curl -fsS http://127.0.0.1:18082/health
docker compose exec -T api alembic current | grep -q "0002 (head)"
docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U wecom -d wecom_ai -Atc \
  "select count(*) from information_schema.tables where table_schema='public';"
test "$(docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U wecom -d wecom_ai -Atc \
  \"select count(*) from information_schema.tables where table_schema='public' and table_name='outbox_tasks';\")" = "1"
docker compose exec -T redis redis-cli ping
test "$(stat -c '%a' .env)" = "600"
printf '\n部署验收通过。\n'
