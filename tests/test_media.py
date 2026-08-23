"""媒体消息安全生命周期测试：元数据记录、白名单/大小校验、TTL 清理。"""

import pytest

from wecom_ai_gateway import media as media_service
from wecom_ai_gateway.channels import ChannelMessage
from wecom_ai_gateway.models import MediaAsset, Message, User
from wecom_ai_gateway.services import ingest_channel_message


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
