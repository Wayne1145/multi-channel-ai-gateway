from fastapi.testclient import TestClient

from wecom_ai_gateway.db import SessionLocal
from wecom_ai_gateway.main import app
from wecom_ai_gateway.models import Account, ChannelInstance, User
from wecom_ai_gateway.security import hash_password

client = TestClient(app)


def _login_user(username: str = "qr_user") -> tuple[str, str]:
    db = SessionLocal()
    user = User(display_name="QR User", mode="self_service")
    db.add(user)
    db.flush()
    db.add(
        Account(
            user_id=user.id,
            username=username,
            password_hash=hash_password("qr-user-pass-123"),
            role="user",
        )
    )
    db.commit()
    user_id = user.id
    db.close()
    token = client.post(
        "/api/auth/login",
        json={"username": username, "password": "qr-user-pass-123"},
    ).json()["token"]
    return user_id, token


def test_user_can_create_and_start_own_clawbot_instance(monkeypatch):
    from wecom_ai_gateway.channels import registry

    user_id, token = _login_user()
    adapter = registry.get("wechat_clawbot")

    async def pending_start(_instance_id):
        return {
            "status": "pending_login",
            "qrcode_url": "https://qr.example/private-login-token",
        }

    monkeypatch.setattr(adapter, "start_instance", pending_start)
    headers = {"Authorization": f"Bearer {token}"}
    created = client.post(
        "/api/me/channel-instances",
        headers=headers,
        json={"instance_name": "我的微信"},
    )
    started = client.post(
        f"/api/me/channel-instances/{created.json()['id']}/start",
        headers=headers,
    )

    assert created.status_code == 200
    assert created.json()["owner_user_id"] == user_id
    assert started.status_code == 200
    assert started.json()["status"] == "logging_in"
    assert "qrcode_url" not in str(started.json())
    assert started.json()["login"]["qrcode_available"] is True


def test_qrcode_svg_requires_owner_session(monkeypatch):
    from wecom_ai_gateway.channels import registry

    _, token = _login_user("owner")
    _, other_token = _login_user("other")
    adapter = registry.get("wechat_clawbot")

    async def pending_start(_instance_id):
        return {"status": "pending_login", "qrcode_url": "https://qr.example/secret"}

    monkeypatch.setattr(adapter, "start_instance", pending_start)
    headers = {"Authorization": f"Bearer {token}"}
    instance = client.post(
        "/api/me/channel-instances",
        headers=headers,
        json={"instance_name": "Owner 微信"},
    ).json()
    client.post(f"/api/me/channel-instances/{instance['id']}/start", headers=headers)

    unauthorized = client.get(f"/api/me/channel-instances/{instance['id']}/qrcode")
    foreign = client.get(
        f"/api/me/channel-instances/{instance['id']}/qrcode",
        headers={"Authorization": f"Bearer {other_token}"},
    )
    owner = client.get(
        f"/api/me/channel-instances/{instance['id']}/qrcode",
        headers=headers,
    )

    assert unauthorized.status_code == 401
    assert foreign.status_code == 404
    assert owner.status_code == 200
    assert owner.headers["content-type"].startswith("image/svg+xml")
    assert b"<svg" in owner.content
    assert b"https://qr.example/secret" not in owner.content


def test_user_cannot_start_foreign_instance():
    user_id, token = _login_user("first")
    other_id, _ = _login_user("second")
    db = SessionLocal()
    row = ChannelInstance(
        channel="wechat_clawbot",
        instance_name="别人的微信",
        owner_user_id=other_id,
        login_state={},
        status="offline",
        config={},
    )
    db.add(row)
    db.commit()
    instance_id = row.id
    db.close()

    response = client.post(
        f"/api/me/channel-instances/{instance_id}/start",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert user_id != other_id
    assert response.status_code == 404


def test_admin_qrcode_svg_accepts_admin_bearer_session(monkeypatch):
    from wecom_ai_gateway.channels import registry
    from wecom_ai_gateway.config import settings

    monkeypatch.setattr(settings, "admin_username", "wayne")
    adapter = registry.get("wechat_clawbot")

    async def pending_start(_instance_id):
        return {"status": "pending_login", "qrcode_url": "https://qr.example/admin-secret"}

    monkeypatch.setattr(adapter, "start_instance", pending_start)
    admin_token = client.post(
        "/api/auth/login",
        json={"username": "wayne", "password": "test-admin-token"},
    ).json()["token"]
    headers = {"Authorization": f"Bearer {admin_token}"}
    instance = client.post(
        "/api/admin/channel-instances",
        headers=headers,
        json={"channel": "wechat_clawbot", "instance_name": "管理员微信"},
    ).json()
    client.post(f"/api/admin/channel-instances/{instance['id']}/start", headers=headers)

    response = client.get(
        f"/api/admin/channel-instances/{instance['id']}/qrcode",
        headers=headers,
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store, private"
    assert response.headers["content-type"].startswith("image/svg+xml")
