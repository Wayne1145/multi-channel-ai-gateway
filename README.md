# WeCom AI Gateway

一个面向企业微信「微信客服」的独立、多用户大模型网关。普通微信用户通过同一个客服入口聊天，但每个人拥有隔离的会话、模型、人设、参数和长期记忆。

> 本项目不依赖个人微信 Hook、旧版 Windows 客户端或 Hermes 私有会话。

## 当前能力

- 企业微信客服回调验签与 AES 解密
- `sync_msg` 完整分页、按 `msgid` 幂等入库、按客服账号持久化游标
- Redis 任务队列，API 与 AI Worker 分离
- PostgreSQL 数据持久化及 Alembic 迁移
- OpenAI-compatible 模型接口
- 每用户独立身份、会话、模型、人设和参数
- 私有长期记忆（默认关闭）
- 每日 Token 配额
- 管理 API 和轻量管理首页
- Docker Compose / 1Panel 友好部署

## 微信命令

发送 `/help` 查看命令。主要命令：

```text
/status
/models
/model use deepseek-chat
/persona set 你是一位严谨的学习助手
/temperature 0.6
/max-tokens 2048
/context 20
/new
/clear confirm
/memory on
/memory add 我偏好简洁回答
/memory list
/usage
```

所有命令只修改发送者自己的配置。用户不能读取他人的会话或记忆。

## 快速部署

```bash
cp .env.example .env
# 编辑 .env，填入企业微信客服与模型配置
docker compose up -d --build
curl http://127.0.0.1:18082/health
```

企业微信后台回调 URL：

```text
https://你的域名/wecom/kf/callback
```

将反向代理指向 `127.0.0.1:18082`。生产环境必须设置高强度的 `ADMIN_TOKEN`、`IDENTITY_HMAC_KEY` 和 `SECRET_ENCRYPTION_KEY`。

生成 Fernet 加密密钥：

```bash
python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

## 架构

```text
微信用户 → 企业微信客服 → FastAPI 回调网关 → Redis
                                           ↓
                                     Worker / sync_msg
                                           ↓
                 PostgreSQL ← 用户隔离/命令/会话 → 模型供应商
                                           ↓
                                      send_msg 回复
```

## 安全边界

- 原始 `external_userid` 使用 Fernet 加密，检索使用 HMAC-SHA256。
- 模型密钥和企业微信 Secret 只从环境变量读取。
- `.env`、数据库、日志和上传文件不会进入 Git。
- 管理 API 需要 `X-Admin-Token`。
- 长期记忆默认关闭，并只能由当前用户管理。
- 建议定期轮换在聊天、工单或终端中出现过的凭据。

## 开发

```bash
uv sync --extra dev
uv run pytest
uv run ruff check src tests
```

详见 [部署文档](docs/deployment.md)、[架构说明](docs/architecture.md) 和 [安全策略](SECURITY.md)。

## 微信客服限制

企业微信目前限制：客户主动发送消息后，企业通常可在 48 小时内最多下发 5 条消息。平台会将一次 AI 回复尽量合并成一条文本。接口返回成功不保证最终投递，生产环境还应处理消息发送失败事件。

## License

Apache-2.0
