# 架构说明

## 身份与隔离

微信客服的 `external_userid` 不直接作为业务主键。平台创建内部 UUID，并保存：

- `external_id_hash`：HMAC-SHA256，用于稳定查找；
- `external_id_encrypted`：Fernet 加密，仅发送回复时解密；
- 用户配置、会话、消息、记忆和用量均按内部 `user_id` 隔离。

## 消息流程

1. FastAPI 校验回调签名并解密事件；
2. 回调仅把 `Token + open_kfid` 放入 Redis，同步返回 200；
3. Worker 调用 `sync_msg`，遍历所有分页；
4. 每条消息按平台和外部 `msgid` 幂等写入 PostgreSQL；
5. 游标按 `open_kfid` 独立提交；
6. 文本客户消息进入 AI 队列；
7. 命令引擎优先处理 `/` 命令，否则调用模型；
8. 回复通过 `send_msg` 发出并记录出站消息。

## 配置优先级

平台默认值 → 客服账号默认值（后续）→ 用户设置 → 当前会话临时设置（后续）。

## 扩展点

- `providers.py`：增加 Anthropic、Gemini、Ollama、Dify 等供应商；
- `commands.py`：增加知识库、预设和 BYOK 绑定；
- `wecom.py`：增加图片、语音、文件和欢迎事件；
- 后续采用 pgvector 提供每用户独立知识库。
