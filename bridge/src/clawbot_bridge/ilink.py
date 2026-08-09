"""腾讯微信 iLink Bot 的最小文本传输层。

HTTP 契约依据腾讯 MIT 许可的 ``@tencent-weixin/openclaw-weixin`` 2.4.6
独立实现；本模块不依赖 OpenClaw，也不包含任何第三方未授权实现的源码。
"""

import asyncio
import base64
import logging
import secrets
from dataclasses import dataclass

import httpx

PROTOCOL_REFERENCE_VERSION = "2.4.6"
APP_ID = "bot"
BOT_AGENT = "multi-channel-ai-gateway-clawbot/0.1.0"
LOGIN_ORIGIN = "https://ilinkai.weixin.qq.com"
log = logging.getLogger(__name__)


def _encode_client_version(version: str) -> str:
    """将语义版本编码为 iLink 使用的 24 位十进制版本号。"""
    parts = version.split(".")
    if len(parts) != 3 or any(not part.isdecimal() for part in parts):
        raise ValueError("iLink 客户端版本必须是 major.minor.patch")
    major, minor, patch = (int(part) & 0xFF for part in parts)
    return str((major << 16) | (minor << 8) | patch)


CLIENT_VERSION = _encode_client_version(PROTOCOL_REFERENCE_VERSION)


class SessionExpiredError(RuntimeError):
    """iLink 会话过期，需要重新扫码。"""


@dataclass(frozen=True)
class ILinkCredentials:
    bot_token: str
    base_url: str


@dataclass(frozen=True)
class ParsedInboundText:
    sender_id: str
    external_message_id: str
    context_token: str
    content: str
    raw: dict


def parse_inbound_text(raw: dict) -> ParsedInboundText | None:
    """只接受用户文本消息，并提取异步回复所需的精确 context_token。"""
    if raw.get("message_type") != 1:
        return None
    sender_id = raw.get("from_user_id")
    context_token = raw.get("context_token")
    if not isinstance(sender_id, str) or not sender_id:
        return None
    if not isinstance(context_token, str) or not context_token:
        return None

    content = ""
    for item in raw.get("item_list") or []:
        if item.get("type") == 1:
            candidate = (item.get("text_item") or {}).get("text")
            if isinstance(candidate, str) and candidate:
                content = candidate
                break
    if not content:
        return None

    raw_external_id = raw.get("client_id") or raw.get("message_id")
    if raw_external_id is None:
        return None
    return ParsedInboundText(
        sender_id=sender_id,
        external_message_id=str(raw_external_id),
        context_token=context_token,
        content=content,
        raw=raw,
    )


