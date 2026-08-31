"""普通用户渠道身份中心测试。"""

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from wecom_ai_gateway.db import SessionLocal
from wecom_ai_gateway.main import app
from wecom_ai_gateway.models import (
    Account,
    ChannelIdentity,
    ChannelInstance,
    CharacterCard,
    CommandPolicy,
    Memory,
    MfaChallenge,
    MfaCredential,
    Preset,
    SettingOverride,
    UsageRecord,
    User,
    UserSettings,
)
from wecom_ai_gateway.security import encrypt_secret, external_id_hash, hash_password

client = TestClient(app)


def _user_with_identity(username: str, channel: str, external_id: str, account_id: str):
    db = SessionLocal()
    try:
        user = User(display_name=username, mode="self_service")
        db.add(user)
        db.flush()
        db.add(UserSettings(user_id=user.id))
        account = Account(
            user_id=user.id,
            username=username,
            password_hash=hash_password("identity-pass-123"),
            role="user",
        )
        identity = ChannelIdentity(
            user_id=user.id,
            channel=channel,
            account_id=account_id,
            external_id_hash=external_id_hash(external_id),
            external_id_encrypted=encrypt_secret(external_id),
            last_seen_at=datetime.now(UTC),
        )
        db.add_all([account, identity])
        db.commit()
        result = (user.id, identity.id)
    finally:
        db.close()
    token = client.post(
        "/api/auth/login",
        json={"username": username, "password": "identity-pass-123"},
    ).json()["token"]
    return (*result, token)


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_identity_list_is_owned_masked_and_never_returns_encrypted_value():
    user_id, identity_id, token = _user_with_identity(
        "identity_owner", "wecom_kf", "sensitive-external-id", "wk-owner"
    )
    _user_with_identity("identity_other", "wechat_clawbot", "other@wechat", "other-inst")

    response = client.get("/api/me/identities", headers=_headers(token))

    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 1
    assert rows[0]["id"] == identity_id
    assert rows[0]["channel"] == "wecom_kf"
    assert rows[0]["masked_external_id"] != "sensitive-external-id"
    assert "external_id" not in rows[0]
    assert "encrypted" not in str(rows[0]).lower()
    assert rows[0]["user_id"] == user_id


def test_user_can_issue_bind_code_from_identity_center():
    _, _, token = _user_with_identity("identity_code", "wecom_kf", "code-ext", "wk-code")

    response = client.post("/api/me/identities/bind-code", headers=_headers(token))

    assert response.status_code == 200
    assert len(response.json()["code"]) >= 22
    assert response.json()["expires_in_seconds"] == 600


def test_merge_preview_reports_counts_and_conflicts_without_mutating():
    target_id, _, token = _user_with_identity("merge_target", "wecom_kf", "target-ext", "wk-target")
    source_id, _, _ = _user_with_identity(
        "merge_source", "wechat_clawbot", "source@wechat", "source-inst"
    )
    db = SessionLocal()
    db.add(CharacterCard(user_id=target_id, name="同名卡", content_encrypted=None))
    db.add(CharacterCard(user_id=source_id, name="同名卡", content_encrypted=None))
    db.add(Memory(user_id=source_id, content="源记忆"))
    db.add(Preset(user_id=source_id, name="源预设", config={}))
    db.commit()
    from wecom_ai_gateway.binding import create_bind_code

    code = create_bind_code(db, source_id)
    db.close()

    response = client.post(
        "/api/me/identities/merge-preview",
        headers=_headers(token),
        json={"code": code, "password": "identity-pass-123"},
    )

    assert response.status_code == 200
    body = response.json()
    assert "source_user_id" not in body
    assert "target_user_id" not in body
    assert body["counts"]["identities"] == 1
    assert body["counts"]["memories"] == 1
    assert body["conflicts"]["card_names"] == ["同名卡"]
    db = SessionLocal()
    assert db.get(User, source_id) is not None
    db.close()


def test_merge_preview_requires_current_password():
    _, _, token = _user_with_identity("preview_target", "wecom_kf", "preview-target", "wk-preview")
    source_id, _, _ = _user_with_identity(
        "preview_source", "wechat_clawbot", "preview-source@wechat", "preview-source"
    )
    db = SessionLocal()
    from wecom_ai_gateway.binding import create_bind_code

    code = create_bind_code(db, source_id)
    db.close()

    response = client.post(
        "/api/me/identities/merge-preview",
        headers=_headers(token),
        json={"code": code, "password": "wrong-password"},
    )

    assert response.status_code == 400


def test_confirmed_merge_keeps_current_login_account_and_consumes_code():
    target_id, _, token = _user_with_identity("merge_keep", "wecom_kf", "keep-ext", "wk-keep")
    source_id, _, _ = _user_with_identity(
        "merge_delete", "wechat_clawbot", "delete@wechat", "delete-inst"
    )
    db = SessionLocal()
    from wecom_ai_gateway.binding import create_bind_code

    code = create_bind_code(db, source_id)
    db.close()

    response = client.post(
        "/api/me/identities/merge",
        headers=_headers(token),
        json={
            "code": code,
            "password": "identity-pass-123",
            "confirm": "MERGE",
        },
    )

    assert response.status_code == 200
    db = SessionLocal()
    assert db.get(User, source_id) is None
    account = db.query(Account).filter_by(username="merge_keep").one()
    assert account.user_id == target_id
    assert db.query(ChannelIdentity).filter_by(user_id=target_id).count() == 2
    db.close()
    assert client.get("/api/auth/me", headers=_headers(token)).status_code == 200


