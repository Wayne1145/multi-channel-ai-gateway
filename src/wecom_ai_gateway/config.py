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
    unconfigured_model_message: str = "模型服务正在配置中。命令功能仍可使用，发送 /help 查看可用设置。"
    worker_poll_seconds: float = 2.0
    task_max_attempts: int = 5
    task_retry_base_seconds: int = 5
    task_retry_max_seconds: int = 300
    task_lock_timeout_seconds: int = 300
    sync_lock_seconds: int = 120
    # 平台默认运行模式：self_service（用户自足）| managed（统一管理）
    platform_mode: str = "self_service"
    # 全局单用户开关：true 时忽略平台/用户模式，一切指令放行（.env 手动开启）
    single_user_mode: bool = False


@lru_cache
def get_settings():
    return Settings()


settings = get_settings()
