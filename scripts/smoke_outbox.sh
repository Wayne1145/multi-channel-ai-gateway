#!/usr/bin/env bash
# 队列自检脚本：真实 Worker 领取→失败→退避→死信→管理API重放→清理
# 全程不调用企业微信，不触碰真实消息数据。
set -euo pipefail
cd /opt/1panel/docker/compose/wecom-ai-gateway

PSQL() { docker compose exec -T postgres psql -U wecom -d wecom_ai -tA -c "$1"; }
REDIS() { docker compose exec -T redis redis-cli "$@"; }

echo "===STEP0: CLEANUP STALE SELF_TEST==="
PSQL "DELETE FROM outbox_tasks WHERE task_type='self_test';" >/dev/null
echo "stale removed"

echo "===STEP1: INSERT SELF_TEST TASK==="
TASK_ID=$(PSQL "INSERT INTO outbox_tasks (id, task_type, dedupe_key, payload, status, attempts, available_at) VALUES (gen_random_uuid(), 'self_test', 'self_test:'||SUBSTR(md5(random()::text),1,12), '{}'::json, 'pending', 0, now()) RETURNING id;" | grep -Eo '^[0-9a-f-]{36}$' | head -1)
[ -n "$TASK_ID" ] || { echo "FATAL: no task id returned"; exit 1; }
echo "TASK_ID=$TASK_ID"
REDIS RPUSH wecom-ai:wake 1 >/dev/null
echo "wake sent"

echo "===STEP2: WAIT 12s FOR NATURAL FAILURE + BACKOFF==="
sleep 12
PSQL "SELECT 'status='||status||' attempts='||attempts||' future_retry='||(available_at > now())||' err='||COALESCE(last_error,'') FROM outbox_tasks WHERE id='$TASK_ID';"

echo "===STEP3: ACCELERATE TO RETRY LIMIT -> DEAD==="
PSQL "UPDATE outbox_tasks SET attempts=4, status='pending', available_at=now(), locked_at=NULL WHERE id='$TASK_ID';" >/dev/null
REDIS RPUSH wecom-ai:wake 1 >/dev/null
sleep 5
PSQL "SELECT 'status='||status||' attempts='||attempts||' err='||COALESCE(last_error,'') FROM outbox_tasks WHERE id='$TASK_ID';"

echo "===STEP4: ADMIN API — DEAD LIST + REPLAY==="
TOKEN=$(grep '^ADMIN_TOKEN=' .env | cut -d= -f2 | tr -d '\r')
echo "-- dead list contains task:"
curl -s -H "X-Admin-Token: $TOKEN" http://127.0.0.1:18082/api/admin/tasks/dead | grep -c "$TASK_ID" || true
echo "-- replay:"
curl -s -X POST -H "X-Admin-Token: $TOKEN" http://127.0.0.1:18082/api/admin/tasks/$TASK_ID/replay
echo
echo "-- status after replay:"
PSQL "SELECT 'status='||status||' attempts='||attempts FROM outbox_tasks WHERE id='$TASK_ID';"

echo "===STEP5: CLEANUP==="
PSQL "DELETE FROM outbox_tasks WHERE id='$TASK_ID';"
echo "cleaned"

echo "===DONE==="
