"""跨渠道统一身份测试：绑定码生成/校验/合并/一次性。"""

from datetime import UTC, datetime, timedelta

from wecom_ai_gateway.binding import create_bind_code, resolve_bind
from wecom_ai_gateway.models import (
    Account,
    BindCode,
    ChannelIdentity,
    CharacterCard,
    Memory,
    Message,
    Preset,
    User,
    UserSettings,
)
from wecom_ai_gateway.security import encrypt_secret, external_id_hash, hash_password


def _user(db, name: str, channel: str, external_id: str, account_id: str = "acct-1") -> User:
    user = User(display_name=name, mode="self_service")
    db.add(user)
    db.flush()
    db.add(UserSettings(user_id=user.id))
    db.add(
        ChannelIdentity(
            user_id=user.id,
            channel=channel,
            account_id=account_id,
            external_id_hash=external_id_hash(external_id),
            external_id_encrypted=encrypt_secret(external_id),
        )
    )
    return user


def test_create_bind_code_returns_6_digit_and_replaces_old(db):
    user = _user(db, "A", "wechat_clawbot", "a@im.wechat")
    code1 = create_bind_code(db, user.id)
    assert len(code1) == 6 and code1.isdigit()
    code2 = create_bind_code(db, user.id)
    assert code1 != code2
    assert db.query(BindCode).filter_by(user_id=user.id).count() == 1


def test_resolve_bind_merges_identity_and_data(db):
    user_a = _user(db, "A", "wechat_clawbot", "a@im.wechat")
    user_b = _user(db, "B", "wecom_kf", "b-external", "wk-b")
    db.add(CharacterCard(user_id=user_b.id, name="B的卡", format="soul_md", content_encrypted=None))
    db.add(Memory(user_id=user_b.id, content="", content_encrypted=encrypt_secret("B记忆")))
    db.add(Preset(user_id=user_b.id, name="B预设", config={}))
    db.add(
        Message(
            user_id=user_b.id,
            channel="wecom_kf",
            channel_instance_id="wk-b",
            external_message_id="m-1",
            direction="inbound",
            message_type="text",
            content="旧消息",
            status="sent",
        )
    )
    db.commit()

    code = create_bind_code(db, user_a.id)
    result = resolve_bind(
        db,
        code,
        user_id=user_b.id,
        channel="wecom_kf",
        account_id="wk-b",
        external_id="b-external",
    )

    assert result["ok"] is True
    # B 的身份迁移到 A
    identities = db.query(ChannelIdentity).filter_by(user_id=user_a.id).count()
    assert identities == 2
    assert db.get(User, user_b.id) is None
    # 数据迁移
    assert db.query(Message).filter_by(user_id=user_a.id).count() == 1
    assert db.query(Memory).filter_by(user_id=user_a.id).count() == 1
    assert db.query(CharacterCard).filter_by(user_id=user_a.id).count() == 1
    assert db.query(Preset).filter_by(user_id=user_a.id).count() == 1


def test_resolve_bind_same_user_rejected(db):
    user = _user(db, "A", "wechat_clawbot", "a@im.wechat")
    code = create_bind_code(db, user.id)
    result = resolve_bind(
        db, code, user_id=user.id, channel="wechat_clawbot", account_id="acct-1", external_id="a@im.wechat"
    )
    assert result["ok"] is False
    assert "当前账号" in result["message"]


def test_resolve_bind_expired_code_rejected(db):
    user_a = _user(db, "A", "wechat_clawbot", "a@im.wechat")
    user_b = _user(db, "B", "wecom_kf", "b-ext", "wk-b")
    code = create_bind_code(db, user_a.id)
    row = db.query(BindCode).filter_by(code=code).first()
    row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    db.commit()

    result = resolve_bind(
        db, code, user_id=user_b.id, channel="wecom_kf", account_id="wk-b", external_id="b-ext"
    )
    assert result["ok"] is False
    assert "过期" in result["message"]


def test_resolve_bind_deletes_source_account_and_sessions(db):
    user_a = _user(db, "A", "wechat_clawbot", "a@im.wechat")
    user_b = _user(db, "B", "wecom_kf", "b-ext", "wk-b")
    db.add(
        Account(user_id=user_b.id, username="bbind", password_hash=hash_password("strong-pass-123"), role="user")
    )
    db.commit()
    code = create_bind_code(db, user_a.id)

    result = resolve_bind(
        db, code, user_id=user_b.id, channel="wecom_kf", account_id="wk-b", external_id="b-ext"
    )

    assert result["ok"] is True
    assert db.query(Account).filter_by(user_id=user_b.id).count() == 0
