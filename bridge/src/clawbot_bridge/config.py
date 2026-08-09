"""桥接服务环境配置。"""

from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class BridgeSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    clawbot_bridge_token: str
    gateway_base_url: str = "http://api:8080"
    state_dir: Path = Path("/data")

    @model_validator(mode="after")
    def validate_secret(self):
        if len(self.clawbot_bridge_token.strip()) < 16:
            raise ValueError("CLAWBOT_BRIDGE_TOKEN 必须是至少 16 字符的随机密钥")
        return self
