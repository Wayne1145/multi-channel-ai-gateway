# 部署文档

## 资源建议

MVP 至少需要 2 核、2GB 内存和 5GB 可用磁盘。若启用文件知识库，应扩容磁盘并增加对象存储。

## 1Panel

项目可放置于：

```text
/opt/1panel/docker/compose/wecom-ai-gateway
```

在 1Panel 的「容器 → 编排」中导入 `docker-compose.yml`，网站反向代理到：

```text
http://127.0.0.1:18082
```

## ClawBot 桥接（可选）

> 个人微信接入可能受平台规则、上游 Agent 稳定性及账号风险影响。请只在隔离测试账号和明确授权范围内启用；生产默认不启用。

1. 将实际 ClawBot/微信 Agent 运行在独立容器或主机上，并暴露受内网保护的 HTTP 桥接服务；
2. 在网关 `.env` 中设置 `CLAWBOT_BRIDGE_BASE_URL` 与独立的 `CLAWBOT_BRIDGE_TOKEN`；桥接地址不得携带查询参数、Cookie 或凭据；
3. 通过管理 API 创建 `wechat_clawbot` 实例，再启动实例；
4. 桥接服务调用 `POST /api/internal/channel-instances/{instance_id}/messages`，使用 `Authorization: Bearer <CLAWBOT_BRIDGE_TOKEN>` 投递规范化入站消息。

启动实例后，桥接可返回 `{status: "pending_login", qrcode_url: "https://..."}`；
管理 API 只会保存并返回白名单登录状态，不保存 `bot_token`、Cookie 或会话密钥。
扫码成功后桥接调用 `POST /api/internal/channel-instances/{instance_id}/status` 上报
`{status: "online", account_id: "..."}`，网关才会接受该实例的入站消息。

公开仓库只包含渠道抽象和上述 HTTP 契约；具体个人微信协议运行时应独立部署并自行承担平台合规与可用性风险。
5. 桥接服务应实现三个端点：`POST /instances/{id}/start`、`/stop`、`/messages`。出站文本字段为 `conversationId` 与 `text`。

桥接令牌、扫码会话、Cookie 和个人微信凭据只允许保存在桥接服务的受保护存储中，绝不可写入网关的渠道实例配置、数据库、日志或 Git。

## 备份

至少备份：

- `data/postgres/`
- `.env`（单独加密备份）

推荐使用 `pg_dump` 做逻辑备份，不要在数据库运行时直接复制数据目录：

```bash
chmod +x scripts/*.sh
./scripts/backup.sh
```

恢复前先阅读脚本；恢复会短暂停止 API 和 Worker：

```bash
./scripts/restore.sh backups/wecom_ai_YYYYMMDDTHHMMSSZ.sql.gz
```

## 部署验收

```bash
./scripts/verify-deployment.sh
```

该脚本验证容器、健康接口、Alembic 版本、业务表、Redis 和 `.env` 文件权限。

## 升级

```bash
git pull --ff-only
docker compose build
docker compose up -d --build
```

### 可选 ClawBot Bridge

仓库内置的 Bridge 默认受 Compose profile 隔离，不会随普通部署启动。启用前，在 `.env` 中设置
独立的 `CLAWBOT_BRIDGE_TOKEN`，并将 `CLAWBOT_BRIDGE_BASE_URL` 指向
`http://clawbot-bridge:8787`。该令牌不得与 `ADMIN_TOKEN` 复用。

```bash
# Bridge 以 UID/GID 10001 运行；先确保持久化目录真实可写。
mkdir -p data/clawbot-bridge
sudo chown 10001:10001 data/clawbot-bridge
sudo chmod 700 data/clawbot-bridge
docker compose --profile clawbot up -d --build
docker compose --profile clawbot exec -T clawbot-bridge \
  sh -c 'touch /data/.permission-probe && rm /data/.permission-probe'
```

Bridge 启动时会扫描每个实例目录的 `session.encrypted`，恢复有效会话并向网关重新上报
`online`；有效会话无需重新扫码。若 iLink 返回会话失效错误，Bridge 会清除旧密文并回到扫码状态。
不要把 `data/clawbot-bridge/`、`.env`、二维码临时链接、token 或游标提交到仓库。

API 容器启动时会先执行 `alembic upgrade head`。禁止通过删除数据目录解决升级问题。
