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
4. 桥接服务调用 `POST /api/internal/channel-instances/{instance_id}/messages`，使用 `Authorization: Bearer <CLAWBOT_BRIDGE_TOKEN>`；
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
docker compose up -d
```

API 容器启动时会先执行 `alembic upgrade head`。禁止通过删除数据目录解决升级问题。
