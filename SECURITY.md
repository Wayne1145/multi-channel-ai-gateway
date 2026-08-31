# Security Policy

请优先通过 GitHub Security Advisory 私下报告安全问题。若仓库尚未启用 Private Vulnerability Reporting，请联系维护者并仅说明需要私下沟通，绝不要在公开 Issue 中附带漏洞细节、生产密钥、微信用户标识或聊天内容。

## 生产要求

- 使用随机且唯一的管理员令牌、HMAC 密钥和 Fernet 密钥；
- 生产数据库和 Redis 不对公网开放；
- 反向代理只公开 HTTPS；
- 定期轮换企业微信、模型供应商和 GitHub 凭据；
- 不在日志中记录访问令牌、模型密钥、用户原始微信标识和完整回调正文；
- 对管理接口增加 Cloudflare Access、VPN 或 IP 白名单会更安全。
- 账号激活/密码重置原始令牌仅置于 URL fragment，数据库只保存 SHA-256 摘要；不得将原始链接写入消息历史或访问日志。
- 知识文档和分块使用 Fernet 加密；URL 导入仅允许公网 HTTPS、禁止重定向和私网/环回地址，并固定已校验 IP、保留原域名 TLS 校验，防止 SSRF 与 DNS 重绑定。
- ClawBot 入站文档仅在 Bridge 内使用官方 CDN 参数下载并解密，限制 10MB 与文档白名单；网关只保存提取后的 Fernet 密文文本，不保存原始文件、CDN 参数或 AES key。
- 渠道身份合并预览与确认都验证当前密码；解绑至少保留一个身份，在线 ClawBot 必须先停止。