def test_confirmed_merge_transfers_source_channel_instance_ownership():
    target_id, _, token = _user_with_identity(
        "merge_instance_target", "wecom_kf", "instance-target", "instance-target-account"
    )
    source_id, _, _ = _user_with_identity(
        "merge_instance_source", "wechat_clawbot", "instance-source", "owned-instance"
    )
    db = SessionLocal()
    db.add(
        ChannelInstance(
            id="owned-instance",
            channel="wechat_clawbot",
            instance_name="源微信实例",
            owner_user_id=source_id,
            login_state={},
            status="offline",
            config={},
        )
    )
    db.commit()
    from wecom_ai_gateway.binding import create_bind_code

    code = create_bind_code(db, source_id)
    db.close()

    response = client.post(
        "/api/me/identities/merge",
        headers=_headers(token),
        json={"code": code, "password": "identity-pass-123", "confirm": "MERGE"},
    )

    assert response.status_code == 200
    assert response.json()["preview"]["counts"]["channel_instances"] == 1
    db = SessionLocal()
    assert db.get(ChannelInstance, "owned-instance").owner_user_id == target_id
    db.close()


def test_confirmed_merge_preserves_source_usage_policy_override_and_mfa_rows():
    """合并不能因删除源 User 而静默丢失用户级业务/安全记录。"""
    target_id, _, token = _user_with_identity(
        "merge_data_target", "wecom_kf", "data-target", "data-target-inst"
    )
    source_id, _, _ = _user_with_identity(
        "merge_data_source", "wechat_clawbot", "data-source", "data-source-inst"
    )
    db = SessionLocal()
    source_account = db.query(Account).filter_by(user_id=source_id).one()
    db.add_all(
        [
            UsageRecord(
                user_id=source_id,
                provider="source-provider",
                model="source-model",
                prompt_tokens=10,
                completion_tokens=20,
            ),
            CommandPolicy(user_id=source_id, command="search", allowed=False),
            SettingOverride(
                scope_type="user",
                scope_id=source_id,
                key="model",
                value={"value": "source-model"},
            ),
            MfaCredential(
                subject_type="account",
                subject_id=source_account.id,
                secret_encrypted=encrypt_secret("source-mfa-secret"),
                enabled=True,
                recovery_code_hashes=[],
            ),
            MfaChallenge(
                token_hash="a" * 64,
                subject_type="account",
                subject_id=source_account.id,
                account_id=source_account.id,
                user_id=source_id,
                role="user",
                username="merge_data_source",
                attempts=0,
                expires_at=datetime.now(UTC),
            ),
        ]
    )
    db.commit()
    from wecom_ai_gateway.binding import create_bind_code

    code = create_bind_code(db, source_id)
    db.close()

    response = client.post(
        "/api/me/identities/merge",
        headers=_headers(token),
        json={"code": code, "password": "identity-pass-123", "confirm": "MERGE"},
    )

    assert response.status_code == 200
    db = SessionLocal()
    assert db.query(UsageRecord).filter_by(user_id=target_id).count() == 1
    assert db.query(CommandPolicy).filter_by(user_id=target_id, command="search").count() == 1
    override = db.query(SettingOverride).filter_by(scope_type="user", scope_id=target_id).one()
    assert override.value == {"value": "source-model"}
    assert db.query(MfaCredential).filter_by(subject_id=source_account.id).count() == 0
    assert db.query(MfaChallenge).filter_by(user_id=source_id).count() == 0
    db.close()


def test_unbind_requires_password_confirmation_and_keeps_at_least_one_identity():
    _, first_identity, token = _user_with_identity(
        "unbind_user", "wecom_kf", "unbind-ext", "wk-unbind"
    )
    db = SessionLocal()
    user = db.query(Account).filter_by(username="unbind_user").one()
    second = ChannelIdentity(
        user_id=user.user_id,
        channel="wecom_kf",
        account_id="wk-second",
        external_id_hash=external_id_hash("second-ext"),
        external_id_encrypted=encrypt_secret("second-ext"),
    )
    db.add(second)
    db.commit()
    second_id = second.id
    db.close()

    bad = client.post(
        f"/api/me/identities/{second_id}/unbind",
        headers=_headers(token),
        json={"password": "wrong-password", "confirm": "UNBIND"},
    )
    assert bad.status_code == 400

    ok = client.post(
        f"/api/me/identities/{second_id}/unbind",
        headers=_headers(token),
        json={"password": "identity-pass-123", "confirm": "UNBIND"},
    )
    assert ok.status_code == 200

    last = client.post(
        f"/api/me/identities/{first_identity}/unbind",
        headers=_headers(token),
        json={"password": "identity-pass-123", "confirm": "UNBIND"},
    )
    assert last.status_code == 409


def test_online_clawbot_identity_cannot_be_unbound():
    user_id, identity_id, token = _user_with_identity(
        "online_identity", "wechat_clawbot", "online@wechat", "online-inst"
    )
    db = SessionLocal()
    db.add(
        ChannelInstance(
            id="online-inst",
            channel="wechat_clawbot",
            instance_name="在线微信",
            owner_user_id=user_id,
            login_state={"status": "online"},
            status="online",
            config={},
        )
    )
    db.commit()
    db.close()

    response = client.post(
        f"/api/me/identities/{identity_id}/unbind",
        headers=_headers(token),
        json={"password": "identity-pass-123", "confirm": "UNBIND"},
    )

    assert response.status_code == 409
    assert "先停止" in response.json()["detail"]
