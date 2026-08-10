# WeCom AI Gateway

一个面向企业微信「微信客服」的独立、多用户大模型网关。普通微信用户通过同一个客服入口聊天，但每个人拥有隔离的会话、模型、人设、参数和长期记忆。

> 本项目不依赖个人微信 Hook、旧版 Windows 客户端或 Hermes 私有会话。

## 当前能力

- 企业微信客服回调验签与 AES 解密
- `sync_msg` 完整分页、按 `msgid` 幂等入库、按客服账号持久化游标
- 多渠道入站幂等范围包含具体渠道实例，避免不同微信账号的局部消息 ID 相互冲突
- Redis 唤醒 + PostgreSQL 持久化 Outbox，API 与 AI Worker 分离
- 有限重试、指数退避、死信、人工重放和遗留任务补偿
- 客服账号同步锁与消息处理锁
- PostgreSQL 数据持久化及 Alembic 迁移
- OpenAI-compatible 模型接口；未配置 API Key 时仍可运行网关、命令系统和管理后台，并向普通消息返回维护提示
- 双管理模式：用户自足（self_service）/ 统一管理（managed），支持按用户覆盖；`.env` 可开启全局单用户模式
- 角色卡系统：每用户多槽位，SOUL.md（OpenClaw 标准）与 SillyTavern v2/v3 JSON 双格式，内容加密存储、管理员不可读
- 指令策略三级覆盖（平台 → 渠道 → 用户），支持静默禁用（redirect_to_ai / ignore 两种处理）
- 用户预设：`/preset save` 快照模型、温度、上下文、人设与角色卡，`/preset use` 一键切换
- 长期记忆加密存储（新写入数据入库即密文）
- 平台设置中心：9 组 44 项运行时可调参数（配额/上限/媒体/任务/保留策略等），带类型与上下限校验、审计日志，保存即生效
- 每用户独立身份、会话、模型、人设和参数
- 私有长期记忆（默认关闭）
- 每日 Token 配额
- 管理 API 和轻量管理首页
- Docker Compose / 1Panel 友好部署
- 可选 `wechat_clawbot` Bridge：仓库内置可独立部署的 iLink 文本桥接，多实例会话加密隔离；登录凭据由 Bridge 保管，网关仅处理规范化消息

> 当前版本是 **0.3.0-dev 可靠任务与多渠道基础**，并非最初路线图的全部功能。已实现、已验证与尚未实现的明确边界见 [项目状态与路线图](docs/project-status.md)。接入真实模型或个人微信桥接前仍需隔离环境联调；大规模生产使用前还需发送失败事件、压力与故障测试。

## 微信命令

发送 `/help` 查看命令。主要命令：

```text
/status
/models
/model use deepseek-chat
/persona set 你是一位严谨的学习助手
/temperature 0.6
/max-tokens 4096
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

启用可选 ClawBot Bridge：

```bash
mkdir -p data/clawbot-bridge
sudo chown 10001:10001 data/clawbot-bridge
sudo chmod 700 data/clawbot-bridge
docker compose --profile clawbot up -d --build
```

Bridge 当前完成文本收发；图片、语音、视频与文件仍未实现。详见 [Bridge 文档](bridge/README.md)。

企业微信后台回调 URL：

```text
https://你的域名/wecom/kf/callback
```

将反向代理指向 `127.0.0.1:18082`。生产环境必须设置高强度的 `ADMIN_TOKEN`、`IDENTITY_HMAC_KEY` 和 `SECRET_ENCRYPTION_KEY`。若启用 ClawBot 桥接，还需单独设置 `CLAWBOT_BRIDGE_TOKEN`；它不得与管理员令牌复用。

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

## 持续集成

仓库已启用 `.github/workflows/ci.yml`，每次 push 与 pull request 会运行 Ruff、pytest 覆盖率报告和 Docker 镜像构建。

详见 [项目状态与路线图](docs/project-status.md)、[Bridge 文档](bridge/README.md)、[第三方依赖与参考说明](THIRD_PARTY.md)、[部署文档](docs/deployment.md)、[架构说明](docs/architecture.md) 和 [安全策略](SECURITY.md)。

## 微信客服限制

企业微信目前限制：客户主动发送消息后，企业通常可在 48 小时内最多下发 5 条消息。平台会将一次 AI 回复尽量合并成一条文本。接口返回成功不保证最终投递，生产环境还应处理消息发送失败事件。

## License

Apache-2.0
