"""知识库/RAG 测试：分块、检索、命令、注入。"""

from wecom_ai_gateway.commands import execute
from wecom_ai_gateway.knowledge import add_item, build_injection, chunk_text, search
from wecom_ai_gateway.models import User, UserSettings


def test_chunk_text_splits_and_overlaps():
    chunks = chunk_text("甲" * 1000, 300)
    assert len(chunks) >= 3
    assert all(len(c) <= 300 for c in chunks)
    assert "".join(chunks).replace("甲", "") == ""
    assert chunk_text("", 100) == []
    assert chunk_text("短内容", 100) == ["短内容"]


def test_add_item_upserts_and_scopes_by_user(db):
    user = User(display_name="kb", mode="self_service")
    db.add(user)
    db.flush()
    add_item(db, user.id, "产品手册", "这是产品使用说明。支持多行内容。")
    add_item(db, user.id, "产品手册", "更新后的说明。")  # 同名覆盖
    rows = search(db, user.id, "产品使用说明", limit=3)
    assert len(rows) == 1
    assert "更新后的说明" in rows[0]["text"]


def test_search_ranks_relevant_chunk_first(db):
    user = User(display_name="kb2", mode="self_service")
    db.add(user)
    db.flush()
    add_item(db, user.id, "服务器", "我们的服务器部署在境外机房，延迟约 200ms。")
    add_item(db, user.id, "菜单", "今日菜单有红烧肉和清蒸鱼。")

    results = search(db, user.id, "服务器延迟多少", limit=2)
    assert results and results[0]["title"] == "服务器"


def test_search_is_isolated_between_users(db):
    u1 = User(display_name="u1", mode="self_service")
    u2 = User(display_name="u2", mode="self_service")
    db.add(u1)
    db.add(u2)
    db.flush()
    add_item(db, u1.id, "私有", "只有 u1 能看到这段秘密内容。")
    assert search(db, u2.id, "秘密内容", limit=3) == []


def test_build_injection_returns_empty_without_match(db):
    user = User(display_name="kb3", mode="self_service")
    db.add(user)
    db.flush()
    add_item(db, user.id, "服务器运维", "服务器部署在境外机房，延迟约 200 毫秒，带宽 100M。")
    assert build_injection(db, user.id, "今天天气怎么样", max_chunks=3, chunk_chars=800) == ""


def test_kb_commands_lifecycle(db):
    user = User(display_name="kb4", mode="self_service")
    db.add(user)
    db.flush()
    db.add(UserSettings(user_id=user.id))
    db.commit()

    added = execute(db, user.id, "/kb add 运维手册 服务器重启流程：先备份再重启。")
    assert added.handled and "已保存" in added.reply

    listing = execute(db, user.id, "/kb list")
    assert "运维手册" in listing.reply

    found = execute(db, user.id, "/kb search 重启流程")
    assert "运维手册" in found.reply

    deleted = execute(db, user.id, "/kb delete 运维手册")
    assert "已删除" in deleted.reply

    empty = execute(db, user.id, "/kb list")
    assert "为空" in empty.reply
