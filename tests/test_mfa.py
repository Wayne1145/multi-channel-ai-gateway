"""E2 TOTP 多因素认证：注册、登录挑战、恢复码与管理员重置。"""

import hashlib

from fastapi.testclient import TestClient

from wecom_ai_gateway.config import settings
from wecom_ai_gateway.db import SessionLocal
from wecom_ai_gateway.main import app
from wecom_ai_gateway.models import Account, AuditLog, AuthSession, MfaCredential, User
from wecom_ai_gateway.security import decrypt_secret, hash_password
from wecom_ai_gateway.totp import generate_totp_secret, totp_code

client = TestClient(app)
ADMIN_HEADERS = {"X-Admin-Token": "test-admin-token"}
PASSWORD = "strong-pass-123"


def _create_account(username: str = "mfauser") -> tuple[str, str]:
    db = SessionLocal()
    user = User(display_name="MFA User", mode="self_service")
    db.add(user)
    db.flush()
    account = Account(
        user_id=user.id,
        username=username,
        password_hash=hash_password(PASSWORD),
        role="user",
    )
    db.add(account)
    db.commit()
    result = (user.id, account.id)
    db.close()
    return result


def _login(username: str = "mfauser", password: str = PASSWORD) -> dict:
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200
    return response.json()


def _enable_user_mfa(username: str = "mfauser") -> tuple[str, list[str]]:
    auth = _login(username)
    headers = {"Authorization": f"Bearer {auth['token']}"}
    setup = client.post(
        "/api/auth/mfa/setup",
        headers=headers,
        json={"password": PASSWORD},
    )
    assert setup.status_code == 200
    body = setup.json()
    enabled = client.post(
        "/api/auth/mfa/enable",
        headers=headers,
        json={"code": totp_code(body["secret"])},
    )
    assert enabled.status_code == 200
    return body["secret"], enabled.json()["recovery_codes"]


def test_totp_matches_rfc_6238_sha1_vector():
    # RFC 6238 的 SHA-1 测试秘钥与 59 秒时间向量。
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    assert totp_code(secret, at_time=59) == "287082"


def test_setup_requires_current_password_and_encrypts_pending_secret():
    _, account_id = _create_account()
    auth = _login()
    headers = {"Authorization": f"Bearer {auth['token']}"}

    denied = client.post(
        "/api/auth/mfa/setup",
        headers=headers,
        json={"password": "wrong-password"},
    )
    response = client.post(
        "/api/auth/mfa/setup",
        headers=headers,
        json={"password": PASSWORD},
    )

    assert denied.status_code == 400
    assert response.status_code == 200
    body = response.json()
    assert body["secret"] in body["otpauth_uri"]
    assert "<svg" in body["qr_svg"]
    db = SessionLocal()
    row = db.query(MfaCredential).filter_by(subject_type="account", subject_id=account_id).one()
    assert row.enabled is False
    assert row.secret_encrypted != body["secret"]
    assert decrypt_secret(row.secret_encrypted) == body["secret"]
    db.close()


def test_enable_requires_valid_code_returns_recovery_codes_once_and_never_stores_plaintext():
    _, account_id = _create_account()
    auth = _login()
    headers = {"Authorization": f"Bearer {auth['token']}"}
    setup = client.post(
        "/api/auth/mfa/setup", headers=headers, json={"password": PASSWORD}
    ).json()

    invalid = client.post(
        "/api/auth/mfa/enable", headers=headers, json={"code": "000000"}
    )
    enabled = client.post(
        "/api/auth/mfa/enable",
        headers=headers,
        json={"code": totp_code(setup["secret"])},
    )

    assert invalid.status_code == 400
    assert enabled.status_code == 200
    codes = enabled.json()["recovery_codes"]
    assert len(codes) == 10
    assert len(set(codes)) == 10
    db = SessionLocal()
    row = db.query(MfaCredential).filter_by(subject_type="account", subject_id=account_id).one()
    assert row.enabled is True
    assert len(row.recovery_code_hashes) == 10
    assert all(code not in str(row.recovery_code_hashes) for code in codes)
    db.close()
    status = client.get("/api/auth/mfa/status", headers=headers).json()
    assert status == {"enabled": True, "recovery_codes_remaining": 10}
    assert setup["secret"] not in str(status)


