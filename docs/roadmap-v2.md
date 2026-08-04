# Tsukuyomi AI Gateway · v2 产品路线方案

> 定位：多场景、多用户、多渠道的 AI 交流管理平台。
> 本文档基于用户需求澄清与开源生态调研（2026-08-05）撰写，是 v2 开发的路线图与架构蓝图。

---

## 1. 产品定位

不再是"企业微信客服机器人"，而是：

> 一套自托管的 AI 交流平台：多人、多角色、多模型、多渠道，可一键在
> 「用户自足」与「统一管理」两种运营模式间切换。

适用场景：
- **个人单机**（单用户简单模式）：只服务自己，界面与功能自动简化。
- **小圈子共享**（用户自足模式）：每人用自己的微信，各自管理角色卡、记忆与模型密钥，互相看不到隐私。
- **商业化托管**（统一管理模式）：管理员统一分发人设与模型，用户以为在跟真客服/真角色交流（指令可静默禁用）。

---

## 2. 核心概念

| 概念 | 说明 |
|---|---|
| 用户 User | 平台统一身份；多渠道身份映射到同一用户（已有 `channel_identities`） |
| 模式 Mode | `self_service`（用户自足）/ `managed`（统一管理）/ `single`（单用户），平台级配置，可按用户覆盖 |
| 角色卡 Character Card | 用户的人格槽位；格式兼容 **SOUL.md**（OpenClaw 标准，Markdown）与 **SillyTavern v2/v3 角色卡**（JSON）；服务端加密存储 |
| 记忆 Memory | 用户私有长期记忆（已有 `memories`），同样加密 |
| 供应商 Provider | 管理员级共享供应商 + 用户 BYOK（自带 API Key，加密存储） |
| 预设 Preset | 一组配置快照（模型+参数+角色卡+记忆开关），面板或指令一键切换 |
| 指令策略 Command Policy | 每用户/每渠道的指令权限：`allowed` / `blocked` / `silent_block`（静默） |
| 渠道实例 Channel Instance | 一个具体接入：企微客服账号 / 一个扫码绑定的个人微信 ClawBot 实例 / 未来飞书应用等 |
| 平台配置 Platform Config | key-value 配置（模式、默认值等） |

---

## 3. 双管理模式

### 3.1 模式矩阵

| 能力 | 用户自足 self_service | 统一管理 managed |
|---|---|---|
| 角色卡 | 用户自建多张、自由切换 | 管理员分发，用户只读/不可见来源 |
| 记忆库 | 用户自管 | 管理员统一（或关闭） |
| 模型/API Key | 支持 BYOK | 仅管理员统一配置 |
| 聊天指令 | 默认可用 | 管理员逐指令控制 |
| 指令静默禁用 | 不适用 | 可开启：用户发指令时**不提示被禁**，直接当普通消息处理或静默忽略 |
| 管理员可见性 | 管理全局，**看不到**用户角色卡/记忆/密钥（物理加密） | 全权管理 |
| 典型场景 | 用户知道对面是 AI | 客服接待、角色扮演托管 |

### 3.2 指令静默禁用（关键机制）

统一管理模式的核心差异化能力。在 `command_policies` 中：

- `allowed=true`：指令正常执行；
- `allowed=false, silent_block=false`：回复"该指令不可用"；
- `allowed=false, silent_block=true`：**不回复任何禁用提示**，该条消息按两种子策略之一处理：
  - `redirect_to_ai`：当作普通消息交给 AI 回复（最像真客服）；
  - `ignore`：无任何回复。

策略粒度：`平台默认 → 渠道 → 用户` 三级覆盖。

### 3.3 单用户简单模式

- 由配置手动开启：`.env` 中 `SINGLE_USER_MODE=true`（**不是自动检测**——刚部署完只有管理员一人，自动检测会卡住添加用户的流程）。
- 开启后：管理后台隐藏用户管理/多角色管理等复杂项，只保留"我的设置"；指令权限全部放行。

---

## 4. 数据模型扩展

在现有 0.2.x 模型上新增/扩展：

### 4.1 新增表

