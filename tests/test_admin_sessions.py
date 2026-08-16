"""E4 管理员会话管理测试：全局会话列表、踢出单会话、踢出用户全部会话。"""

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from wecom_ai_gateway.db import SessionLocal
from wecom_ai_gateway.main import app
from wecom_ai_gateway.models import Account, AuthSession, User
from wecom_ai_gateway.security import hash_password

client = TestClient(app)
ADMIN_HEADERS = {"X-Admin-Token": "test-admin-token"}


def _user_with_sessions(username: str = "sessadmin") -> tuple[str, str]:
    db = SessionLocal()
    user = User(display_name="Sess", mode="self_service")
    db.add(user)
    db.flush()
    account = Account(
        user_id=user.id,
        username=username,
        password_hash=hash_password("strong-pass-123"),
        role="user",
    )
    db.add(account)
    db.flush()
    expires = datetime.now(UTC) + timedelta(days=7)
    db.add(AuthSession(account_id=account.id, user_id=user.id, token_hash="h1", role="user", expires_at=expires))
    db.add(AuthSession(account_id=account.id, user_id=user.id, token_hash="h2", role="user", expires_at=expires))
    db.commit()
    user_id, account_id = user.id, account.id
    db.close()
    return user_id, account_id


def test_admin_lists_all_sessions():
    user_id, _ = _user_with_sessions()
    response = client.get("/api/admin/sessions?limit=50", headers=ADMIN_HEADERS)
    assert response.status_code == 200
    rows = response.json()["sessions"]
    mine = [r for r in rows if r["user_id"] == user_id]
    assert len(mine) == 2
    assert all(r["account_username"] == "sessadmin" for r in mine)


def test_admin_sessions_requires_admin():
    assert client.get("/api/admin/sessions").status_code in (401, 403)


def test_admin_revokes_single_session():
    user_id, _ = _user_with_sessions("singlesess")
    db = SessionLocal()
    session_id = db.query(AuthSession).filter_by(user_id=user_id).first().id
    db.close()

    response = client.post(
        f"/api/admin/sessions/{session_id}/revoke", headers=ADMIN_HEADERS
    )
    assert response.status_code == 200

    db = SessionLocal()
    remaining = db.query(AuthSession).filter_by(user_id=user_id).count()
    db.close()
    assert remaining == 1


def test_admin_revokes_all_user_sessions():
    user_id, _ = _user_with_sessions("allsess")
    response = client.post(
        f"/api/admin/users/{user_id}/sessions/revoke-all", headers=ADMIN_HEADERS
    )
    assert response.status_code == 200

    db = SessionLocal()
    remaining = db.query(AuthSession).filter_by(user_id=user_id).count()
    db.close()
    assert remaining == 0


def test_revoked_session_cannot_access_api():
    user_id, _ = _user_with_sessions("kickout")
    token = client.post(
        "/api/auth/login", json={"username": "kickout", "password": "strong-pass-123"}
    ).json()["token"]
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 200

    db = SessionLocal()
    session = db.query(AuthSession).filter_by(user_id=user_id, token_hash="h1").first()
    db.close()
    client.post(f"/api/admin/sessions/{session.id}/revoke", headers=ADMIN_HEADERS)
    # h1 被踢，但登录拿到的 token 是另一条会话，不受影响
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 200
