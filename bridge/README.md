# 可选 ClawBot Bridge

该服务把腾讯微信 ClawBot 的 iLink HTTP/JSON 文本协议转换为 Multi-Channel AI Gateway 的渠道契约。它不依赖 OpenClaw，可作为 Docker Compose 的可选 profile 运行。

## 安全边界

- 登录凭据、逐消息 `context_token`、更新游标只保存在 Bridge 数据卷中。
- 会话通过 `CLAWBOT_BRIDGE_TOKEN` 派生的 Fernet 密钥加密后落盘。
- 网关数据库不会接触 iLink `bot_token`。
- 每个实例使用独立目录、密文文件、游标和回复上下文映射。
- 当前只支持文本收发；媒体请求会明确返回未实现，而不会伪报成功。

## 启动

先在仓库根目录 `.env` 配置：

```text
CLAWBOT_BRIDGE_BASE_URL=http://clawbot-bridge:8787
CLAWBOT_BRIDGE_TOKEN=至少32字符的独立随机令牌
```

首次启动前创建数据目录并赋予容器用户权限：

```bash
mkdir -p data/clawbot-bridge
sudo chown 10001:10001 data/clawbot-bridge
sudo chmod 700 data/clawbot-bridge
docker compose --profile clawbot up -d --build
```

生产环境应以实际容器用户执行创建与删除探针，确认 `/data` 真正可写。扫码登录、实例管理和受保护二维码均通过网关账号中心完成，不应直接暴露 Bridge 端口。

## 独立开发

```bash
cd bridge
uv sync --extra dev
uv run pytest
uv run ruff check src tests
```

## 来源与许可证

Bridge 的网关适配、多实例运行时、加密状态和测试为本项目独立实现。iLink HTTP 契约依据腾讯发布的 MIT 包 `@tencent-weixin/openclaw-weixin` 2.4.6；腾讯 MIT 许可证副本位于 [`third_party/tencent-openclaw-weixin-LICENSE`](third_party/tencent-openclaw-weixin-LICENSE)。详细说明见仓库根目录 [`THIRD_PARTY.md`](../THIRD_PARTY.md)。
