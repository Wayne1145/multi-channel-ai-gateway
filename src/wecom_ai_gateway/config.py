from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    app_env: str = "development"
    app_name: str = "WeCom AI Gateway"
    public_base_url: str = "http://localhost:8080"
    database_url: str = "sqlite:///./data/dev.db"
    redis_url: str = "redis://localhost:6379/0"
    admin_token: str = "dev-admin-token"
    identity_hmac_key: str = "dev-identity-hmac-key-change-me"
    secret_encryption_key: str = ""
    wecom_corp_id: str = ""
    wecom_secret: str = ""
    wecom_callback_token: str = ""
    wecom_encoding_aes_key: str = ""
    wecom_open_kfid: str = ""
    wecom_callback_path: str = "/wecom/kf/callback"
    default_provider: str = "openai-compatible"
    default_model: str = "deepseek-chat"
    default_system_prompt: str = "你是一个准确、友善且尊重隐私的人工智能助手。"
    openai_compatible_base_url: str = "https://api.deepseek.com/v1"
    openai_compatible_api_key: str = ""
    request_timeout_seconds: int = 90
    user_daily_token_quota: int = 100000


@lru_cache
def get_settings():
    return Settings()


settings = get_settings()
