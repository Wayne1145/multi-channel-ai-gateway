"""媒体消息安全生命周期。

原则：
- 默认只记录媒体**元数据**（类型/大小/哈希/渠道定位）；
- 受信 Bridge 可提交已解密、限额的文档字节。API 只提取文本并 Fernet 加密，
  不落盘原始文件，也不接收 CDN URL/AES key；
- 类型与大小在白名单/上限内才标记 stored；超限记录为 rejected 便于审计；
- expires_at 到期后由 Worker 周期调用 ``cleanup_expired_media`` 删除记录；
- 管理 API 只返回脱敏元数据，绝不返回可能携带渠道凭据的 storage_key/URL。
"""

import base64
import hashlib
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .config import settings
from .knowledge import extract_document_text_isolated
from .models import MediaAsset
from .runtime_settings import get_runtime_value
from .security import encrypt_secret

# 媒体类型 → 允许的 mime 前缀（宽松匹配，允许带 charset 等参数）
_ALLOWED_PREFIXES = {
    "image": ("image/",),
    "voice": ("audio/", "application/octet-stream"),
    "file": ("application/", "text/", "audio/", "image/"),
}
_MAX_INLINE_DOCUMENT_BYTES = 10 * 1024 * 1024
_MAX_INLINE_DOCUMENT_BASE64_CHARS = ((_MAX_INLINE_DOCUMENT_BYTES + 2) // 3) * 4


def _allowed_mimes(db: Session | None = None) -> set[str]:
    raw = get_runtime_value(db, "media_allowed_mime_types") if db else settings.media_allowed_mime_types
    return {m.strip().lower() for m in str(raw).split(",") if m.strip()}


def _classify(mime: str | None, media_type: str | None) -> str:
    """由渠道媒体类型或 mime 推断大类：image | voice | file。"""
    if media_type in {"image", "voice", "file"}:
        return media_type
    mime = (mime or "").lower()
    if mime.startswith("image/"):
        return "image"
    if mime.startswith(("audio/", "video/")):
        return "voice"
    return "file"


def _sha256_hex(data: bytes | None) -> str | None:
    if not data:
        return None
    return hashlib.sha256(data).hexdigest()


def validate_media_item(item: dict, db: Session | None = None) -> tuple[bool, str | None]:
    """校验单个媒体条目：(是否通过, 拒绝原因)。

    仅当显式给出 mime 且不在白名单内时拒绝；未提供 mime 的条目按
    application/octet-stream 处理，避免误杀渠道细节差异。
    """
    mime = (item.get("mime") or item.get("content_type") or "").strip().lower()
    size = int(item.get("size_bytes") or 0)
    max_size = int(get_runtime_value(db, "media_max_size_bytes")) if db else settings.media_max_size_bytes
    if size > max_size:
        return False, f"size_exceeds_limit:{max_size}"
    if mime and mime not in _allowed_mimes(db):
        return False, f"mime_not_allowed:{mime}"
    return True, None


def record_media_items(
    db: Session, message_id: str, channel: str, media: list[dict]
) -> list[MediaAsset]:
    """把入站媒体条目转为 MediaAsset 记录（含校验与哈希）。

    media 条目格式：{media_type|type, mime|content_type, size_bytes, filename,
    url|file_id|media_id, data(可选 bytes)}。管理端不会看到 storage_key。
    """
    rows: list[MediaAsset] = []
    expires = datetime.now(UTC) + timedelta(
        hours=int(get_runtime_value(db, "media_retention_hours"))
    )
    for item in media or []:
        if not isinstance(item, dict):
            continue
        mime = (item.get("mime") or item.get("content_type") or "").strip().lower() or None
        media_type = _classify(mime, item.get("media_type") or item.get("type"))
        size = int(item.get("size_bytes") or 0)
        ok, reason = validate_media_item(item, db)
        document_text = ""
        data: bytes | None = None
        encoded = item.get("data_base64")
        if encoded:
            encoded_text = str(encoded)
            if len(encoded_text) > _MAX_INLINE_DOCUMENT_BASE64_CHARS:
                ok, reason = False, "size_exceeds_limit:10485760"
            try:
                data = base64.b64decode(encoded_text, validate=True) if ok else None
            except (ValueError, TypeError):
                ok, reason = False, "invalid_base64"
            if data is not None and len(data) > _MAX_INLINE_DOCUMENT_BYTES:
                ok, reason = False, "size_exceeds_limit:10485760"
            if data is not None:
                # Bridge 报文大小只作预检；最终以解码后的真实字节数执行平台限额和审计。
                size = len(data)
                runtime_limit = int(get_runtime_value(db, "media_max_size_bytes"))
                if size > runtime_limit:
                    ok, reason = False, f"size_exceeds_limit:{runtime_limit}"
            if ok and data is not None and media_type == "file":
                try:
                    document_text = extract_document_text_isolated(
                        data,
                        mime or "application/octet-stream",
                        str(item.get("filename") or "file"),
                    )
                except Exception:  # noqa: BLE001 - 不可信文档解析失败必须降级为拒绝，不能击穿入站循环
                    ok, reason = False, "document_parse_failed"
        storage_key = item.get("url") or item.get("file_id") or item.get("media_id")
        row = MediaAsset(
            message_id=message_id,
            channel=channel,
            media_type=media_type,
            mime=mime,
            size_bytes=size,
            filename=(item.get("filename") or "")[:255] or None,
            sha256=_sha256_hex(data or item.get("data")),
            content_encrypted=encrypt_secret(document_text) if ok and data is not None else None,
            content_chars=len(document_text),
            storage_key=str(storage_key)[:2000] if storage_key else None,
            status="stored" if ok else "rejected",
            rejected_reason=reason,
            expires_at=expires,
        )
        db.add(row)
        rows.append(row)
    return rows


def cleanup_expired_media(db: Session, now: datetime | None = None) -> int:
    """删除超过保留时长的媒体元数据记录，返回删除条数。"""
    cutoff = now or datetime.now(UTC)
    count = len(list(db.scalars(select(MediaAsset.id).where(MediaAsset.expires_at < cutoff))))
    if count:
        db.execute(delete(MediaAsset).where(MediaAsset.expires_at < cutoff))
    return count


def list_media_metadata(db: Session, limit: int = 100) -> list[dict]:
    """管理 API 用：只返回安全元数据，不返回 storage_key。"""
    rows = db.scalars(
        select(MediaAsset).order_by(MediaAsset.created_at.desc()).limit(min(limit, 500))
    )
    return [
        {
            "id": row.id,
            "message_id": row.message_id,
            "channel": row.channel,
            "media_type": row.media_type,
            "mime": row.mime,
            "size_bytes": row.size_bytes,
            "filename": row.filename,
            "sha256": row.sha256,
            "status": row.status,
            "rejected_reason": row.rejected_reason,
            "expires_at": row.expires_at,
            "created_at": row.created_at,
        }
        for row in rows
    ]
