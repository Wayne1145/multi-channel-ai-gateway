from wecom_ai_gateway.commands import execute
from wecom_ai_gateway.models import User, UserSettings


def user(db):
    u = User()
    db.add(u)
    db.flush()
    db.add(UserSettings(user_id=u.id))
    db.commit()
    return u


def test_user_settings_are_isolated(db):
    a, b = user(db), user(db)
    assert execute(db, a.id, "/model use deepseek-reasoner").handled
    execute(db, a.id, "/persona set 你是甲的助手")
    db.commit()
    assert "deepseek-reasoner" in execute(db, a.id, "/status").reply
    assert "deepseek-chat" in execute(db, b.id, "/status").reply
    assert execute(db, b.id, "/persona show").reply != "你是甲的助手"


def test_memory_lifecycle(db):
    u = user(db)
    assert "保存" in execute(db, u.id, "/memory add 我喜欢短回答").reply
    db.commit()
    assert "我喜欢短回答" in execute(db, u.id, "/memory list").reply
    assert "删除" in execute(db, u.id, "/memory delete 1").reply


def test_invalid_parameters(db):
    u = user(db)
    assert "超出" in execute(db, u.id, "/temperature 9").reply
    assert "未知命令" in execute(db, u.id, "/does-not-exist").reply
