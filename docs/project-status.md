# Project Status and Roadmap

## Current version: 0.3.0-dev (self-service multi-channel platform)

The project now provides a deployable multi-user gateway for WeCom customer service and optional WeChat iLink/ClawBot instances. It includes durable Outbox processing, per-user isolation, account activation and recovery, MFA, model routing/failover, encrypted persona/memory/knowledge storage, restricted read-only tools, media metadata and a responsive admin/user console.

## Implemented

### Reliability and channels

- WeCom callback validation/decryption, paginated `sync_msg`, idempotent ingestion and send-failure events.
- PostgreSQL Outbox with lease fencing, retries, dead letters, replay and compensation scans.
- `wechat_clawbot` Bridge with encrypted multi-instance sessions, precise per-message reply context, text and media protocol support.
- Live Bridge status reconciliation, protected status endpoint, QR auto-rotation, transient-network retry, status timestamps, offline/recovery alerts and a 72-hour soak monitor.
- Responsive mobile channel console with truthful Chinese lifecycle states.

### Accounts, identity and privacy

- Administrator and user sessions with scrypt passwords, TOTP MFA and one-time recovery codes.
- Existing chat users can send `/account` for a 15-minute one-time account activation link.
- Existing account holders can send `/account reset` for a 15-minute one-time password-reset link; reset revokes sessions/challenges and preserves MFA.
- User identity center: masked owned identities, bind-code generation, password-protected merge preview/confirmation and guarded unbind.
- Per-user encrypted role cards, long-term memories and BYOK credentials. Administrators can inspect metadata but not private content.

### Models, tools and knowledge

- Ordered model groups with retryable failover; BYOK remains isolated from platform credentials.
- Restricted structured tools: current time, weather and multi-engine web search. No shell, arbitrary URL, file or device-control tools.
- Encrypted document RAG for manual text, TXT, Markdown, HTML, PDF, DOCX and safe public HTTPS URLs.
- Keyed local 256-dimensional multilingual feature embeddings, PostgreSQL pgvector candidate search, lexical/vector score fusion and explicit `[KB:title#chunk]` citations.
- Legacy plaintext knowledge entries are migrated into encrypted document/chunk storage by the Worker.

### Operations

- Runtime settings, audit logs, maintenance mode, quotas, retention jobs and optional SMTP alerts.
- Docker Compose deployment with PostgreSQL/pgvector, Redis, API, Worker, optional Bridge and open-websearch.
- CI runs Ruff, full gateway tests, Bridge tests and both image builds.

## Remaining boundaries

These are future product directions, not blockers for personal or small-group use:

- Additional channels such as Feishu, QQ, Web Chat, Telegram or Discord.
- Human-agent takeover, ticket queues, internal notes and customer-service analytics.
- Organization/team RBAC, billing, plans and payment integration.
- Multi-node HA for API, Worker, PostgreSQL, Redis and Bridge.
- OCR for scanned PDFs and richer media understanding beyond current channel/model support.

## Production acceptance boundaries

- Personal-WeChat/iLink automation can carry platform/account risk and should use isolated authorized accounts.
- A successful protocol mock is not a real media acceptance. Image/voice/file must be tested with a fresh inbound context on an isolated account.
- The 72-hour soak monitor is a long-running observation. Accelerated fault-injection tests may pass before the real-time window has completed; reports must distinguish them.
