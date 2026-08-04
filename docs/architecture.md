# 架构说明

## 身份与隔离

微信客服的 `external_userid` 不直接作为业务主键。平台创建内部 UUID，并保存：

- `external_id_hash`：HMAC-SHA256，用于稳定查找；
- `external_id_encrypted`：Fernet 加密，仅发送回复时解密；
- 用户配置、会话、消息、记忆和用量均按内部 `user_id` 隔离。

## 消息流程

1. FastAPI 校验回调签名并解密事件；
2. 回调在 PostgreSQL 中创建持久化同步任务，再尝试通过 Redis 唤醒 Worker，并立即返回 200；
3. Worker 领取 Outbox 任务并调用 `sync_msg`，遍历所有分页；
4. 每条消息按平台和外部 `msgid` 幂等写入 PostgreSQL；
5. 消息、游标与消息 Outbox 任务在同一事务内提交；
6. Worker 使用客服账号同步锁与消息处理锁，失败按指数退避，超过上限进入死信；
7. 命令引擎优先处理 `/` 命令，否则调用模型；
8. 回复通过 `send_msg` 发出并记录出站消息；
9. Redis 不可用时，Worker 数据库轮询和补偿扫描保证任务不会仅因通知丢失而消失。

## 配置优先级

平台默认值 → 客服账号默认值（后续）→ 用户设置 → 当前会话临时设置（后续）。

## 扩展点

- `providers.py`：增加 Anthropic、Gemini、Ollama、Dify 等供应商；
- `commands.py`：增加知识库、预设和 BYOK 绑定；
- `wecom.py`：增加图片、语音、文件和欢迎事件；
- 后续采用 pgvector 提供每用户独立知识库。
