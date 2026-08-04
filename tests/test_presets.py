"""预设系统测试：保存快照、应用与删除。"""

from wecom_ai_gateway.commands import execute
from wecom_ai_gateway.models import Preset, User, UserSettings


def user(db):
    u = User()
    db.add(u)
    db.flush()
    db.add(UserSettings(user_id=u.id))
    db.commit()
    return u


def test_preset_snapshot_apply_delete(db):
    u = user(db)
    # 先调参再保存
    execute(db, u.id, "/model use deepseek-reasoner")
    execute(db, u.id, "/temperature 0.3")
    db.commit()
    assert "保存" in execute(db, u.id, "/preset save 工作").reply
    db.commit()
    row = db.query(Preset).one()
    assert row.config["model"] == "deepseek-reasoner"
    assert row.config["temperature"] == 0.3

    # 改回默认后应用预设
    execute(db, u.id, "/model use deepseek-chat")
    execute(db, u.id, "/temperature 0.7")
    db.commit()
    assert "应用" in execute(db, u.id, "/preset use 工作").reply
    db.commit()
    status = execute(db, u.id, "/status").reply
    assert "deepseek-reasoner" in status
    assert "0.3" in status

    # 覆盖保存 + 删除
    execute(db, u.id, "/preset save 工作")
    db.commit()
    assert "删除" in execute(db, u.id, "/preset delete 工作").reply
    db.commit()
    assert db.query(Preset).count() == 0


def test_preset_unknown_actions(db):
    u = user(db)
    assert "没有这个预设" in execute(db, u.id, "/preset use 不存在").reply
    assert "用法" in execute(db, u.id, "/preset").reply
