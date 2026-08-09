from pathlib import Path

from clawbot_bridge.ilink import ILinkCredentials
from clawbot_bridge.state import EncryptedStateStore, StoredInstanceState


def test_state_store_encrypts_credentials_and_context_tokens(tmp_path: Path) -> None:
    store = EncryptedStateStore(tmp_path, "bridge-secret-at-least-16")
    state = StoredInstanceState(
        credentials=ILinkCredentials(
            bot_token="private-bot-token",
            base_url="https://ilinkai.weixin.qq.com",
        ),
        account_id="bot@im.bot",
        context_tokens={"incoming-1": "exact-context-token"},
    )

    store.save("instance-1", state)

    encrypted = (tmp_path / "instance-1" / "session.encrypted").read_bytes()
    assert b"private-bot-token" not in encrypted
    assert b"exact-context-token" not in encrypted
    assert store.load("instance-1") == state


def test_state_store_clear_removes_only_selected_instance(tmp_path: Path) -> None:
    store = EncryptedStateStore(tmp_path, "bridge-secret-at-least-16")
    state = StoredInstanceState(
        credentials=ILinkCredentials("token", "https://ilinkai.weixin.qq.com"),
        account_id="bot@im.bot",
        context_tokens={},
    )
    store.save("instance-1", state)
    store.save("instance-2", state)

    store.clear("instance-1")

    assert store.load("instance-1") is None
    assert store.load("instance-2") == state