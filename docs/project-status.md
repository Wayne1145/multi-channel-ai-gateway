# 项目状态与路线图

## 当前版本：0.3.0-dev（P1 多渠道基础 + 剩余项完成）

已完成可靠任务 MVP、多渠道抽象与安全桥接（P1 第一段），并补齐 P1 剩余部分：
PNG 角色卡导入、媒体安全生命周期、用户详情管理 API、模式迁移审计、管理后台
渠道实例/媒体/用户详情面板。仍不是功能完整或高可用生产平台。

## 已实现

### 可靠消息与安全边界

- 企业微信客服回调验签、AES 解密、URL 验证与 `sync_msg` 分页拉取；
- PostgreSQL 持久化 Outbox、有限重试、指数退避、死信、人工重放、补偿扫描；
- 任务领取 `lease_token`、续租与旧 Worker 栅栏；
- 入站消息幂等、用户 HMAC 检索、原始标识与私密内容加密保存；
- 外部投递前持久化投递栅栏：投递状态未知时不自动补发；
- Provider、角色卡、预设均校验资源所有者；异常、死信与日志统一脱敏；
- 长推理默认等待 300 秒；默认输出预算 4096（`default_max_tokens`）；
  空模型响应进入可靠重试，不向用户伪造“没有内容”的成功提示。

### 用户能力与双模式

- 每用户独立会话、模型名、人设、温度、上下文、最大输出、记忆开关与每日配额；
- 角色卡多槽位（`/card list/new/use/set/show/export/delete`）；
  SOUL.md 与 SillyTavern v2/v3 双格式；**PNG 内嵌卡导入**（tEXt `chara` / iTXt `ccv3`，含 zlib 压缩）；
- 预设系统（`/preset list/save/use/delete`）；
- `/help`、`/status`、`/model`、`/persona`、`/temperature`、`/context`、`/new`、`/clear`、`/memory`、`/usage` 等命令；
- `single`、`self_service`、`managed` 模式及命令策略覆盖（含静默禁用）；
- 模式切换迁移服务 `migration.migrate_user_mode`：审计轨迹 + 用户数据规模摘要；
- 管理统计、用户模式、死信重放、封禁与最小化用户数据视图。

### 媒体消息安全生命周期（新增）

- `media_assets` 表：只记录元数据（类型/大小/哈希/渠道定位），不主动下载外部文件；
- 类型白名单与大小上限（`media_allowed_mime_types` / `media_max_size_bytes`），超限记 rejected；
- 保留时长 TTL（`media_retention_hours`，默认 7 天），Worker 每小时清理过期记录；
- 管理 API `GET /api/admin/media` 只返回安全元数据，绝不返回 storage_key/URL；
- 渠道入站媒体（image/voice/file）自动记录，`metadata_json` 只留汇总不滞留凭据。

### P1 多渠道基础

- 渠道适配器协议与进程内注册表；新渠道只能通过统一 `ChannelMessage` / `OutgoingMessage` 进入服务层；
- `wechat_clawbot` HTTP 桥接适配器；管理 API：渠道实例创建、查询、启动、停止；
- 独立桥接 Bearer 鉴权；桥接入站幂等，复用既有用户隔离、会话、Outbox 和模型流水线；
- 管理后台新增：渠道实例面板（创建/启停）、媒体审计、用户详情（卡/预设/BYOK/策略/统计）。

## 已验证

- 单元、API、安全、回调、隔离、Outbox、渠道、媒体、迁移测试（本地全量 69 项）；
- Ruff 与 Python `compileall` 静态检查；`git diff --check`；
- 生产 PostgreSQL 迁移链升级至 0006（media_assets），容器 healthy；
- `pip-audit` 无已知漏洞（v0.3.0 审计修复）。

## 尚未完成或尚未真实联调

- 真实 ClawBot/微信 Agent 的隔离测试实例联调；个人微信渠道存在平台规则与账号风险，默认保持禁用；
- 图片/语音/文件的实际发送（出站媒体上传渠道 media_id）与人工客服接管；
- 企业微信发送失败事件闭环；高并发、断网、Redis/PostgreSQL 故障注入及多 Worker 长稳测试；
- Anthropic、Gemini、Ollama、Dify 等 Provider；BYOK 网页绑定页；
- 多租户组织、细粒度 RBAC、计费、知识库、MCP/插件、更多渠道和高可用部署。

## 发布判断

- **可作为可靠消息网关与多渠道 P1 基础继续开发。**
- **不应宣称为完整多渠道产品或高可用生产平台。**
- ClawBot 桥接仅在隔离实例、独立凭据与明确合规评估后启用。
