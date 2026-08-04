from fastapi.testclient import TestClient

from wecom_ai_gateway.main import app

client = TestClient(app)


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_admin_auth():
    assert client.get("/api/admin/stats").status_code == 401
    headers = {"X-Admin-Token": "test-admin-token"}
    assert client.get("/api/admin/stats", headers=headers).status_code == 200
    assert client.get("/api/admin/tasks/dead", headers=headers).status_code == 200
    assert client.post("/api/admin/tasks/missing/replay", headers=headers).status_code == 409


def test_usage_trend():
    from datetime import UTC, datetime

    from wecom_ai_gateway.db import SessionLocal
    from wecom_ai_gateway.models import UsageRecord

    headers = {"X-Admin-Token": "test-admin-token"}
    db = SessionLocal()
    db.add(UsageRecord(user_id="u1", provider="openai-compatible", model="deepseek-chat", prompt_tokens=100, completion_tokens=50))
    db.add(UsageRecord(user_id="u1", provider="openai-compatible", model="deepseek-chat", prompt_tokens=30, completion_tokens=20))
    db.commit()
    db.close()

    r = client.get("/api/admin/usage/trend?days=7", headers=headers)
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 7
    today = datetime.now(UTC).date().isoformat()
    today_row = next(row for row in rows if row["date"] == today)
    assert today_row["tokens"] == 200
    assert client.get("/api/admin/usage/trend?days=999", headers=headers).status_code == 200