```text
character_cards
  id, user_id(FK,CASCADE,idx), name, format('soul_md'|'st_v2'|'st_v3'),
  content_encrypted(Text), active(bool), created_at, updated_at
  # 加密存储；管理员查询接口不解密

user_providers                      -- BYOK
  id, user_id(FK,CASCADE,idx), provider_key('openai-compatible'|...),
  base_url, api_key_encrypted(Text), models(JSON), is_default(bool)

command_policies                    -- 指令权限
  id, user_id(FK nullable), channel(nullable), command, allowed(bool),
  silent_block(bool), blocked_strategy('redirect_to_ai'|'ignore')
  UNIQUE(user_id, channel, command)

channel_instances                   -- ClawBot/未来多渠道实例
  id, channel('wechat_clawbot'|'wecom_kf'|...), owner_user_id(FK nullable),
  instance_name, login_state(JSON), session_encrypted(Text),
  status('offline'|'logging_in'|'online'|'error'), config(JSON), created_at, updated_at

platform_config                     -- 平台级配置
  key(PK), value(JSON)
```

### 4.2 扩展现有表

```text
users
  + mode('self_service'|'managed'|'single'|NULL)  -- NULL=跟随平台默认

user_settings
  + active_card_id(FK character_cards nullable)
  + provider_key  -- 当前生效供应商（优先用户 BYOK，其次平台）
```

### 4.3 加密方案

- 复用现有 `security.py`（Fernet/AES 对称加密，主密钥在服务器 `.env`）；
- 角色卡、记忆、BYOK 密钥三类字段**入库即密文**；
- 管理员 API 与后台**不提供解密出口**——技术上不可读，不是展示层隐藏；
- 主密钥可轮换（`KEK` 方案，后续迭代）。

---

## 5. 角色卡系统

### 5.1 双格式兼容

| 格式 | 来源 | 结构 |
|---|---|---|
| `soul_md` | OpenClaw SOUL.md 标准 | Markdown：Core Truths / Boundaries / Vibe / Continuity |
| `st_v2` / `st_v3` | SillyTavern 角色卡（行业事实标准） | JSON（v2 可嵌 PNG/WebP，v3 支持资源字段）：`char_name, description, personality, scenario, first_mes, mes_example, creator_notes, system_prompt, post_history_instructions, alternate_greetings, tags, character_version` |

### 5.2 功能

- 每用户多槽位（多张卡），`active` 标记当前生效；
- 面板切换 + 聊天指令切换（`/card list` `/card use 名称`）；
- 导入导出：SOUL.md 纯文本 / SillyTavern PNG 卡（解析内嵌 JSON）/ JSON；
- 注入方式：生效卡内容 → system_prompt 前缀（角色定义）+ 记忆 + 用户参数。

---

## 6. 渠道抽象层

### 6.1 统一消息模型

```python
class ChannelMessage:
    channel: str            # "wecom_kf" | "wechat_clawbot" | ...
    instance_id: str        # 渠道实例 ID
    sender_id: str          # 渠道侧用户标识
    msgtype: str            # text/image/voice/...
    content: str | None
    media: list             # 附件（已下载解密）
    raw: dict
    timestamp: datetime

class OutgoingMessage:
    channel, instance_id, to_sender_id, text, media, metadata
```

### 6.2 ChannelAdapter 接口

```python
class ChannelAdapter(ABC):
    channel_key: str
    async def start_instance(self, instance_id) -> None   # 企微回调监听 / ClawBot 扫码启动
    async def stop_instance(self, instance_id) -> None
    async def send(self, msg: OutgoingMessage) -> str     # 返回渠道消息 ID
    async def resolve_identity(self, sender_id) -> str    # 统一用户 ID（跨渠道合并）
    def webhook_handler(self) -> Callable                # webhook 类渠道
```

### 6.3 已确认渠道生态

| 渠道 | 方式 | 现状 |
|---|---|---|
| 企业微信客服 | 官方 API（回调 + sync_msg + send_msg） | ✅ 已实现（0.2.x） |
| 微信 ClawBot | 扫码绑定个人微信号；上游腾讯官方插件 `@tencent-weixin/openclaw-weixin`，社区 `wong2/weixin-agent-sdk`（Agent 接口 `chat({conversationId,text,media})`）、`fastclaw-ai/weclaw`（多账号） | 待接入（P2） |
| 飞书 / QQ / 企微应用 / Telegram | 官方机器人 API；LangBot 渠道层为参考 | 后续 |

