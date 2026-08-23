"""月见八千代默认角色卡自动播种测试。

覆盖：
- 新用户 resolve_user 时自动创建并激活默认卡
- 已有卡的同名用户不覆盖
- ensure_default_persona_card 幂等补建
- 内嵌种子文本满足稳定契约，不依赖开发者机器上的私有附件
- providers.py 对 sensenova 模型注入 reasoning_effort=none
- 非 sensenova 模型不注入
"""

import httpx
import pytest

from wecom_ai_gateway import cards as card_service
from wecom_ai_gateway import providers as prov
from wecom_ai_gateway import security as sec
from wecom_ai_gateway.commands import execute
from wecom_ai_gateway.models import CharacterCard, User, UserSettings
from wecom_ai_gateway.persona_seed import (
    DEFAULT_PERSONA_FORMAT,
    DEFAULT_PERSONA_NAME,
    DEFAULT_PERSONA_TEXT,
)
from wecom_ai_gateway.services import ensure_default_persona_card, resolve_user


def read_text_from_card(card: CharacterCard) -> str:
    assert card.content_encrypted is not None
    return sec.decrypt_secret(card.content_encrypted)


def user(db):
    u = User()
    db.add(u)
    db.flush()
    db.add(UserSettings(user_id=u.id))
    db.commit()
    return u


def test_resolve_user_auto_seeds_default_persona(db):
    u = resolve_user(db, "u1", "inst-a", channel="wecom_kf")
    db.flush()
    cards = db.query(CharacterCard).filter(CharacterCard.user_id == u.id).all()
    assert len(cards) == 1
    card = cards[0]
    assert card.name == DEFAULT_PERSONA_NAME
    assert card.format == DEFAULT_PERSONA_FORMAT
    # CharacterCard.active 由 user_settings.active_card_id 推导，不是卡自身的字段
    assert read_text_from_card(card) == DEFAULT_PERSONA_TEXT
    us = db.get(UserSettings, u.id)
    assert us.active_card_id == card.id


def test_resolve_user_does_not_overwrite_existing_card(db):
    u = resolve_user(db, "u2", "inst-a", channel="wecom_kf")
    db.commit()
    card = db.query(CharacterCard).one()
    # 覆盖为"用户自己的卡"
    card.content_encrypted = card_service.encrypt_card_content("## 用户自己的卡")
    db.commit()
    same = resolve_user(db, "u2", "inst-a", channel="wecom_kf")
    assert same.id == u.id
    cards = db.query(CharacterCard).filter(CharacterCard.user_id == u.id).all()
    assert len(cards) == 1
    assert read_text_from_card(cards[0]) == "## 用户自己的卡"


def test_ensure_default_persona_card_backfills_user_without_card(db):
    u = user(db)
    r = ensure_default_persona_card(db, u.id)
    db.commit()
    assert r["created"] is True
    assert r["active_card_id"] is not None
    card = db.get(CharacterCard, r["active_card_id"])
    assert card.name == DEFAULT_PERSONA_NAME
    assert read_text_from_card(card) == DEFAULT_PERSONA_TEXT
    assert db.get(UserSettings, u.id).active_card_id == card.id


def test_ensure_default_persona_card_is_idempotent_when_user_has_card(db):
    u = resolve_user(db, "u3", "inst-a", channel="wecom_kf")
    db.commit()
    r = ensure_default_persona_card(db, u.id)
    assert r["created"] is False
    assert r["active_card_id"] is not None


def test_persona_seed_text_is_self_contained_and_complete():
    """公开仓库测试不得依赖开发者主目录中的上传附件。"""
    assert DEFAULT_PERSONA_TEXT == DEFAULT_PERSONA_TEXT.strip()
    assert not DEFAULT_PERSONA_TEXT.startswith("\ufeff")
    assert "月见八千代" in DEFAULT_PERSONA_TEXT
    assert "理性 60%" in DEFAULT_PERSONA_TEXT
    assert "简体中文" in DEFAULT_PERSONA_TEXT


@pytest.mark.anyio
async def test_provider_injects_reasoning_effort_none_for_sensenova(monkeypatch):
    captures = {}

    class Patcher:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, url, headers, json):
            captures["url"] = url
            captures["json"] = dict(json)
            resp = httpx.Response(200, json={
                "choices": [{"message": {"content": "你好"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 5},
            })
            resp._request = httpx.Request("POST", url)
            return resp

    # 必须在 providers 模块的 httpx 命名空间上打补丁——模块级 import 已锁定引用
    monkeypatch.setattr("wecom_ai_gateway.providers.httpx.AsyncClient", Patcher)
    from wecom_ai_gateway.config import settings

    monkeypatch.setattr(settings, "openai_compatible_base_url", "https://token.sensenova.cn/v1")
    monkeypatch.setattr(settings, "openai_compatible_api_key", "fake-key")
    monkeypatch.setattr(settings, "request_timeout_seconds", 10.0)

    client = prov.OpenAICompatibleProvider()
    await client.complete(
        messages=[{"role": "user", "content": "hi"}],
        model="sensenova-6.8-flash-lite",
        temperature=0.7,
        max_tokens=512,
    )
    assert captures["json"].get("reasoning_effort") == "none"


@pytest.mark.anyio
async def test_provider_does_not_inject_reasoning_effort_for_non_sensenova(monkeypatch):
    captures = {}

    class Patcher:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, url, headers, json):
            captures["json"] = dict(json)
            resp = httpx.Response(200, json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            })
            resp._request = httpx.Request("POST", url)
            return resp

    monkeypatch.setattr("wecom_ai_gateway.providers.httpx.AsyncClient", Patcher)
    from wecom_ai_gateway.config import settings

    monkeypatch.setattr(settings, "openai_compatible_base_url", "https://api.deepseek.com/v1")
    monkeypatch.setattr(settings, "openai_compatible_api_key", "fake-key")
    monkeypatch.setattr(settings, "request_timeout_seconds", 10.0)

    client = prov.OpenAICompatibleProvider()
    await client.complete(
        messages=[{"role": "user", "content": "hi"}],
        model="deepseek-v4-flash",
        temperature=0.7,
        max_tokens=512,
    )
    assert "reasoning_effort" not in captures["json"]


def test_persona_name_shows_in_status_command(db):
    u = resolve_user(db, "u4", "inst-a", channel="wecom_kf")
    db.commit()
    # 通过 /status 观察默认卡是否被激活
    resp = execute(db, u.id, "/status")
    assert DEFAULT_PERSONA_NAME in resp.reply