"""知识库/RAG 测试：分块、加密文档、混合检索、解析与引用。"""

import io
import ipaddress
import zipfile
from unittest.mock import patch

import pytest
from docx import Document
from fastapi.testclient import TestClient
from pypdf import PdfWriter

from wecom_ai_gateway.commands import execute
from wecom_ai_gateway.db import SessionLocal
from wecom_ai_gateway.knowledge import (
    add_item,
    build_injection,
    chunk_text,
    deterministic_embedding,
    extract_document_text,
    fetch_public_document,
    migrate_legacy_items,
    search,
    vector_candidate_query,
)
from wecom_ai_gateway.main import app
from wecom_ai_gateway.models import Account, KnowledgeChunk, KnowledgeItem, User, UserSettings
from wecom_ai_gateway.security import hash_password

client = TestClient(app)


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


def test_knowledge_content_and_chunks_are_encrypted_with_embeddings(db):
    user = User(display_name="encrypted-kb", mode="self_service")
    db.add(user)
    db.flush()

    item = add_item(db, user.id, "私密手册", "极光计划的内部口令是晨星。")
    db.expire_all()
    stored = db.get(KnowledgeItem, item.id)
    chunks = db.query(KnowledgeChunk).filter_by(item_id=item.id).all()

    assert stored.content == ""
    assert "晨星" not in (stored.content_encrypted or "")
    assert stored.content_sha256
    assert chunks
    assert all("晨星" not in chunk.content_encrypted for chunk in chunks)
    assert all(len(chunk.embedding) == 256 for chunk in chunks)


def test_legacy_plaintext_items_are_migrated_to_encrypted_chunks(db):
    user = User(display_name="legacy-kb", mode="self_service")
    db.add(user)
    db.flush()
    item = KnowledgeItem(user_id=user.id, title="旧条目", content="旧版明文不应继续留在数据库")
    db.add(item)
    db.commit()

    migrated = migrate_legacy_items(db)
    db.expire_all()
    stored = db.get(KnowledgeItem, item.id)

    assert migrated == 1
    assert stored.content == ""
    assert "旧版明文" not in stored.content_encrypted
    assert db.query(KnowledgeChunk).filter_by(item_id=item.id).count() == 1


def test_existing_encrypted_items_with_legacy_plain_hashes_are_rekeyed(db):
    import hashlib

    user = User(display_name="legacy-hash", mode="self_service")
    db.add(user)
    db.flush()
    item = add_item(db, user.id, "旧指纹", "需要重新派生的正文")
    item.content_sha256 = hashlib.sha256("需要重新派生的正文".encode()).hexdigest()
    db.commit()

    migrated = migrate_legacy_items(db)
    db.expire_all()

    assert migrated == 1
    assert db.get(KnowledgeItem, item.id).content_sha256 != hashlib.sha256(
        "需要重新派生的正文".encode()
    ).hexdigest()


def test_deterministic_embedding_is_normalized_and_semantically_stable():
    first = deterministic_embedding("服务器部署手册")
    second = deterministic_embedding("服务器部署手册")

    assert first == second
    assert len(first) == 256
    assert abs(sum(value * value for value in first) - 1.0) < 1e-6


def test_postgres_candidate_query_uses_pgvector_cosine_operator():
    query = vector_candidate_query("user-1", deterministic_embedding("服务器"), 20)
    sql = str(query.compile(compile_kwargs={"literal_binds": True}))

    assert "<=>" in sql
    assert "knowledge_chunks.user_id = 'user-1'" in sql
    assert "LIMIT 20" in sql


def test_hybrid_search_returns_citations_and_source_metadata(db):
    user = User(display_name="citation-kb", mode="self_service")
    db.add(user)
    db.flush()
    add_item(
        db,
        user.id,
        "运维手册",
        "月光服务器的重启窗口是每周二凌晨三点。",
        source_type="markdown",
        source_name="ops.md",
    )

    results = search(db, user.id, "服务器什么时候重启", limit=3)
    injection = build_injection(
        db,
        user.id,
        "服务器什么时候重启",
        max_chunks=3,
        chunk_chars=800,
    )

    assert results[0]["title"] == "运维手册"
    assert results[0]["source_name"] == "ops.md"
    assert results[0]["citation"] == "[KB:运维手册#1]"
    assert "[KB:运维手册#1]" in injection


def test_extract_document_text_supports_txt_markdown_html_docx_and_pdf():
    assert extract_document_text(b"plain text", "text/plain", "a.txt") == "plain text"
    assert "标题" in extract_document_text("# 标题".encode(), "text/markdown", "a.md")
    html = extract_document_text(
        "<h1>标题</h1><script>secret()</script><p>正文</p>".encode(),
        "text/html",
        "a.html",
    )
    assert "标题" in html and "正文" in html and "secret" not in html

    document = Document()
    document.add_paragraph("Word 文档正文")
    docx_buf = io.BytesIO()
    document.save(docx_buf)
    assert "Word 文档正文" in extract_document_text(
        docx_buf.getvalue(),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "a.docx",
    )

    pdf = PdfWriter()
    pdf.add_blank_page(width=100, height=100)
    pdf_buf = io.BytesIO()
    pdf.write(pdf_buf)
    assert extract_document_text(pdf_buf.getvalue(), "application/pdf", "a.pdf") == ""


