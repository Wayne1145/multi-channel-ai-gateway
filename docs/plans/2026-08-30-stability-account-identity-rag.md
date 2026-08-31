# Stability, Account Recovery, Identity Center and RAG Implementation Plan

> **For Hermes:** Execute this plan sequentially with strict RED → GREEN → REFACTOR cycles. Do not commit or deploy before all gates and independent review pass.

**Goal:** Complete the product stability and user self-service phases: truthful ClawBot lifecycle, durable alerts and soak monitoring, trusted-channel password recovery, identity management, encrypted hybrid RAG with file/URL ingestion and real media acceptance.

**Architecture:** The Bridge remains the source of truth for live iLink state and exposes a token-protected status endpoint. The gateway periodically reconciles this state into PostgreSQL and emits durable Outbox alerts on transitions. Account recovery and identity operations are scoped to the authenticated/original `User`; all bearer secrets are high-entropy, short-lived, one-time and stored only as SHA-256 digests. Knowledge documents remain tenant-scoped, content is encrypted, chunks carry pgvector-compatible embeddings, and retrieval combines vector and lexical scores with explicit source citations.

**Tech Stack:** FastAPI, SQLAlchemy 2, PostgreSQL 16 + pgvector, SQLite test fallback, Redis/Outbox, vanilla JS/CSS, pypdf, python-docx, pgvector Python package, httpx.

---

## Task 1: ClawBot live status and QR rotation

**Files:**
- Modify: `bridge/src/clawbot_bridge/ilink.py`
- Modify: `bridge/src/clawbot_bridge/runtime.py`
- Modify: `bridge/src/clawbot_bridge/app.py`
- Modify: `src/wecom_ai_gateway/clawbot.py`
- Test: `bridge/tests/test_runtime.py`, `bridge/tests/test_app.py`, `tests/test_clawbot.py`

**Acceptance:** Expired QR produces a fresh pending QR without entering error; Bridge exposes protected per-instance status; gateway adapter can fetch it; sensitive credentials never appear.

## Task 2: Persist truthful lifecycle and alerts

**Files:**
- Modify: `src/wecom_ai_gateway/models.py`
- Create: `migrations/versions/0016_stability_and_self_service.py`
- Create: `src/wecom_ai_gateway/channel_health.py`
- Modify: `src/wecom_ai_gateway/main.py`, `worker.py`, `tasks.py`
- Test: `tests/test_api.py`, `tests/test_alert.py`, `tests/test_worker_channels.py`

**Acceptance:** Store `status_updated_at`, `last_online_at`, `last_error_at`, and sanitized `last_error`; list endpoints reconcile Bridge truth; transitions queue deduplicated failure/recovery alerts; Worker continuously reconciles without blocking messages.

## Task 3: Mobile lifecycle UI and soak harness

**Files:**
- Modify: `web/index.html`, `web/static/app.js`, `web/static/app.css`
- Create: `scripts/clawbot_soak_monitor.py`
- Test: `tests/test_web_ui.py`, script self-test

**Acceptance:** Chinese statuses, last online/error, reconnect controls and auto-refresh remain visible on mobile. Soak monitor writes secret-free JSONL snapshots and exits nonzero on sustained unhealthy state; accelerated fault-injection tests pass; production 72-hour run is launched durably.

## Task 4: Trusted-channel password reset

**Files:**
- Modify: `src/wecom_ai_gateway/models.py`, `services.py`, `main.py`, `commands.py`
- Create: `src/wecom_ai_gateway/password_reset.py`
- Extend migration `0016`
- Modify: `web/index.html`, `web/static/app.js`
- Create/Test: `tests/test_password_reset.py`, `tests/test_web_ui.py`

**Acceptance:** `/account reset` sends a 15-minute one-time fragment link; raw token is absent from logs/history/DB; invalid tokens do not trigger scrypt; successful reset revokes sessions, preserves MFA, consumes token and requires normal login.

## Task 5: Identity center and safe merge/unbind

**Files:**
- Modify: `src/wecom_ai_gateway/models.py`, `binding.py`, `services.py`, `main.py`
- Extend migration `0016`
- Modify: `web/index.html`, `web/static/app.js`, `web/static/app.css`
- Test: `tests/test_binding.py`, `tests/test_me_center.py`, `tests/test_web_ui.py`

**Acceptance:** User sees only masked owned identities and last-seen metadata; can issue a bind code; entering a source-channel code previews counts/conflicts before merge into the current account; confirmation requires current password; unbind requires password + explicit phrase, preserves at least one identity, and rejects online ClawBot identity removal.

## Task 6: Encrypted hybrid RAG and document ingestion

**Files:**
- Modify: `pyproject.toml`, `uv.lock`, `src/wecom_ai_gateway/models.py`, `knowledge.py`, `main.py`, `services.py`, `runtime_settings.py`
- Extend migration `0016`
- Modify: `web/index.html`, `web/static/app.js`, `web/static/app.css`
- Test: `tests/test_knowledge.py`, `tests/test_me_center.py`, `tests/test_runtime_settings.py`, `tests/test_web_ui.py`

**Acceptance:** Manual text, TXT/Markdown/HTML/PDF/DOCX and safe public HTTPS URL imports; MIME/size/path/SSRF validation; encrypted source/chunk text; incremental SHA-256 reindex; pgvector-compatible embeddings with SQLite fallback; lexical + vector fusion; strict user ownership; source/chunk citations in retrieval and model prompt; list/search/delete/reindex UI.

## Task 7: Verification, real media acceptance and delivery

**Files:**
- Update: `README.md`, `docs/project-status.md`, `docs/deployment.md`, `bridge/README.md`
- Add acceptance assets only if needed, then remove after test.

**Acceptance:** Full tests, Ruff, compileall, JS syntax, migration chain on PostgreSQL 16, dependency audit, secret scan, Docker builds, independent review and GitHub CI pass. Back up production; deploy only affected services; verify migration and all health checks. Using a Wayne-triggered fresh inbound context, send and receive image/voice/file on isolated ClawBot and verify DB/Bridge/channel evidence without exposing tokens. Launch the durable 72-hour soak monitor and report it separately from completed accelerated tests.
