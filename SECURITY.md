# Security Policy

请通过 GitHub Security Advisory 私下报告安全问题，不要在公开 Issue 中附带生产密钥、微信用户标识或聊天内容。

## 生产要求

- 使用随机且唯一的管理员令牌、HMAC 密钥和 Fernet 密钥；
- 生产数据库和 Redis 不对公网开放；
- 反向代理只公开 HTTPS；
- 定期轮换企业微信、模型供应商和 GitHub 凭据；
- 不在日志中记录访问令牌、模型密钥、用户原始微信标识和完整回调正文；
- 对管理接口增加 Cloudflare Access、VPN 或 IP 白名单会更安全。
