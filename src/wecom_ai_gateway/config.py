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
    # 模型推理可能包含较长的思考阶段。默认 300 秒避免把正常长响应误判为失败；
    # Worker 会持续续租任务，避免该等待期间被其他 Worker 重复领取。
    request_timeout_seconds: int = 300
    # 推理模型将思考 token 与最终回答共同计入 max_tokens；2048 容易只产出思考而没有最终文本。
    # 用户未单独设置时给 4096，确保复杂请求仍有空间生成可发送答案。
    default_max_tokens: int = 4096
    user_daily_token_quota: int = 100000
    unconfigured_model_message: str = "模型服务正在配置中。命令功能仍可使用，发送 /help 查看可用设置。"
    worker_poll_seconds: float = 2.0
    task_max_attempts: int = 5
    task_retry_base_seconds: int = 5
    task_retry_max_seconds: int = 300
    task_lock_timeout_seconds: int = 300
    sync_lock_seconds: int = 120
    # 可选的微信 ClawBot HTTP 桥接服务。留空即不启用个人微信渠道。
    # 此桥接地址不应携带凭据；认证令牌仅从环境变量读取，绝不落库或出现在管理 API。
    clawbot_bridge_base_url: str = ""
    clawbot_bridge_token: str = ""
    clawbot_request_timeout_seconds: int = 30
    # 平台默认运行模式：self_service（用户自足）| managed（统一管理）
    platform_mode: str = "self_service"
    # 全局单用户开关：true 时忽略平台/用户模式，一切指令放行（.env 手动开启）
    single_user_mode: bool = False
    # 媒体消息安全生命周期：白名单、大小上限、保留时长（小时）。
    # 网关只记录元数据，不主动下载外部文件；到期后由 Worker 清理记录。
    media_allowed_mime_types: str = (
        "image/jpeg,image/png,image/gif,image/webp,audio/mpeg,audio/ogg,audio/wav,"
        "application/pdf,text/plain,application/octet-stream"
    )
    media_max_size_bytes: int = 20 * 1024 * 1024
    media_retention_hours: int = 168


@lru_cache
def get_settings():
    return Settings()


settings = get_settings()