class ILinkClient:
    def __init__(
        self,
        credentials: ILinkCredentials | None = None,
        timeout_seconds: float = 45.0,
    ) -> None:
        if credentials is not None and not credentials.bot_token:
            raise ValueError("bot_token 不能为空")
        self._credentials = credentials
        self._timeout_seconds = timeout_seconds

    @staticmethod
    def _base_info() -> dict[str, str]:
        return {"channel_version": PROTOCOL_REFERENCE_VERSION, "bot_agent": BOT_AGENT}

    def _request_headers(self, *, authenticated: bool) -> dict[str, str]:
        decimal_uin = str(secrets.randbits(32)).encode("ascii")
        headers = {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": base64.b64encode(decimal_uin).decode("ascii"),
            "iLink-App-Id": APP_ID,
            "iLink-App-ClientVersion": CLIENT_VERSION,
        }
        if authenticated:
            if self._credentials is None:
                raise RuntimeError("缺少 iLink 凭据")
            headers["Authorization"] = f"Bearer {self._credentials.bot_token}"
        return headers

    async def _post_json(
        self,
        url: str,
        payload: dict,
        *,
        authenticated: bool,
        timeout: float | None = None,
    ) -> dict:
        async with httpx.AsyncClient(timeout=timeout or self._timeout_seconds) as client:
            response = await client.post(
                url,
                json=payload,
                headers=self._request_headers(authenticated=authenticated),
            )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise TypeError("iLink 响应不是 JSON 对象")
        return data

    async def start_login(self):
        """获取登录二维码；不等待扫码，避免阻塞网关的 start 请求。"""
        from .runtime import LoginPending

        url = f"{LOGIN_ORIGIN}/ilink/bot/get_bot_qrcode?bot_type=3"
        data = await self._post_json(
            url,
            {"local_token_list": []},
            authenticated=False,
            timeout=5.0,
        )
        qrcode = data.get("qrcode")
        qrcode_url = data.get("qrcode_img_content")
        if not isinstance(qrcode, str) or not qrcode:
            raise RuntimeError("iLink 登录响应缺少 qrcode")
        if not isinstance(qrcode_url, str) or not qrcode_url:
            raise RuntimeError("iLink 登录响应缺少二维码地址")
        return LoginPending(session_key=qrcode, qrcode_url=qrcode_url)

    async def wait_login(self, pending):
        """长轮询扫码状态，成功后仅在内存中返回敏感凭据。"""
        from .runtime import LoginSuccess

        base_url = LOGIN_ORIGIN
        while True:
            url = f"{base_url}/ilink/bot/get_qrcode_status"
            async with httpx.AsyncClient(timeout=40.0) as client:
                response = await client.get(url, params={"qrcode": pending.session_key})
                response.raise_for_status()
                data = response.json()
            status = data.get("status")
            if status == "confirmed":
                token = data.get("bot_token")
                account_id = data.get("ilink_bot_id")
                if not isinstance(token, str) or not token:
                    log.error(
                        "iLink login response invalid reason=missing_bot_token status=%s fields=%s",
                        status,
                        ",".join(sorted(str(key) for key in data)),
                    )
                    raise RuntimeError("扫码成功但缺少 bot_token")
                if not isinstance(account_id, str) or not account_id:
                    log.error(
                        "iLink login response invalid reason=missing_account_id status=%s fields=%s",
                        status,
                        ",".join(sorted(str(key) for key in data)),
                    )
                    raise RuntimeError("扫码成功但缺少 ilink_bot_id")
                return LoginSuccess(
                    bot_token=token,
                    base_url=str(data.get("baseurl") or base_url),
                    account_id=account_id,
                )
            if status == "expired":
                log.error(
                    "iLink login response invalid reason=qrcode_expired status=%s fields=%s",
                    status,
                    ",".join(sorted(str(key) for key in data)),
                )
                raise RuntimeError("登录二维码已过期")
            if status == "scaned_but_redirect" and data.get("redirect_host"):
                base_url = f"https://{data['redirect_host']}"
            await asyncio.sleep(1)

    async def get_updates(self, cursor: str) -> tuple[str, list[ParsedInboundText]]:
        if not self._credentials:
            raise RuntimeError("缺少 iLink 凭据")
        url = f"{self._credentials.base_url.rstrip('/')}/ilink/bot/getupdates"
        data = await self._post_json(
            url,
            {"get_updates_buf": cursor, "base_info": self._base_info()},
            authenticated=True,
        )
        if data.get("ret") == -14 or data.get("errcode") == -14:
            raise SessionExpiredError("iLink session expired")
        if data.get("ret") not in {None, 0}:
            raise RuntimeError(f"iLink getupdates failed: ret={data.get('ret')}")
        messages = [
            parsed
            for raw in data.get("msgs") or []
            if (parsed := parse_inbound_text(raw)) is not None
        ]
        return str(data.get("get_updates_buf") or cursor), messages

    async def send_text(self, *, to_user_id: str, context_token: str, text: str) -> str:
        if not self._credentials:
            raise RuntimeError("缺少 iLink 凭据")
        if not to_user_id or not context_token or not text:
            raise ValueError("to_user_id、context_token 和 text 均不能为空")
        client_id = f"mcag-clawbot-{secrets.token_hex(8)}"
        message = {
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": client_id,
            "message_type": 2,
            "message_state": 2,
            "context_token": context_token,
            "item_list": [{"type": 1, "text_item": {"text": text}}],
        }
        url = f"{self._credentials.base_url.rstrip('/')}/ilink/bot/sendmessage"
        data = await self._post_json(
            url,
            {"msg": message, "base_info": self._base_info()},
            authenticated=True,
        )
        if data.get("ret") not in {None, 0}:
            raise RuntimeError(f"iLink sendmessage failed: ret={data.get('ret')}")
        return client_id
