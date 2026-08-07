"""角色卡系统测试：解析、加密存取与指令生命周期。"""

import base64
import struct
import zlib

import pytest
from fastapi.testclient import TestClient

from wecom_ai_gateway import cards as card_service
from wecom_ai_gateway.commands import execute
from wecom_ai_gateway.main import app
from wecom_ai_gateway.models import CharacterCard, Memory, User, UserSettings
from wecom_ai_gateway.security import decrypt_secret

client = TestClient(app)

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


def _png_chunk(ctype: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + ctype + payload + struct.pack(
        ">I", zlib.crc32(ctype + payload) & 0xFFFFFFFF
    )


def _tiny_png(extra_chunks: list[bytes]) -> bytes:
    ihdr = _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    idat = _png_chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
    iend = _png_chunk(b"IEND", b"")
    return b"\x89PNG\r\n\x1a\n" + ihdr + b"".join(extra_chunks) + idat + iend


def test_extract_card_from_png_v2_text_chunk():
    png = _tiny_png([_png_chunk(b"tEXt", b"chara\x00" + ST_V2_CARD.encode())])
    assert card_service.extract_card_from_png(png) == ST_V2_CARD


def test_extract_card_from_png_v3_itxt_chunk():
    v3 = '{"spec":"chara_card_v3","data":{"name":"Eve","description":"测试"}}'
    payload = b"ccv3\x00\x00\x00en\x00ccv3\x00" + v3.encode()
    png = _tiny_png([_png_chunk(b"iTXt", payload)])
    assert card_service.extract_card_from_png(png) == v3


def test_extract_card_from_png_v3_compressed():
    v3 = '{"spec":"chara_card_v3","data":{"name":"Zip","description":"压缩"}}'
    payload = b"ccv3\x00\x01\x00en\x00ccv3\x00" + zlib.compress(v3.encode())
    png = _tiny_png([_png_chunk(b"iTXt", payload)])
    assert card_service.extract_card_from_png(png) == v3


def test_extract_card_from_non_png_and_broken_png_returns_none():
    assert card_service.extract_card_from_png(b"not a png") is None
    truncated = _tiny_png([_png_chunk(b"tEXt", b"chara\x00" + ST_V2_CARD.encode())])[:20]
    assert card_service.extract_card_from_png(truncated) is None


@pytest.mark.anyio
def test_admin_import_card_via_png_and_text():
    from wecom_ai_gateway.db import SessionLocal
    from wecom_ai_gateway.models import User

    headers = {"X-Admin-Token": "test-admin-token"}
    db = SessionLocal()
    u = User()
    db.add(u)
    db.commit()
    uid = u.id
    db.close()

    # PNG 导入（v2 tEXt）
    png = _tiny_png([_png_chunk(b"tEXt", b"chara\x00" + ST_V2_CARD.encode())])
    r = client.post(
        f"/api/admin/users/{uid}/cards/import",
        headers=headers,
        json={"name": "从PNG导入", "png_base64": base64.b64encode(png).decode()},
    )
    assert r.status_code == 200, r.text
    assert r.json()["card"]["format"] == "st_v2"

    # 文本导入（SOUL.md）
    r = client.post(
        f"/api/admin/users/{uid}/cards/import",
        headers=headers,
        json={"name": "从文本导入", "content": SOUL_MD},
    )
    assert r.status_code == 200
    assert r.json()["card"]["format"] == "soul_md"

    # 管理端元数据不含内容；库里是密文且可解回原文
    rows = client.get(f"/api/admin/users/{uid}/cards", headers=headers).json()
    assert len(rows) == 2
    assert all("content" not in row and "content_encrypted" not in row for row in rows)

    # 空内容 / 双字段 / 坏 PNG 拒绝
    assert client.post(
        f"/api/admin/users/{uid}/cards/import", headers=headers, json={"name": "x"}
    ).status_code == 400
    assert client.post(
        f"/api/admin/users/{uid}/cards/import",
        headers=headers,
        json={"name": "x", "content": "a", "png_base64": "b"},
    ).status_code == 400
    assert client.post(
        f"/api/admin/users/{uid}/cards/import",
        headers=headers,
        json={"name": "x", "png_base64": base64.b64encode(b"not png").decode()},
    ).status_code == 400