def test_docx_rejects_excessive_uncompressed_zip_payload():
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "x" * (60 * 1024 * 1024))

    with pytest.raises(ValueError, match="解压"):
        extract_document_text(
            payload.getvalue(),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "bomb.docx",
        )


def test_knowledge_hashes_and_embedding_are_keyed_not_plain_digests(db):
    import hashlib

    user = User(display_name="keyed-kb", mode="self_service")
    db.add(user)
    db.flush()
    content = "低熵内部口令"
    item = add_item(db, user.id, "密钥派生", content)
    chunk = db.query(KnowledgeChunk).filter_by(item_id=item.id).one()

    assert item.content_sha256 != hashlib.sha256(content.encode()).hexdigest()
    assert chunk.content_sha256 != hashlib.sha256(content.encode()).hexdigest()


def _knowledge_login(username: str) -> str:
    db = SessionLocal()
    user = User(display_name=username, mode="self_service")
    db.add(user)
    db.flush()
    db.add(UserSettings(user_id=user.id))
    db.add(
        Account(
            user_id=user.id,
            username=username,
            password_hash=hash_password("knowledge-pass-123"),
            role="user",
        )
    )
    db.commit()
    db.close()
    return client.post(
        "/api/auth/login",
        json={"username": username, "password": "knowledge-pass-123"},
    ).json()["token"]


def test_knowledge_upload_list_search_and_delete_are_user_scoped():
    token = _knowledge_login("knowledge_api")
    other = _knowledge_login("knowledge_other")
    headers = {"Authorization": f"Bearer {token}"}

    created = client.post(
        "/api/me/knowledge/upload",
        headers=headers,
        data={"title": "上传手册"},
        files={"file": ("guide.md", b"Moon database recovery steps", "text/markdown")},
    )
    assert created.status_code == 200
    item_id = created.json()["id"]
    rows = client.get("/api/me/knowledge", headers=headers).json()
    assert rows[0]["source_name"] == "guide.md"
    assert "content" not in rows[0] and "content_encrypted" not in rows[0]
    assert client.get(
        "/api/me/knowledge/search?q=database recovery", headers=headers
    ).json()[0]["item_id"] == item_id
    assert client.delete(
        f"/api/me/knowledge/{item_id}",
        headers={"Authorization": f"Bearer {other}"},
    ).status_code == 404
    assert client.delete(f"/api/me/knowledge/{item_id}", headers=headers).status_code == 200


def test_url_import_rejects_private_network_before_fetch():
    token = _knowledge_login("knowledge_url")

    with patch("wecom_ai_gateway.knowledge.httpx.AsyncClient") as request:
        response = client.post(
            "/api/me/knowledge/url",
            headers={"Authorization": f"Bearer {token}"},
            json={"title": "内网", "url": "https://127.0.0.1/private"},
        )

    assert response.status_code == 400
    request.assert_not_called()


def test_url_fetch_pins_validated_ip_and_preserves_tls_hostname():
    """校验后的公网 IP 必须直接用于连接，避免第二次 DNS 解析产生重绑定窗口。"""

    class FakeResponse:
        status_code = 200

        def __init__(self):
            self.headers = {"content-type": "text/plain"}

        def raise_for_status(self):
            return None

        async def aiter_bytes(self):
            yield b"safe document"

    class FakeStream:
        async def __aenter__(self):
            return FakeResponse()

        async def __aexit__(self, *args):
            return None

    class FakeClient:
        request = None

        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False
            assert kwargs["follow_redirects"] is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def stream(self, method, url, **kwargs):
            type(self).request = (method, url, kwargs)
            return FakeStream()

    address = ipaddress.ip_address("93.184.216.34")
    with (
        patch("wecom_ai_gateway.knowledge._resolve_public_addresses", return_value=[address]),
        patch("wecom_ai_gateway.knowledge.httpx.AsyncClient", FakeClient),
    ):
        data, mime, source = __import__("asyncio").run(
            fetch_public_document("https://example.com/guide?q=1")
        )

    assert data == b"safe document"
    assert mime == "text/plain"
    assert source == "https://example.com/guide"
    _, pinned_url, kwargs = FakeClient.request
    assert pinned_url == "https://93.184.216.34/guide?q=1"
    assert kwargs["headers"]["Host"] == "example.com"
    assert kwargs["extensions"]["sni_hostname"] == "example.com"


def test_url_fetch_stops_when_stream_exceeds_limit():
    """远端未给 Content-Length 时也必须流式限额，不能先把超大响应读入内存。"""

    class HugeResponse:
        status_code = 200

        def __init__(self):
            self.headers = {"content-type": "text/plain"}

        def raise_for_status(self):
            return None

        async def aiter_bytes(self):
            for _ in range(11):
                yield b"x" * (1024 * 1024)

    class HugeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def stream(self, *args, **kwargs):
            class Context:
                async def __aenter__(self):
                    return HugeResponse()

                async def __aexit__(self, *args):
                    return None

            return Context()

    with (
        patch(
            "wecom_ai_gateway.knowledge._resolve_public_addresses",
            return_value=[ipaddress.ip_address("93.184.216.34")],
        ),
        patch("wecom_ai_gateway.knowledge.httpx.AsyncClient", HugeClient),
        __import__("pytest").raises(ValueError, match="10MB"),
    ):
        __import__("asyncio").run(fetch_public_document("https://example.com/huge"))


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
