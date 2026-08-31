from wecom_ai_gateway.channels import registry
from wecom_ai_gateway.worker import migrate_legacy_knowledge, register_worker_adapters


def test_worker_registers_clawbot_adapter():
    registry._adapters.pop("wechat_clawbot", None)

    register_worker_adapters()

    assert registry.get("wechat_clawbot").channel_key == "wechat_clawbot"


def test_worker_migrates_legacy_knowledge_at_startup(db):
    from wecom_ai_gateway.models import KnowledgeItem, User

    user = User(display_name="legacy-worker")
    db.add(user)
    db.flush()
    row = KnowledgeItem(user_id=user.id, title="旧知识", content="仍是明文")
    db.add(row)
    db.commit()

    migrated = migrate_legacy_knowledge()
    db.expire_all()

    assert migrated == 1
    assert row.content == ""
    assert row.content_encrypted