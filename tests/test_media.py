"""媒体消息安全生命周期测试：元数据、加密附件正文、白名单/大小与 TTL。"""

import base64

import pytest

from wecom_ai_gateway import media as media_service
from wecom_ai_gateway.channels import ChannelMessage
from wecom_ai_gateway.models import MediaAsset, Message, User
from wecom_ai_gateway.security import decrypt_secret
from wecom_ai_gateway.services import _attachment_context, ingest_channel_message


def _media_item(**overrides):
    item = {
        "media_type": "image",
        "mime": "image/png",
        "size_bytes": 1024,
        "filename": "a.png",
        "media_id": "MEDIA_123",
    }
    item.update(overrides)
    return item


def test_validate_media_accepts_whitelist_and_rejects_oversize(monkeypatch):
    monkeypatch.setattr(media_service.settings, "media_max_size_bytes", 1024)
    ok, reason = media_service.validate_media_item(_media_item())
    assert ok and reason is None
    ok, reason = media_service.validate_media_item(_media_item(size_bytes=2048))
    assert not ok and reason and "size" in reason
    ok, reason = media_service.validate_media_item(_media_item(mime="application/x-msdownload"))
    assert not ok and reason and "mime" in reason


def test_record_media_items_stores_metadata_without_url_exposure(db):
    user = User()
    db.add(user)
    db.flush()
    message = Message(
        user_id=user.id,
        channel="wechat_clawbot",
        external_message_id="media-msg-1",
        direction="inbound",
        message_type="image",
        content=None,
        status="ignored",
    )
    db.add(message)
    db.flush()
    items = [
        _media_item(media_id="MEDIA_1", data=b"hello"),
        _media_item(media_type="file", mime="application/octet-stream", media_id="MEDIA_2"),
    ]
    rows = media_service.record_media_items(db, message.id, "wechat_clawbot", items)
    db.commit()
    assert len(rows) == 2
    stored = db.query(MediaAsset).filter_by(message_id=message.id).all()
    assert len(stored) == 2
    # 哈希已记录；管理列表不暴露 storage_key
    assert any(r.sha256 for r in stored)
    view = media_service.list_media_metadata(db)
    assert view and "storage_key" not in view[0]
    assert "media_id" not in view[0]


def test_inline_media_rechecks_decoded_size_and_records_truthful_size(db, monkeypatch):
    user = User()
    db.add(user)
    db.flush()
    message = Message(
        user_id=user.id,
        channel="wechat_clawbot",
        external_message_id="media-real-size",
        direction="inbound",
        message_type="file",
        status="ignored",
    )
    db.add(message)
    db.flush()
    monkeypatch.setattr(media_service.settings, "media_max_size_bytes", 4)

    row = media_service.record_media_items(
        db,
        message.id,
        "wechat_clawbot",
        [
            _media_item(
                media_type="file",
                mime="text/plain",
                filename="oversize.txt",
                size_bytes=1,
                data_base64=base64.b64encode(b"12345").decode(),
            )
        ],
    )[0]

    assert row.size_bytes == 5
    assert row.status == "rejected"
    assert row.rejected_reason == "size_exceeds_limit:4"


def test_cleanup_expired_media_removes_only_expired(db):
    user = User()
    db.add(user)
    db.flush()
    message = Message(
        user_id=user.id,
        channel="wecom_kf",
        external_message_id="media-expire",
        direction="inbound",
        message_type="image",
        status="ignored",
    )
    db.add(message)
    db.flush()
    import datetime

    now = datetime.datetime.now(datetime.UTC)
    fresh = MediaAsset(
        message_id=message.id, channel="wecom_kf", media_type="image",
        size_bytes=1, expires_at=now + datetime.timedelta(hours=10), status="stored",
    )
    expired = MediaAsset(
        message_id=message.id, channel="wecom_kf", media_type="image",
        size_bytes=1, expires_at=now - datetime.timedelta(hours=1), status="stored",
    )
    db.add_all([fresh, expired])
    db.commit()
    assert media_service.cleanup_expired_media(db, now=now) == 1
    db.commit()
    remaining = db.query(MediaAsset).all()
    assert [r.id for r in remaining] == [fresh.id]


@pytest.mark.anyio
def test_generic_channel_ingest_records_media_for_non_text_message(db):
    incoming = ChannelMessage(
        channel="wechat_clawbot",
        instance_id="instance-media",
        sender_id="user-media",
        external_message_id="media-msg-2",
        message_type="image",
        content=None,
        media=[_media_item(media_id="MEDIA_9")],
        raw={"source": "bridge"},
    )
    row = ingest_channel_message(db, incoming)
    db.commit()
    assert row is not None
    # 图片消息可回复（走多模态模型），因此进入 queued；语音/文件仍为 ignored
    assert row.status == "queued"
    assets = db.query(MediaAsset).filter_by(message_id=row.id).all()
    assert len(assets) == 1
    assert assets[0].media_type == "image"
    # metadata_json 只保留汇总信息，不滞留原始 URL/凭据
    assert "MEDIA_9" not in str(row.metadata_json)


