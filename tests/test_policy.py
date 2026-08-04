"""双管理模式与指令策略三级覆盖测试。"""

from unittest.mock import patch

from wecom_ai_gateway.models import CommandPolicy, PlatformConfig, User
from wecom_ai_gateway.policy import get_command_decision, normalize_command, resolve_user_mode


def test_normalize_command():
    assert normalize_command("/Card Use x") == "card"
    assert normalize_command("/status") == "status"
    assert normalize_command("hello") is None
    assert normalize_command("") is None


def test_default_decision_allows(db):
    u = User()
    db.add(u)
    db.commit()
    decision = get_command_decision(db, u.id, "wecom_kf", "card")
    assert decision.allowed is True
    assert decision.source == "default"


def test_three_level_override(db):
    u = User()
    db.add(u)
    db.commit()
    # 平台级：禁止 card
    db.add(CommandPolicy(user_id=None, channel=None, command="card", allowed=False))
    db.commit()
    decision = get_command_decision(db, u.id, "wecom_kf", "card")
    assert decision.allowed is False and decision.source == "platform"
    # 渠道级：wecom_kf 放行
    db.add(
        CommandPolicy(user_id=None, channel="wecom_kf", command="card", allowed=True)
    )
    db.commit()
    decision = get_command_decision(db, u.id, "wecom_kf", "card")
    assert decision.allowed is True and decision.source == "channel"
    # 用户级：再禁止并静默忽略
    db.add(
        CommandPolicy(
            user_id=u.id,
            channel=None,
            command="card",
            allowed=False,
            silent_block=True,
            blocked_strategy="ignore",
        )
    )
    db.commit()
    decision = get_command_decision(db, u.id, "wecom_kf", "card")
    assert decision.allowed is False
    assert decision.silent_block is True
    assert decision.blocked_strategy == "ignore"
    assert decision.source == "user"


def test_other_user_policy_does_not_leak(db):
    a, b = User(), User()
    db.add_all([a, b])
    db.commit()
    db.add(CommandPolicy(user_id=a.id, channel=None, command="card", allowed=False))
    db.commit()
    assert get_command_decision(db, b.id, "wecom_kf", "card").allowed is True
    assert get_command_decision(db, a.id, "wecom_kf", "card").allowed is False


def test_mode_resolution_precedence(db):
    u = User()
    db.add(u)
    db.commit()
    # 平台默认（.env）
    assert resolve_user_mode(db, u) == "self_service"
    # platform_config 覆盖
    db.add(PlatformConfig(key="mode", value={"mode": "managed"}))
    db.commit()
    assert resolve_user_mode(db, u) == "managed"
    # 用户级覆盖
    u.mode = "self_service"
    db.commit()
    assert resolve_user_mode(db, u) == "self_service"
    # single 全局开关最高优先
    with patch("wecom_ai_gateway.policy.settings.single_user_mode", True):
        assert resolve_user_mode(db, u) == "single"
