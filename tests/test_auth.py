import hashlib

from fastapi.testclient import TestClient

from wecom_ai_gateway.db import SessionLocal
from wecom_ai_gateway.main import app
from wecom_ai_gateway.models import Account, AuthSession, ChannelInstance, User
from wecom_ai_gateway.security import hash_password

client = TestClient(app)


def _create_account(username: str = "alice", password: str = "strong-pass-123") -> tuple[str, str]:
    db = SessionLocal()
    user = User(display_name="Alice", mode="self_service")
    db.add(user)
    db.flush()
    account = Account(
        user_id=user.id,
        username=username,
        password_hash=hash_password(password),
        role="user",
    )
    db.add(account)
    db.commit()
    result = (user.id, account.id)
    db.close()
    return result


def test_admin_login_requires_username_and_existing_admin_password(monkeypatch):
    from wecom_ai_gateway.config import settings

    monkeypatch.setattr(settings, "admin_username", "wayne")
    response = client.post(
        "/api/auth/login",
        json={"username": "wayne", "password": "test-admin-token"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["role"] == "admin"
    assert body["username"] == "wayne"
    assert len(body["token"]) >= 32
    assert "test-admin-token" not in str(body)


def test_user_login_returns_opaque_session_and_stores_only_hash():
    user_id, account_id = _create_account()

    response = client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "strong-pass-123"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["role"] == "user"
    assert body["user_id"] == user_id
    token = body["token"]
    db = SessionLocal()
    row = db.query(AuthSession).filter_by(account_id=account_id).one()
    assert row.token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert token not in row.token_hash
    db.close()


def test_user_session_only_lists_own_clawbot_instances():
    user_id, _ = _create_account()
    db = SessionLocal()
    other = User(display_name="Other", mode="self_service")
    db.add(other)
    db.flush()
    own = ChannelInstance(
        channel="wechat_clawbot",
        instance_name="Alice 微信",
        owner_user_id=user_id,
        login_state={},
        status="offline",
        config={},
    )
    foreign = ChannelInstance(
        channel="wechat_clawbot",
        instance_name="Other 微信",
        owner_user_id=other.id,
        login_state={},
        status="offline",
        config={},
    )
    db.add_all([own, foreign])
    db.commit()
    db.close()
    login = client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "strong-pass-123"},
    ).json()

    response = client.get(
        "/api/me/channel-instances",
        headers={"Authorization": f"Bearer {login['token']}"},
    )

    assert response.status_code == 200
    assert [row["instance_name"] for row in response.json()] == ["Alice 微信"]


def test_user_cannot_use_admin_session_for_admin_api():
    _create_account()
    token = client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "strong-pass-123"},
    ).json()["token"]

    response = client.get(
        "/api/admin/users",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 401


def test_self_service_registration_creates_account(monkeypatch):
    from wecom_ai_gateway.config import settings

    monkeypatch.setattr(settings, "platform_mode", "self_service")
    monkeypatch.setattr(settings, "allow_public_registration", True)
    response = client.post(
        "/api/auth/register",
        json={
            "username": "new_user",
            "password": "register-pass-123",
            "display_name": "新用户",
        },
    )

    assert response.status_code == 200
    assert response.json()["role"] == "user"


def test_managed_mode_disables_public_registration(monkeypatch):
    from wecom_ai_gateway.config import settings

    monkeypatch.setattr(settings, "platform_mode", "managed")
    response = client.post(
        "/api/auth/register",
        json={"username": "blocked", "password": "register-pass-123"},
    )

    assert response.status_code == 403


def test_auth_config_reports_registration_availability(monkeypatch):
    from wecom_ai_gateway.config import settings

    monkeypatch.setattr(settings, "platform_mode", "self_service")
    disabled_by_default = client.get("/api/auth/config")
    monkeypatch.setattr(settings, "allow_public_registration", True)
    enabled = client.get("/api/auth/config")

    assert disabled_by_default.json() == {
        "registration_enabled": False,
        "announcement": "",
        "maintenance_mode": False,
    }
    assert enabled.json() == {
        "registration_enabled": True,
        "announcement": "",
        "maintenance_mode": False,
    }


def test_admin_provisions_existing_user_account_without_exposing_hash():
    db = SessionLocal()
    user = User(display_name="已有微信用户", mode="self_service")
    db.add(user)
    db.commit()
    user_id = user.id
    db.close()

    response = client.put(
        f"/api/admin/users/{user_id}/account",
        headers={"X-Admin-Token": "test-admin-token"},
        json={"username": "existing_user", "password": "provision-pass-123"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "user_id": user_id,
        "username": "existing_user",
        "is_active": True,
    }
    assert "password" not in response.text.lower()
    login_response = client.post(
        "/api/auth/login",
        json={"username": "existing_user", "password": "provision-pass-123"},
    )
    assert login_response.status_code == 200
    assert login_response.json()["user_id"] == user_id


def test_admin_password_reset_revokes_existing_sessions():
    user_id, _ = _create_account()
    old_token = client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "strong-pass-123"},
    ).json()["token"]

    reset = client.put(
        f"/api/admin/users/{user_id}/account",
        headers={"X-Admin-Token": "test-admin-token"},
        json={"username": "alice", "password": "new-strong-pass-456"},
    )

    assert reset.status_code == 200
    assert client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {old_token}"}
    ).status_code == 401
    assert client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "new-strong-pass-456"},
    ).status_code == 200


def test_admin_cannot_assign_duplicate_username_to_another_user():
    _create_account()
    db = SessionLocal()
    other = User(display_name="Other")
    db.add(other)
    db.commit()
    other_id = other.id
    db.close()

    response = client.put(
        f"/api/admin/users/{other_id}/account",
        headers={"X-Admin-Token": "test-admin-token"},
        json={"username": "alice", "password": "other-strong-pass-123"},
    )

    assert response.status_code == 409


def test_blocked_user_cannot_login_or_reuse_existing_session():
    user_id, _ = _create_account()
    token = client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "strong-pass-123"},
    ).json()["token"]

    blocked = client.post(
        f"/api/admin/users/{user_id}/block?blocked=true",
        headers={"X-Admin-Token": "test-admin-token"},
    )

    assert blocked.status_code == 200
    assert client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {token}"}
    ).status_code == 401
    assert client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "strong-pass-123"},
    ).status_code == 401


def test_user_summary_contains_only_own_aggregates():
    from wecom_ai_gateway.models import Conversation, UsageRecord

    user_id, _ = _create_account()
    db = SessionLocal()
    other = User(display_name="Other")
    db.add(other)
    db.flush()
    db.add_all(
        [
            Conversation(user_id=user_id),
            UsageRecord(
                user_id=user_id,
                provider="test",
                model="test",
                prompt_tokens=10,
                completion_tokens=5,
            ),
            UsageRecord(
                user_id=other.id,
                provider="test",
                model="test",
                prompt_tokens=999,
                completion_tokens=999,
            ),
        ]
    )
    db.commit()
    db.close()
    token = client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "strong-pass-123"},
    ).json()["token"]

    response = client.get(
        "/api/me/summary",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json()["conversations"] == 1
    assert response.json()["tokens_total"] == 15