def test_mfa_login_creates_no_session_until_single_use_challenge_is_verified(monkeypatch):
    _create_account()
    secret, _ = _enable_user_mfa()
    db = SessionLocal()
    db.query(AuthSession).delete()
    db.commit()
    db.close()
    monkeypatch.setattr("wecom_ai_gateway.totp.time.time", lambda: 2_000_000_000)

    first = _login()
    assert first["mfa_required"] is True
    assert "token" not in first
    db = SessionLocal()
    assert db.query(AuthSession).count() == 0
    db.close()

    verified = client.post(
        "/api/auth/mfa/verify",
        json={
            "challenge_token": first["challenge_token"],
            "code": totp_code(secret, at_time=2_000_000_000),
        },
    )
    replay = client.post(
        "/api/auth/mfa/verify",
        json={
            "challenge_token": first["challenge_token"],
            "code": totp_code(secret, at_time=2_000_000_000),
        },
    )

    assert verified.status_code == 200
    assert verified.json()["role"] == "user"
    assert replay.status_code == 401
    assert client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {verified.json()['token']}"},
    ).status_code == 200


def test_totp_code_cannot_be_replayed_across_two_login_challenges(monkeypatch):
    _create_account()
    clock = [2_000_000_000]
    monkeypatch.setattr("wecom_ai_gateway.totp.time.time", lambda: clock[0])
    secret, _ = _enable_user_mfa()
    clock[0] += 30
    first = _login()
    code = totp_code(secret, at_time=clock[0])

    accepted = client.post(
        "/api/auth/mfa/verify",
        json={"challenge_token": first["challenge_token"], "code": code},
    )
    second = _login()
    rejected = client.post(
        "/api/auth/mfa/verify",
        json={"challenge_token": second["challenge_token"], "code": code},
    )

    assert accepted.status_code == 200
    assert rejected.status_code == 401


def test_recovery_code_is_single_use_and_other_codes_remain_available():
    _create_account()
    _, recovery_codes = _enable_user_mfa()
    first = _login()
    accepted = client.post(
        "/api/auth/mfa/verify",
        json={"challenge_token": first["challenge_token"], "code": recovery_codes[0]},
    )
    second = _login()
    replay = client.post(
        "/api/auth/mfa/verify",
        json={"challenge_token": second["challenge_token"], "code": recovery_codes[0]},
    )

    assert accepted.status_code == 200
    assert replay.status_code == 401
    db = SessionLocal()
    row = db.query(MfaCredential).filter_by(subject_type="account").one()
    assert len(row.recovery_code_hashes) == 9
    db.close()


def test_wrong_mfa_code_exhausts_challenge_without_creating_session():
    _create_account()
    _enable_user_mfa()
    db = SessionLocal()
    db.query(AuthSession).delete()
    db.commit()
    db.close()
    challenge = _login()["challenge_token"]

    statuses = [
        client.post(
            "/api/auth/mfa/verify",
            json={"challenge_token": challenge, "code": "000000"},
        ).status_code
        for _ in range(6)
    ]

    assert statuses == [401, 401, 401, 401, 401, 401]
    db = SessionLocal()
    assert db.query(AuthSession).count() == 0
    db.close()


def test_wrong_mfa_code_counts_toward_username_lock(monkeypatch):
    from wecom_ai_gateway import main as main_module

    _create_account()
    _enable_user_mfa()
    recorded = []
    monkeypatch.setattr(main_module, "record_login_failure", recorded.append)
    challenge = _login()["challenge_token"]

    response = client.post(
        "/api/auth/mfa/verify",
        json={"challenge_token": challenge, "code": "000000"},
    )

    assert response.status_code == 401
    assert recorded == ["mfauser"]


def test_password_step_does_not_clear_failure_count_until_mfa_succeeds(monkeypatch):
    from wecom_ai_gateway import main as main_module

    _create_account()
    clock = [2_000_000_000]
    monkeypatch.setattr("wecom_ai_gateway.totp.time.time", lambda: clock[0])
    secret, _ = _enable_user_mfa()
    cleared = []
    monkeypatch.setattr(main_module, "clear_login_failures", cleared.append)

    clock[0] += 30
    challenge = _login()["challenge_token"]
    assert cleared == []
    verified = client.post(
        "/api/auth/mfa/verify",
        json={"challenge_token": challenge, "code": totp_code(secret, at_time=clock[0])},
    )

    assert verified.status_code == 200
    assert cleared == ["mfauser"]


def test_enabling_mfa_revokes_other_sessions_but_preserves_current_session():
    _, account_id = _create_account()
    current = _login()
    other = _login()
    headers = {"Authorization": f"Bearer {current['token']}"}
    setup = client.post(
        "/api/auth/mfa/setup", headers=headers, json={"password": PASSWORD}
    ).json()

    enabled = client.post(
        "/api/auth/mfa/enable",
        headers=headers,
        json={"code": totp_code(setup["secret"])},
    )

    assert enabled.status_code == 200
    assert client.get("/api/auth/me", headers=headers).status_code == 200
    assert client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {other['token']}"}
    ).status_code == 401
    db = SessionLocal()
    assert db.query(AuthSession).filter_by(account_id=account_id).count() == 1
    db.close()


