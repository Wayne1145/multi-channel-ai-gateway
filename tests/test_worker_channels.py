from wecom_ai_gateway.channels import registry
from wecom_ai_gateway.worker import register_worker_adapters


def test_worker_registers_clawbot_adapter():
    registry._adapters.pop("wechat_clawbot", None)

    register_worker_adapters()

    assert registry.get("wechat_clawbot").channel_key == "wechat_clawbot"