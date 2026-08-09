from pathlib import Path

import pytest

from clawbot_bridge.config import BridgeSettings
from clawbot_bridge.server import build_app


def test_bridge_settings_require_real_secrets(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="CLAWBOT_BRIDGE_TOKEN"):
        BridgeSettings(
            clawbot_bridge_token="",
            gateway_base_url="http://api:8080",
            state_dir=tmp_path,
        )


def test_build_app_exposes_health_without_leaking_configuration(tmp_path: Path) -> None:
    settings = BridgeSettings(
        clawbot_bridge_token="bridge-secret-at-least-16",
        gateway_base_url="http://api:8080",
        state_dir=tmp_path,
    )
    app = build_app(settings)

    assert app.title == "Multi-Channel ClawBot Bridge"
    assert "bridge-secret-at-least-16" not in repr(app)