def test_disable_requires_password_and_second_factor_then_revokes_all_sessions(monkeypatch):
    user_id, account_id = _create_account()
    clock = [2_000_000_000]
    monkeypatch.setattr("wecom_ai_gateway.totp.time.time", lambda: clock[0])
    secret, _ = _enable_user_mfa()
    clock[0] += 30
    auth = _login()
    verified = client.post(
        "/api/auth/mfa/verify",
        json={
            "challenge_token": auth["challenge_token"],
            "code": totp_code(secret, at_time=clock[0]),
        },
    )
    headers = {"Authorization": f"Bearer {verified.json()['token']}"}

    denied = client.post(
        "/api/auth/mfa/disable",
        headers=headers,
        json={"password": "wrong-password", "code": totp_code(secret)},
    )
    clock[0] += 30
    disabled = client.post(
        "/api/auth/mfa/disable",
        headers=headers,
        json={"password": PASSWORD, "code": totp_code(secret, at_time=clock[0])},
    )

    assert denied.status_code == 400
    assert disabled.status_code == 200
    db = SessionLocal()
    assert db.query(MfaCredential).filter_by(subject_id=account_id).count() == 0
    assert db.query(AuthSession).filter_by(account_id=account_id).count() == 0
    audit = db.query(AuditLog).filter_by(user_id=user_id, action="mfa.disable").one()
    assert audit.detail == {"subject_type": "account"}
    db.close()


def test_admin_can_enable_mfa_and_login_requires_challenge(monkeypatch):
    monkeypatch.setattr(settings, "admin_username", "wayne")
    initial = _login("wayne", "test-admin-token")
    headers = {"Authorization": f"Bearer {initial['token']}"}
    setup = client.post(
        "/api/auth/mfa/setup",
        headers=headers,
        json={"password": "test-admin-token"},
    ).json()
    enabled = client.post(
        "/api/auth/mfa/enable",
        headers=headers,
        json={"code": totp_code(setup["secret"])},
    )
    assert enabled.status_code == 200
    # 管理员启用 MFA 后，旧的单因素 Header 旁路必须立即关闭；当前 MFA 设置会话仍可用。
    assert client.get("/api/admin/users", headers=ADMIN_HEADERS).status_code == 401
    assert client.get("/api/admin/users", headers=headers).status_code == 200

    login = _login("wayne", "test-admin-token")
    assert login["mfa_required"] is True
    verified = client.post(
        "/api/auth/mfa/verify",
        json={"challenge_token": login["challenge_token"], "code": enabled.json()["recovery_codes"][0]},
    )
    assert verified.status_code == 200
    assert verified.json()["role"] == "admin"


def test_admin_reset_removes_user_mfa_revokes_sessions_and_audits():
    user_id, account_id = _create_account()
    _enable_user_mfa()

    response = client.delete(f"/api/admin/users/{user_id}/mfa", headers=ADMIN_HEADERS)

    assert response.status_code == 200
    db = SessionLocal()
    assert db.query(MfaCredential).filter_by(subject_id=account_id).count() == 0
    assert db.query(AuthSession).filter_by(account_id=account_id).count() == 0
    audit = db.query(AuditLog).filter_by(action="mfa.admin_reset").one()
    assert audit.user_id == user_id
    assert audit.detail == {"subject_type": "account"}
    db.close()
    ordinary_login = _login()
    assert ordinary_login["role"] == "user"
    assert ordinary_login.get("mfa_required") is not True


def test_admin_detail_reports_mfa_state_without_exposing_secret():
    user_id, _ = _create_account()
    secret, recovery_codes = _enable_user_mfa()

    response = client.get(f"/api/admin/users/{user_id}/detail", headers=ADMIN_HEADERS)

    assert response.status_code == 200
    assert response.json()["mfa_enabled"] is True
    assert secret not in response.text
    assert recovery_codes[0] not in response.text


def test_recovery_codes_are_sha256_hashes_not_reversible_secrets():
    _create_account()
    _, recovery_codes = _enable_user_mfa()
    db = SessionLocal()
    row = db.query(MfaCredential).filter_by(subject_type="account").one()
    assert row.recovery_code_hashes[0] == hashlib.sha256(
        recovery_codes[0].replace("-", "").upper().encode()
    ).hexdigest()
    db.close()


def test_generated_secret_has_160_bits_of_base32_entropy():
    secret = generate_totp_secret()
    assert len(secret) == 32
    assert set(secret) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")
