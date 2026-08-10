import base64
import hashlib
import logging
import struct
import time
from dataclasses import dataclass
from urllib.parse import urlsplit
from xml.etree import ElementTree

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .config import settings
from .redaction import redact_error

log = logging.getLogger(__name__)


@dataclass
class CallbackEvent:
    token: str
    open_kfid: str
    event: str
    msg_id: str = ""
    status: str = ""
    fail_reason: str = ""


def verify_signature(signature, timestamp, nonce, encrypted):
    expected = hashlib.sha1(
        "".join(sorted([settings.wecom_callback_token, timestamp, nonce, encrypted])).encode()
    ).hexdigest()
    return bool(signature) and expected == signature


def decrypt(encrypted: str) -> bytes:
    key = base64.b64decode(settings.wecom_encoding_aes_key + "=")
    raw = base64.b64decode(encrypted)
    dec = Cipher(algorithms.AES(key), modes.CBC(key[:16])).decryptor()
    padded = dec.update(raw) + dec.finalize()
    pad = padded[-1]
    if not 1 <= pad <= 32 or padded[-pad:] != bytes([pad]) * pad:
        raise ValueError("invalid padding")
    plain = padded[:-pad]
    n = struct.unpack("!I", plain[16:20])[0]
    message = plain[20 : 20 + n]
    receiver = plain[20 + n :].decode()
    if receiver != settings.wecom_corp_id:
        raise ValueError("CorpID mismatch")
    return message


def parse_callback(body: bytes, signature: str, timestamp: str, nonce: str) -> CallbackEvent:
    root = ElementTree.fromstring(body)
    encrypted = root.findtext("Encrypt") or ""
    if not verify_signature(signature, timestamp, nonce, encrypted):
        raise ValueError("signature mismatch")
    event = ElementTree.fromstring(decrypt(encrypted))
    return CallbackEvent(
        event.findtext("Token") or "",
        event.findtext("OpenKfId") or "",
        event.findtext("Event") or event.findtext("MsgType") or "unknown",
        event.findtext("MsgId") or "",
        event.findtext("Status") or "",
        event.findtext("FailReason") or "",
    )


class WeComClient:
    def __init__(self):
        self._token = ""
        self._expires = 0.0

    async def access_token(self, force=False):
        if self._token and self._expires > time.time() + 120 and not force:
            return self._token
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(
                "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
                params={"corpid": settings.wecom_corp_id, "corpsecret": settings.wecom_secret},
            )
            r.raise_for_status()
            d = r.json()
        if d.get("errcode") != 0:
            raise RuntimeError(f"gettoken failed: {d.get('errcode')} {d.get('errmsg')}")
        self._token = d["access_token"]
        self._expires = time.time() + int(d.get("expires_in", 7200))
        return self._token

    async def call(self, path, payload, retry=True):
        token = await self.access_token()
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.post(
                    "https://qyapi.weixin.qq.com" + path,
                    params={"access_token": token},
                    json=payload,
                )
                r.raise_for_status()
                d = r.json()
        except httpx.HTTPError as exc:
            # httpx 的异常文本可能包含带 access_token 的请求 URL；日志/死信一律脱敏。
            raise RuntimeError(redact_error(exc)) from exc
        if d.get("errcode") in {40014, 42001} and retry:
            await self.access_token(True)
            return await self.call(path, payload, False)
        return d

    async def sync(self, callback_token, open_kfid, cursor=""):
        return await self.call(
            "/cgi-bin/kf/sync_msg",
            {
                "cursor": cursor,
                "token": callback_token,
                "limit": 1000,
                "voice_format": 0,
                "open_kfid": open_kfid or settings.wecom_open_kfid,
            },
        )

    async def send_text(self, open_kfid, user_id, content):
        d = await self.call(
            "/cgi-bin/kf/send_msg",
            {
                "touser": user_id,
                "open_kfid": open_kfid,
                "msgtype": "text",
                "text": {"content": content[:2048]},
            },
        )
        if d.get("errcode") != 0:
            raise RuntimeError(f"send_msg failed: {d.get('errcode')} {d.get('errmsg')}")
        return d.get("msgid")

    async def send_media(self, open_kfid, user_id, media: dict):
        """发送客服媒体消息（image/voice/file）。

        企微客服消息 media_id 优先直发；没有 media_id 时先上传临时素材
        （/cgi-bin/media/upload）再发。返回渠道侧 msgid。
        """
        media_type = str(media.get("media_type") or "image")
        if media_type not in {"image", "voice", "file"}:
            raise ValueError(f"不支持的客服媒体类型：{media_type}")
        media_id = media.get("media_id") or ""
        if not media_id and media.get("url"):
            media_id = await self.upload_media(media_type, media["url"])
        if not media_id:
            raise ValueError("发送客服媒体需要 media_id 或可下载的 url")
        d = await self.call(
            "/cgi-bin/kf/send_msg",
            {
                "touser": user_id,
                "open_kfid": open_kfid,
                "msgtype": media_type,
                media_type: {"media_id": media_id},
            },
        )
        if d.get("errcode") != 0:
            raise RuntimeError(f"send_msg failed: {d.get('errcode')} {d.get('errmsg')}")
        return d.get("msgid")

    async def upload_media(self, media_type: str, url: str) -> str:
        """从 url 下载文件并上传为企微临时素材，返回 media_id。

        只允许 https；拒绝带凭据的 URL（httpx 会校验），避免 SSRF 与凭据泄漏。
        """
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.username or parsed.password:
            raise ValueError("媒体 url 必须为无凭据的 https 地址")
        token = await self.access_token()
        try:
            async with httpx.AsyncClient(timeout=settings.wecom_upload_timeout_seconds, follow_redirects=True) as c:
                r = await c.get(url)
                r.raise_for_status()
                upload = await c.post(
                    "https://qyapi.weixin.qq.com/cgi-bin/media/upload",
                    params={"access_token": token, "type": media_type},
                    files={"media": (f"media.{media_type}", r.content)},
                )
                upload.raise_for_status()
                d = upload.json()
        except httpx.HTTPError as exc:
            raise RuntimeError(redact_error(exc)) from exc
        if d.get("errcode") != 0:
            raise RuntimeError(f"media/upload failed: {d.get('errcode')} {d.get('errmsg')}")
        return d["media_id"]


client = WeComClient()
