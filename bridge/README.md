# 可选 ClawBot Bridge

该服务把腾讯微信 ClawBot 的 iLink HTTP/JSON 协议转换为 Multi-Channel AI Gateway 的渠道契约。它不依赖 OpenClaw，可作为 Docker Compose 的可选 profile 运行。

## 能力

- 文本与媒体收发：图片、文件、语音、视频；
- 入站媒体只提取安全元数据（类型/文件名/大小）转发网关，**不下载 CDN、不携带解密密钥**；
- 出站媒体完整管线：下载 → AES-128-ECB 加密 → `getuploadurl` 预签名 → CDN 上传 → 构造媒体消息；
- 下载源强制无凭据 https 且拒绝内网/环回地址（防 SSRF）。

## 安全边界

- 登录凭据、逐消息 `context_token`、更新游标只保存在 Bridge 数据卷中。
- 会话通过 `CLAWBOT_BRIDGE_TOKEN` 派生的 Fernet 密钥加密后落盘。
- 网关数据库不会接触 iLink `bot_token` 或媒体 `aes_key`。
- 每个实例使用独立目录、密文文件、游标和回复上下文映射。
- iLink 游标仅在整批消息被网关持久化接受后提交；临时转发失败会保留旧游标并依赖网关幂等重投。
- 用户显式停止实例时会在加密状态中持久化停止意图；Bridge 重启不会擅自恢复，手动启动仍可复用有效密文会话。

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

Bridge 的网关适配、多实例运行时、加密状态、媒体管线与测试为本项目独立实现。iLink HTTP 契约与 CDN 上传协议依据腾讯发布的 MIT 包 `@tencent-weixin/openclaw-weixin` 2.4.6；腾讯 MIT 许可证副本位于 [`third_party/tencent-openclaw-weixin-LICENSE`](third_party/tencent-openclaw-weixin-LICENSE)。详细说明见仓库根目录 [`THIRD_PARTY.md`](../THIRD_PARTY.md)。
