"""模式切换迁移服务测试。"""

import pytest

from wecom_ai_gateway.migration import migrate_user_mode
from wecom_ai_gateway.models import AuditLog, CharacterCard, Memory, User


def test_migrate_user_mode_writes_audit_and_returns_scale(db):
    u = User()
    db.add(u)
    db.flush()
    db.add(CharacterCard(user_id=u.id, name="卡1", format="soul_md", content_encrypted="enc"))
    db.add(Memory(user_id=u.id, content="", content_encrypted="enc-mem"))
    db.flush()

    result = migrate_user_mode(db, u, "managed")
    db.commit()

    assert result["from"] is None and result["to"] == "managed"
    assert result["scale"]["cards"] == 1
    assert result["scale"]["memories"] == 1
    assert result["scale"]["presets"] == 0
    log = db.query(AuditLog).filter_by(action="mode.migrate").one()
    assert log.detail["to"] == "managed"


def test_migrate_user_mode_rejects_invalid_target(db):
    u = User()
    db.add(u)
    db.flush()
    with pytest.raises(ValueError):
        migrate_user_mode(db, u, "evil")


def test_mode_roundtrip_keeps_user_data(db):
    u = User()
    db.add(u)
    db.flush()
    db.add(CharacterCard(user_id=u.id, name="私有卡", format="soul_md", content_encrypted="enc"))
    db.flush()

    migrate_user_mode(db, u, "managed")
    db.commit()
    migrate_user_mode(db, u, "self_service")
    db.commit()

    assert u.mode == "self_service"
    # 数据始终按用户私有保留，不受模式切换影响
    assert db.query(CharacterCard).filter_by(user_id=u.id).count() == 1
