"""角色卡系统测试：解析、加密存取与指令生命周期。"""

from wecom_ai_gateway import cards as card_service
from wecom_ai_gateway.commands import execute
from wecom_ai_gateway.models import CharacterCard, Memory, User, UserSettings
from wecom_ai_gateway.security import decrypt_secret

SOUL_MD = """# Core Truths
我是月读的守门人，已经守望了八千年。
# Boundaries
不泄露用户私密信息。
# Vibe
温柔、准确、克制。
# Continuity
记住每一次对话的约定。
"""

ST_V2_CARD = (
    '{"spec":"chara_card_v2","spec_version":"2.0","data":{'
    '"name":"Alice","description":"安静的图书管理员","personality":"好奇、耐心",'
    '"scenario":"在旧图书馆相遇","system_prompt":"你是 Alice，一位安静的图书管理员。",'
    '"post_history_instructions":"保持安静的氛围"}}'
)


def user(db):
    u = User()
    db.add(u)
    db.flush()
    db.add(UserSettings(user_id=u.id))
    db.commit()
    return u


def test_soul_md_parse():
    parsed = card_service.parse_soul_md(SOUL_MD)
    assert "守门人" in parsed["Core Truths"]
    assert "温柔" in parsed["Vibe"]
    assert "Continuity" in parsed


def test_detect_format():
    assert card_service.detect_format(SOUL_MD) == "soul_md"
    assert card_service.detect_format(ST_V2_CARD) == "st_v2"
    assert card_service.detect_format('{"spec":"chara_card_v3","data":{"name":"A"}}') == "st_v3"
    assert card_service.detect_format("not json") == "soul_md"


def test_st_v2_to_prompt_uses_system_prompt():
    prompt = card_service.card_to_system_prompt("st_v2", ST_V2_CARD)
    assert "Alice" in prompt
    assert "图书管理员" in prompt


def test_card_lifecycle_and_encryption(db):
    u = user(db)
    assert "创建" in execute(db, u.id, "/card new 学者").reply
    db.commit()
    assert "更新" in execute(db, u.id, "/card set " + SOUL_MD).reply
    db.commit()
    row = db.query(CharacterCard).one()
    # 入库必须是密文，且可解密回原文（命令解析会 strip 首尾空白）
    assert row.content_encrypted != SOUL_MD
    assert decrypt_secret(row.content_encrypted) == SOUL_MD.strip()
    assert "学者" in execute(db, u.id, "/card list").reply
    assert "守门人" in execute(db, u.id, "/card show").reply

    # 第二张卡并切换；active 标记保持唯一
    assert "创建" in execute(db, u.id, "/card new 诗人").reply
    db.commit()
    execute(db, u.id, "/card set 我是诗人")
    db.commit()
    assert "诗人" in execute(db, u.id, "/card list").reply
    execute(db, u.id, "/card use 学者")
    db.commit()
    cards = db.query(CharacterCard).all()
    assert sum(1 for c in cards if c.active) == 1

    # 导出与删除
    assert "守门人" in execute(db, u.id, "/card export").reply
    execute(db, u.id, "/card delete 诗人")
    db.commit()
    assert db.query(CharacterCard).count() == 1


def test_st_json_card_roundtrip(db):
    u = user(db)
    execute(db, u.id, "/card new 图书管理员")
    db.commit()
    execute(db, u.id, "/card set " + ST_V2_CARD)
    db.commit()
    row = db.query(CharacterCard).one()
    assert row.format == "st_v2"
    assert "Alice" in execute(db, u.id, "/card export").reply


def test_memory_stored_encrypted(db):
    u = user(db)
    execute(db, u.id, "/memory add 我不吃香菜")
    db.commit()
    mem = db.query(Memory).one()
    assert mem.content_encrypted and mem.content_encrypted != "我不吃香菜"
    assert "我不吃香菜" in execute(db, u.id, "/memory list").reply