def test_clawbot_pdf_is_parsed_encrypted_and_available_to_followup_text(db):
    import io

    from pypdf import PdfWriter

    pdf = PdfWriter()
    pdf.add_blank_page(width=100, height=100)
    buf = io.BytesIO()
    pdf.write(buf)
    file_row = ingest_channel_message(
        db,
        ChannelMessage(
            channel="wechat_clawbot",
            instance_id="instance-doc",
            sender_id="user-doc",
            external_message_id="doc-file",
            message_type="text",
            content="",
            media=[
                {
                    "media_type": "file",
                    "mime": "application/pdf",
                    "filename": "manual.pdf",
                    "size_bytes": len(buf.getvalue()),
                    "data_base64": base64.b64encode(buf.getvalue()).decode(),
                }
            ],
            raw={},
        ),
    )
    db.commit()

    assert file_row is not None
    assert file_row.status == "ignored"
    asset = db.query(MediaAsset).filter_by(message_id=file_row.id).one()
    assert asset.content_encrypted is not None
    assert asset.storage_key is None
    assert buf.getvalue()[:8].hex() not in str(asset.content_encrypted)
    assert decrypt_secret(asset.content_encrypted) == ""


def test_followup_text_receives_recent_encrypted_attachment_context(db):
    file_row = ingest_channel_message(
        db,
        ChannelMessage(
            channel="wechat_clawbot",
            instance_id="instance-doc-context",
            sender_id="user-doc-context",
            external_message_id="doc-text-file",
            message_type="text",
            content="",
            media=[
                {
                    "media_type": "file",
                    "mime": "text/plain",
                    "filename": "manual.txt",
                    "size_bytes": 30,
                    "data_base64": base64.b64encode("月光维修口令是银钥匙".encode()).decode(),
                }
            ],
            raw={},
        ),
    )
    followup = ingest_channel_message(
        db,
        ChannelMessage(
            channel="wechat_clawbot",
            instance_id="instance-doc-context",
            sender_id="user-doc-context",
            external_message_id="doc-question",
            message_type="text",
            content="总结这个文档",
            media=[],
            raw={},
        ),
    )
    db.commit()

    context = _attachment_context(db, followup)

    assert file_row is not None and followup is not None
    assert file_row.status == "ignored"
    assert followup.status == "queued"
    assert "manual.txt" in context
    assert "月光维修口令是银钥匙" in context
    assert "不可信附件资料" in context


def test_pdf_with_text_is_extracted_then_encrypted(db):
    """最小合法 PDF fixture 带可提取文本，避免只验证空白 PDF。"""
    stream = b"BT /F1 12 Tf 72 720 Td (Moon PDF Summary Marker) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    pdf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(pdf))
        pdf.extend(f"{index} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(pdf)
    pdf.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    pdf.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode())
    pdf.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )

    row = ingest_channel_message(
        db,
        ChannelMessage(
            channel="wechat_clawbot",
            instance_id="instance-pdf-text",
            sender_id="user-pdf-text",
            external_message_id="pdf-text-file",
            content="",
            media=[
                {
                    "media_type": "file",
                    "mime": "application/pdf",
                    "filename": "text.pdf",
                    "size_bytes": len(pdf),
                    "data_base64": base64.b64encode(pdf).decode(),
                }
            ],
        ),
    )
    db.commit()

    asset = db.query(MediaAsset).filter_by(message_id=row.id).one()
    assert asset.content_encrypted
    assert "Moon PDF Summary Marker" in decrypt_secret(asset.content_encrypted)


# ---------- 出站媒体发送 ----------


@pytest.mark.anyio
async def test_wecom_upload_media_rejects_insecure_url():
    from wecom_ai_gateway.wecom import client as wecom_client

    with pytest.raises(ValueError):
        await wecom_client.upload_media("image", "http://example.com/a.png")
    with pytest.raises(ValueError):
        await wecom_client.upload_media("image", "https://user:pass@example.com/a.png")


@pytest.mark.anyio
async def test_wecom_send_media_rejects_unsupported_type():
    from wecom_ai_gateway.wecom import client as wecom_client

    with pytest.raises(ValueError):
        await wecom_client.send_media("kfid", "uid", {"media_type": "video"})


@pytest.mark.anyio
async def test_wecom_send_media_requires_media_id_or_url(monkeypatch):
    from wecom_ai_gateway.wecom import client as wecom_client

    # 无 media_id 且无 url → 直接 ValueError，不应触发任何上传
    async def _no_upload(*args, **kwargs):
        raise AssertionError("不应触发上传")

    original_upload = wecom_client.upload_media
    monkeypatch.setattr(wecom_client, "upload_media", _no_upload)
    with pytest.raises(ValueError):
        await wecom_client.send_media("kfid", "uid", {"media_type": "image"})

    # url 非 https → upload_media 的 URL 校验拒绝（不 monkeypatch）
    monkeypatch.setattr(wecom_client, "upload_media", original_upload)
    with pytest.raises(ValueError):
        await wecom_client.send_media("kfid", "uid", {"media_type": "image", "url": "ftp://x/y"})
