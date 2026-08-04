from fastapi.testclient import TestClient

from wecom_ai_gateway.main import app

client = TestClient(app)


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_admin_auth():
    assert client.get("/api/admin/stats").status_code == 401
    assert client.get("/api/admin/stats", headers={"X-Admin-Token": "test-admin-token"}).status_code == 200
