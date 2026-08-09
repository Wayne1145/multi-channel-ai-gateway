"""桥接敏感状态的本地加密存储。

密钥从独立 bridge token 派生；网关数据库与日志不会接触 iLink 会话凭据。
"""

import base64
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .ilink import ILinkCredentials


@dataclass(frozen=True)
class StoredInstanceState:
    credentials: ILinkCredentials
    account_id: str
    context_tokens: dict[str, str]


class EncryptedStateStore:
    def __init__(self, state_dir: Path, bridge_token: str) -> None:
        key = base64.urlsafe_b64encode(hashlib.sha256(bridge_token.encode()).digest())
        self._fernet = Fernet(key)
        self._state_dir = state_dir

    def _path(self, instance_id: str) -> Path:
        safe_instance_id = instance_id.replace("/", "_").replace("..", "_")
        return self._state_dir / safe_instance_id / "session.encrypted"

    def save(self, instance_id: str, state: StoredInstanceState) -> None:
        payload = json.dumps(
            {
                "bot_token": state.credentials.bot_token,
                "base_url": state.credentials.base_url,
                "account_id": state.account_id,
                "context_tokens": state.context_tokens,
            },
            ensure_ascii=False,
        ).encode()
        encrypted = self._fernet.encrypt(payload)
        path = self._path(instance_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(".tmp")
        temp_path.write_bytes(encrypted)
        temp_path.chmod(0o600)
        temp_path.replace(path)

    def load(self, instance_id: str) -> StoredInstanceState | None:
        path = self._path(instance_id)
        try:
            payload = json.loads(self._fernet.decrypt(path.read_bytes()))
        except (FileNotFoundError, InvalidToken, json.JSONDecodeError, KeyError, TypeError):
            return None
        return StoredInstanceState(
            credentials=ILinkCredentials(
                bot_token=str(payload["bot_token"]),
                base_url=str(payload["base_url"]),
            ),
            account_id=str(payload["account_id"]),
            context_tokens={str(key): str(value) for key, value in payload.get("context_tokens", {}).items()},
        )

    def instance_ids(self) -> list[str]:
        """列出具备加密会话文件的安全实例目录。"""
        if not self._state_dir.is_dir():
            return []
        return sorted(
            path.parent.name
            for path in self._state_dir.glob("*/session.encrypted")
            if path.is_file()
        )

    def clear(self, instance_id: str) -> None:
        try:
            self._path(instance_id).unlink()
        except FileNotFoundError:
            pass
