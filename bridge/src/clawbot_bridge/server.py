"""生产 ASGI 应用装配。"""

from .app import create_app
from .config import BridgeSettings
from .gateway import GatewayClient
from .ilink import ILinkClient
from .ilink_service import ILinkService
from .runtime import BridgeRuntime
from .state import EncryptedStateStore


def build_app(settings: BridgeSettings):
    gateway = GatewayClient(
        base_url=settings.gateway_base_url,
        bridge_token=settings.clawbot_bridge_token,
    )
    ilink_service = ILinkService(state_dir=settings.state_dir)
    runtime = BridgeRuntime(
        login_provider=ILinkClient(),
        gateway=gateway,
        ilink=ilink_service,
        state_store=EncryptedStateStore(
            settings.state_dir,
            settings.clawbot_bridge_token,
        ),
    )
    return create_app(runtime=runtime, bridge_token=settings.clawbot_bridge_token)


def create_server_app():
    """供 uvicorn `--factory` 调用，避免模块导入时读取或打印生产密钥。"""
    return build_app(BridgeSettings())