### 6.4 ClawBot 多实例架构（一服务器 N 实例）

```text
每绑定一个微信号 = 一个 channel_instance
  ├─ 独立扫码登录态（QR 生成 → 用户扫码 → 凭证加密入库）
  ├─ 独立消息循环（asyncio 任务或独立 worker 协程）
  ├─ 独立队列与幂等（复用现有 Outbox 可靠性层）
  └─ 归属 owner_user_id → 复用该用户的角色卡/记忆/供应商配置

Agent 桥接：平台实现 weixin-agent-sdk 的 Agent 接口
  chat(request) → resolve 用户 → 加载模式/角色卡/记忆 → 模型调用 → 返回
```

### 6.5 统一身份（跨渠道合并）

已有 `channel_identities` 表作为地基：同一自然人通过不同渠道进入时，映射到同一 `user_id`（按手机号/实名/管理员手动绑定策略，后续迭代）。

---

## 7. 分阶段路线

| 阶段 | 内容 | 验收 |
|---|---|---|
| P0 | ✅ 已交付：企微客服网关 + 多用户隔离 + 指令系统 + Outbox 可靠性 + DeepSeek + 管理后台 | 已上线 |
| P1 | 角色卡系统（SOUL.md 多槽位 + SillyTavern 导入导出 + 加密）；管理模式（self_service/managed/single）；CommandPolicy 指令策略（含静默禁用）；用户端设置面板 | 两种模式可切换演示；用户建 2 张卡并切换；管理员看不到用户卡内容 |
| P2 | 渠道抽象层落地；微信 ClawBot 扫码多实例（复用 weixin-agent-sdk Agent 接口）；统一身份合并 | 同一服务器同时跑 ≥2 个 ClawBot 实例互不干扰；跨渠道同用户合并 |
| P3 | 更多渠道（飞书/QQ/企微应用）；预设系统；用量/审计增强；BYOK 网页绑定页 | 三渠道可接入；BYOK 自助绑定 |
| P4 | 生态：插件、RAG/文件、语音、MCP、人工接管 | 按需开放 |

**建议节奏**：P1 是下一阶段（角色卡 + 双模式是平台差异化的灵魂），P2 的渠道抽象在设计上先行（P1 时就把消息处理与渠道解耦），实现放在 P1 之后。

---

## 8. 风险与取舍

1. **ClawBot 合规性**：个人微信扫码接入属于非官方渠道（腾讯出品但仍有封号风险）。方案：渠道层独立、可开关；README 明示风险；企业客户主推企微客服官方渠道。
2. **加密与功能平衡**：角色卡/记忆全加密后，管理员侧无法做内容审核。取舍：提供"审计模式"开关（明文索引摘要，不存全文）。
3. **多实例资源**：每个 ClawBot 实例消耗独立登录态与连接；单服务器实例数需压测（目标 ≥10 实例/2C4G）。
4. **模式切换数据迁移**：managed → self_service 切换时，用户数据从"管理员分发"转为"用户私有"，需迁移脚本将分发内容复制为加密用户数据。

---

## 9. 参考开源项目

- 微信 ClawBot：[@tencent-weixin/openclaw-weixin](https://npmjs.com/package/@tencent-weixin/openclaw-weixin) · [wong2/weixin-agent-sdk](https://github.com/wong2/weixin-agent-sdk) · [fastclaw-ai/weclaw](https://github.com/fastclaw-ai/weclaw)
- 人格文件标准：[OpenClaw SOUL.md](https://github.com/openclaw/openclaw)（docs/concepts/soul.md）
- 角色卡标准：[SillyTavern](https://github.com/SillyTavern/SillyTavern)（Character Card Spec v2/v3）
- 多用户平台参考：[LibreChat](https://github.com/danny-avila/LibreChat)（多用户+Admin Panel）· [OpenWebUI](https://github.com/open-webui/open-webui)（RBAC+用量分析）
- 多渠道参考：[LangBot](https://github.com/langbot-app/LangBot)（渠道适配层）
