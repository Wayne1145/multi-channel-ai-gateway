# Web 预览工具（设计参考，不进生产）

- `theme-preview.html` — 三方向主题预览板（Linear 暗色 / Apple 液态 / Stripe 优雅），右下角悬浮条可实时切换主题、开关动效、演示骨架屏。2026-08-07 改版时用于方案确认；正式版采用 Stripe 优雅方向。
- `seed_local_ui.py` — 本地 SQLite 种子数据脚本，配合 uvicorn 起本地后端验证管理后台 UI。

## 本地验证方法

```bash
# 1. 种子数据（注意模型字段：is_blocked / last_error / payload）
uv run python web-preview/seed_local_ui.py

# 2. 起本地后端（必须 PYTHONPATH=src）
PYTHONPATH=src DATABASE_URL="sqlite:////tmp/admin-ui-test.db" \
  ADMIN_TOKEN="test-admin-token-2026" \
  uv run uvicorn wecom_ai_gateway.main:app --host 127.0.0.1 --port 8921

# 3. 浏览器打开 http://127.0.0.1:8921/ ，用 ADMIN_TOKEN 登录
```

> 说明：`test-admin-token-2026` 仅为本地 UI 验证用测试令牌，与生产 `.env` 无关。
> 预览板直接 `python3 -m http.server` 打开即可（无需后端）。
