"""平台运行时设置注册表。

设置持久化在 ``platform_config`` 表（复用 0003 迁移，无需新表）。
读取优先级：数据库已校验值 > ``.env`` 默认（settings 单例）> spec 默认。

设计要点：
- 每个设置项声明类型、上下限、枚举、敏感标记、分组与说明；
- 保存前整体校验，任何一项非法都拒绝落库；
- 敏感项（API Key / Token / Secret）只读：后台只显示 configured 状态，
  内容永远只来自环境变量；
- ``platform_mode`` 特例映射到既有 ``platform_config['mode']``，
  保持 ``policy.resolve_user_mode`` 的读取兼容；
- 连接类参数（数据库/Redis）不进入本注册表，保持 .env 专属。
"""

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .models import PlatformConfig, SettingOverride
from .security import encrypt_secret


@dataclass(frozen=True)
class SettingSpec:
    key: str
    group: str
    label: str
    type: str  # int | float | bool | str | select | secret
    default: Any = None
    env_attr: str | None = None
    min: float | None = None
    max: float | None = None
    options: tuple[str, ...] | None = None
    secret: bool = False
    write_only: bool = False
    editable: bool = True
    description: str = ""
    unit: str = ""


SPECS: list[SettingSpec] = [
    # ---------- 1. 基础与公告 ----------
    SettingSpec("platform_name", "general", "平台名称", "str", env_attr="app_name",
                max=80, description="显示在管理后台标题与系统提示中的平台名称。"),
    SettingSpec("public_base_url", "general", "公网基础地址", "str", env_attr="public_base_url",
                editable=False, description="对外回调地址，修改需同步反代与企微后台，仅展示。"),
    SettingSpec("announcement", "general", "系统公告", "str", default="", max=2000,
                description="显示在登录页与用户面板的公告；留空表示不展示。"),
    SettingSpec("maintenance_mode", "general", "维护模式", "bool", default=False,
                description="开启后普通用户的新消息不进入 AI 处理，改为回复维护提示；管理员后台不受影响。"),
    SettingSpec("maintenance_message", "general", "维护提示文案", "str",
                default="系统维护中，请稍后再试。", max=500,
                description="维护模式下回复给普通用户新消息的提示文案。"),
    # ---------- 2. 模型与供应商 ----------
    SettingSpec("default_provider", "model", "默认供应商", "str", env_attr="default_provider",
                max=80, description="未配置 BYOK 用户使用的供应商标识。"),
    SettingSpec("default_model", "model", "默认模型", "str", env_attr="default_model",
                max=160, description="用户未单独指定时的模型名。"),
    SettingSpec("default_system_prompt", "model", "默认人设", "str", env_attr="default_system_prompt",
                max=4000, description="用户未设置人设时的系统提示。"),
    SettingSpec("openai_compatible_base_url", "model", "兼容接口地址", "str",
                env_attr="openai_compatible_base_url", max=500, editable=False,
                description="平台默认 OpenAI 兼容接口地址，仅展示。"),
    SettingSpec("openai_compatible_api_key", "model", "兼容接口密钥", "secret",
                env_attr="openai_compatible_api_key", secret=True,
                description="平台模型密钥，仅显示是否已配置。"),
    SettingSpec("request_timeout_seconds", "model", "模型请求超时（秒）", "int",
                env_attr="request_timeout_seconds", min=30, max=900, unit="秒",
                description="等待模型完整响应的最大秒数；Worker 会在此期间续租任务。"),
    SettingSpec("default_max_tokens", "model", "默认最大输出 Tokens", "int",
                env_attr="default_max_tokens", min=256, max=32768, unit="tokens",
                description="用户未设置时的最大输出预算。"),
    SettingSpec("unconfigured_model_message", "model", "模型未配置提示", "str",
                env_attr="unconfigured_model_message", max=500,
                description="平台未配置模型凭据时返回给用户的提示。"),
    # ---------- 3. 用户与账号 ----------
    SettingSpec("allow_public_registration", "account", "开放公开注册", "bool",
                env_attr="allow_public_registration",
                description="允许任何人注册账号；默认关闭，由管理员分配。"),
    SettingSpec("platform_mode", "account", "平台默认模式", "select",
                env_attr="platform_mode", options=("self_service", "managed"),
                description="self_service=用户自足；managed=统一管理（可被用户级覆盖）。"),
    SettingSpec("single_user_mode", "account", "全局单用户模式", "bool",
                env_attr="single_user_mode", editable=False,
                description="由 .env 手动开启，忽略平台/用户模式，仅展示。"),
    SettingSpec("auth_session_days", "account", "登录会话有效期（天）", "int",
                env_attr="auth_session_days", min=1, max=90, unit="天",
                description="普通用户与管理员的网页登录会话保持天数。"),
    SettingSpec("password_min_length", "account", "密码最小长度", "int", default=10, min=6, max=64,
                unit="字符", description="新密码与重置密码的最少字符数。"),
    SettingSpec("login_max_attempts", "account", "登录失败锁定阈值", "int", default=5, min=1, max=100,
                unit="次", description="连续失败达到该次数后临时锁定；Redis 不可用时跳过锁定。"),
    SettingSpec("login_lock_minutes", "account", "登录锁定时长（分钟）", "int", default=15, min=1, max=1440,
                unit="分钟", description="超过阈值后禁止登录的分钟数。"),
    SettingSpec("login_ip_max_attempts", "account", "同 IP 登录尝试上限", "int", default=60,
                min=10, max=1000, unit="次",
                description="同一 IP 在窗口期内的总登录尝试上限；管理员登录不计入。"),
    SettingSpec("login_ip_window_seconds", "account", "IP 限流窗口（秒）", "int", default=900,
                min=60, max=86400, unit="秒",
                description="IP 计数窗口长度，窗口结束后自动重置。"),
    # ---------- 4. 用量与限额 ----------
    SettingSpec("daily_quota_enabled", "quota", "启用每日用量配额", "bool", default=True,
                description="关闭后所有用户不受每日 Token 配额限制。"),
    SettingSpec("user_daily_token_quota", "quota", "用户每日 Token 配额", "int",
                env_attr="user_daily_token_quota", min=1000, max=100_000_000, unit="tokens",
                description="用户未单独设置时的每日默认配额。"),
    SettingSpec("quota_alert_threshold", "quota", "配额告警阈值（%）", "int", default=90, min=50, max=100,
                unit="%", description="当日用量达到该百分比时在 /usage 中提示。"),
    # ---------- 5. 消息与内容 ----------
    SettingSpec("message_chunk_chars", "content", "回复分片长度（字符）", "int", default=1500,
                min=200, max=4000, unit="字符",
                description="模型回复超长时按该长度拆成多条依次发送，避免触发微信单条上限。"),
    SettingSpec("message_max_chars", "content", "单条消息长度上限", "int", default=10000,
                min=100, max=100_000, unit="字符",
                description="入站文本超过上限时截断，避免超大输入拖垮上下文。"),
    SettingSpec("max_context_messages", "content", "上下文条数上限", "int", default=100,
                min=2, max=200, unit="条", description="/context 命令允许设置的最大历史条数。"),
    SettingSpec("max_cards_per_user", "content", "角色卡数量上限", "int", default=20,
                min=1, max=100, unit="张", description="每个用户最多可保存的角色卡数量。"),
    SettingSpec("card_max_chars", "content", "单张角色卡内容上限", "int", default=20000,
                min=500, max=200_000, unit="字符", description="角色卡正文的最大长度。"),
    SettingSpec("max_memories_per_user", "content", "记忆条数上限", "int", default=100,
                min=1, max=500, unit="条", description="每个用户最多保存的长期记忆条数。"),
    SettingSpec("memory_max_chars", "content", "单条记忆长度上限", "int", default=2000,
                min=50, max=10000, unit="字符", description="单条长期记忆的最大长度。"),
    SettingSpec("max_presets_per_user", "content", "预设数量上限", "int", default=20,
                min=1, max=100, unit="个", description="每个用户最多保存的预设数量。"),
    SettingSpec("max_providers_per_user", "content", "BYOK 供应商上限", "int", default=5,
                min=1, max=20, unit="个", description="每个用户最多可添加的自带密钥供应商。"),
    # ---------- 6. 媒体 ----------
    SettingSpec("media_allowed_mime_types", "media", "媒体类型白名单", "str",
                env_attr="media_allowed_mime_types", max=2000,
                description="逗号分隔的 MIME 类型；网关只记录元数据。"),
    SettingSpec("media_max_size_bytes", "media", "媒体大小上限（字节）", "int",
                env_attr="media_max_size_bytes", min=1024 * 1024, max=500 * 1024 * 1024,
                unit="字节", description="入站媒体元数据记录的最大字节数。"),
    SettingSpec("media_retention_hours", "media", "媒体记录保留时长（小时）", "int",
                env_attr="media_retention_hours", min=1, max=8760, unit="小时",
                description="过期后由 Worker 每小时清理元数据记录。"),
    SettingSpec("wecom_upload_timeout_seconds", "media", "企微上传下载超时（秒）", "int",
                env_attr="wecom_upload_timeout_seconds", min=5, max=120, unit="秒",
                editable=False, description="企微素材上传下载超时，仅展示。"),
    # ---------- 7. 任务与可靠性 ----------
    SettingSpec("task_max_attempts", "task", "任务最大尝试次数", "int",
                env_attr="task_max_attempts", min=1, max=50, unit="次",
                description="超过后任务进入死信，不再自动重试。"),
    SettingSpec("task_retry_base_seconds", "task", "重试退避基数（秒）", "int",
                env_attr="task_retry_base_seconds", min=1, max=600, unit="秒",
                description="重试间隔 = 基数 × 2^(尝试次数-1)。"),
    SettingSpec("task_retry_max_seconds", "task", "重试间隔上限（秒）", "int",
                env_attr="task_retry_max_seconds", min=5, max=3600, unit="秒",
                description="退避计算后的最大等待秒数。"),
    SettingSpec("task_lock_timeout_seconds", "task", "任务锁超时（秒）", "int",
                env_attr="task_lock_timeout_seconds", min=30, max=3600, unit="秒",
                description="过期后其他 Worker 可重新领取该任务。"),
    SettingSpec("worker_poll_seconds", "task", "Worker 轮询间隔（秒）", "int",
                env_attr="worker_poll_seconds", min=1, max=60, unit="秒",
                editable=False, description="Worker 空闲轮询间隔，仅展示。"),
    SettingSpec("sync_lock_seconds", "task", "企微同步锁时长（秒）", "int",
                env_attr="sync_lock_seconds", min=10, max=600, unit="秒",
                editable=False, description="企微游标同步互斥锁时长，仅展示。"),
    # ---------- 8. 渠道与 ClawBot ----------
    SettingSpec("clawbot_bridge_base_url", "channel", "ClawBot Bridge 地址", "str",
                env_attr="clawbot_bridge_base_url", max=500, editable=False,
                description="桥接服务地址，不得携带凭据，仅展示。"),
    SettingSpec("clawbot_bridge_token", "channel", "ClawBot Bridge 令牌", "secret",
                env_attr="clawbot_bridge_token", secret=True,
                description="桥接鉴权令牌，仅显示是否已配置。"),
    SettingSpec("clawbot_request_timeout_seconds", "channel", "ClawBot 请求超时（秒）", "int",
                env_attr="clawbot_request_timeout_seconds", min=5, max=120, unit="秒",
                editable=False, description="网关调用桥接的超时，仅展示。"),
    SettingSpec("allow_user_clawbot_instances", "channel", "允许用户自助创建实例", "bool",
                default=True, description="关闭后普通用户不能再创建新的 ClawBot 实例。"),
    # ---------- 9. 数据保留 ----------
    SettingSpec("message_retention_days", "retention", "消息保留天数", "int", default=0,
                min=0, max=3650, unit="天", description="超过天数的消息会被清理；0=不清理。"),
    SettingSpec("dead_task_retention_days", "retention", "死信任务保留天数", "int", default=0,
                min=0, max=3650, unit="天", description="超过天数的死信任务会被删除；0=不清理。"),
    SettingSpec("audit_retention_days", "retention", "审计日志保留天数", "int", default=0,
                min=0, max=3650, unit="天", description="超过天数的审计日志会被删除；0=不清理。"),
    # ---------- 10. 告警 ----------
    SettingSpec("alert_email_enabled", "alert", "启用邮件告警", "bool", default=False,
                description="关闭时告警仅写日志；开启后仍需填写 SMTP 与收件人。"),
    SettingSpec("alert_email_recipient", "alert", "告警收件人", "str", default="", max=500,
                description="接收告警的邮箱地址，多个用逗号分隔。"),
    SettingSpec("smtp_host", "alert", "SMTP 服务器", "str", default="", max=255,
                description="邮件服务器地址，如 smtp.example.com。"),
    SettingSpec("smtp_port", "alert", "SMTP 端口", "int", default=465, min=1, max=65535,
                unit="端口", description="465=SSL，587=STARTTLS。"),
    SettingSpec("smtp_user", "alert", "SMTP 账号", "str", default="", max=255,
                description="发信账号。"),
    SettingSpec("smtp_password", "alert", "SMTP 密码", "str", default="", max=255,
                secret=True, write_only=True,
                description="发信密码/授权码；后台可填写，永不回显。"),
    SettingSpec("smtp_from", "alert", "发件人地址", "str", default="", max=255,
                description="邮件 From 地址。"),
]

SPEC_BY_KEY: dict[str, SettingSpec] = {spec.key: spec for spec in SPECS}

# 允许渠道/用户级覆盖的键及其作用域。
# 刻意只开放少量键：覆盖会改变单个用户/渠道的行为，默认保守。
OVERRIDABLE_KEYS: dict[str, set[str]] = {
    "user_daily_token_quota": {"user"},
    "max_context_messages": {"user", "channel"},
    "message_max_chars": {"channel"},
}


def _coerce(raw: Any, spec: SettingSpec) -> Any:
    if spec.type == "bool":
        if not isinstance(raw, bool):
            raise ValueError(f"{spec.label}必须是布尔值")
        return raw
    if spec.type == "int":
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{spec.label}必须是整数") from None
        if spec.min is not None and value < spec.min:
            raise ValueError(f"{spec.label}不能小于 {int(spec.min)}")
        if spec.max is not None and value > spec.max:
            raise ValueError(f"{spec.label}不能大于 {int(spec.max)}")
        return value
    if spec.type == "float":
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{spec.label}必须是数字") from None
        if spec.min is not None and value < spec.min:
            raise ValueError(f"{spec.label}不能小于 {spec.min}")
        if spec.max is not None and value > spec.max:
            raise ValueError(f"{spec.label}不能大于 {spec.max}")
        return value
    if spec.type == "select":
        if raw not in (spec.options or ()):
            raise ValueError(f"{spec.label}必须是 {'/'.join(spec.options or ())} 之一")
        return raw
    # str
    if not isinstance(raw, str):
        raise ValueError(f"{spec.label}必须是文本")  # noqa: TRY004 - 业务校验异常，统一由调用方转 4xx
    if spec.max is not None and len(raw) > spec.max:
        raise ValueError(f"{spec.label}不能超过 {int(spec.max)} 字符")
    return raw


_CACHE_KEY = "_runtime_settings_cache"


def _cache_clear(db: Session) -> None:
    db.info.pop(_CACHE_KEY, None)


def get_runtime_value(db: Session, key: str) -> Any:
    """读取设置：DB 已校验值 > .env（settings 单例）> spec 默认。

    同一 session 内按 key 缓存，避免单条消息处理链路反复查库；
    写入路径（update_settings / 覆盖）会主动清空该缓存。
    """
    cache = db.info.get(_CACHE_KEY)
    if cache is not None and key in cache:
        return cache[key]
    spec = SPEC_BY_KEY[key]
    row = db.get(PlatformConfig, _db_key(spec))
    value: Any = spec.default
    if row is not None:
        try:
            value = _coerce(_from_stored(spec, row.value), spec)
        except ValueError:
            # 库里出现非法值（旧数据/手工修改）时回退默认，避免拖垮运行
            pass
    elif spec.env_attr and hasattr(settings, spec.env_attr):
        value = getattr(settings, spec.env_attr)
    if cache is None:
        cache = db.info[_CACHE_KEY] = {}
    cache[key] = value
    return value


def get_effective_value(
    db: Session,
    key: str,
    *,
    user_id: str | None = None,
    channel: str | None = None,
) -> Any:
    """按 用户 > 渠道 > 平台配置 > .env 的优先级解析设置。"""
    spec = SPEC_BY_KEY[key]
    for scope_type, scope_id in (("user", user_id), ("channel", channel)):
        if not scope_id:
            continue
        row = db.scalar(
            select(SettingOverride).where(
                SettingOverride.scope_type == scope_type,
                SettingOverride.scope_id == scope_id,
                SettingOverride.key == key,
            )
        )
        if row is not None:
            try:
                return _coerce(row.value, spec)
            except ValueError:
                continue
    return get_runtime_value(db, key)


def set_override(
    db: Session,
    scope_type: str,
    scope_id: str,
    key: str,
    value: Any,
) -> SettingOverride:
    """写入渠道/用户级覆盖；校验键可覆盖性与类型/上下限。"""
    allowed = OVERRIDABLE_KEYS.get(key)
    if not allowed:
        raise ValueError("该设置不支持覆盖")
    if scope_type not in allowed:
        raise ValueError(f"该设置不支持 {scope_type} 级覆盖")
    spec = SPEC_BY_KEY[key]
    coerced = _coerce(value, spec)
    row = db.scalar(
        select(SettingOverride).where(
            SettingOverride.scope_type == scope_type,
            SettingOverride.scope_id == scope_id,
            SettingOverride.key == key,
        )
    )
    if row:
        row.value = coerced
    else:
        row = SettingOverride(
            scope_type=scope_type, scope_id=scope_id, key=key, value=coerced
        )
        db.add(row)
    _cache_clear(db)
    db.commit()
    return row


def remove_override(db: Session, scope_type: str, scope_id: str, key: str) -> None:
    db.execute(
        SettingOverride.__table__.delete().where(
            SettingOverride.scope_type == scope_type,
            SettingOverride.scope_id == scope_id,
            SettingOverride.key == key,
        )
    )
    _cache_clear(db)
    db.commit()


def list_overrides(db: Session) -> list[SettingOverride]:
    return list(
        db.scalars(
            select(SettingOverride).order_by(
                SettingOverride.scope_type, SettingOverride.scope_id, SettingOverride.key
            )
        )
    )


def _db_key(spec: SettingSpec) -> str:
    """platform_mode 特例：映射到 policy 读取的 platform_config['mode']。"""
    return "mode" if spec.key == "platform_mode" else spec.key


def _to_stored(spec: SettingSpec, value: Any) -> Any:
    if spec.key == "platform_mode":
        return {"mode": value}
    return value


def _from_stored(spec: SettingSpec, stored: Any) -> Any:
    if spec.key == "platform_mode" and isinstance(stored, dict):
        return stored.get("mode")
    return stored


def update_settings(db: Session, values: dict[str, Any]) -> dict[str, str]:
    """批量保存；全部校验通过才落库。返回 {key: 错误信息}，空 dict 表示成功。"""
    normalized: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for key, raw in values.items():
        spec = SPEC_BY_KEY.get(key)
        if spec is None:
            errors[key] = "未知设置项"
            continue
        if not spec.editable:
            errors[key] = "该设置只读，请通过 .env 修改"
            continue
        if spec.secret and not spec.write_only:
            errors[key] = "该密钥只读，请通过 .env 管理"
            continue
        if spec.write_only and (raw is None or str(raw).strip() == ""):
            continue  # 留空表示不修改
        try:
            value = _coerce(raw, spec)
        except ValueError as exc:
            errors[key] = str(exc)
            continue
        normalized[key] = encrypt_secret(value) if spec.write_only else value
    if errors:
        return errors
    for key, value in normalized.items():
        spec = SPEC_BY_KEY[key]
        db_key = _db_key(spec)
        row = db.get(PlatformConfig, db_key)
        if row is not None:
            row.value = _to_stored(spec, value)
        else:
            db.add(PlatformConfig(key=db_key, value=_to_stored(spec, value)))
    _cache_clear(db)
    db.commit()
    return {}


def settings_view(db: Session) -> list[dict]:
    """返回按声明顺序排列的设置视图（分组由前端聚合）。"""
    rows = {row.key: row.value for row in db.scalars(select(PlatformConfig))}
    view: list[dict] = []
    for spec in SPECS:
        overridable = OVERRIDABLE_KEYS.get(spec.key)
        if spec.secret:
            if spec.write_only:
                # 可写密钥：不回显内容，仅标记是否已设置（DB 有值）
                value = {"configured": spec.key in rows}
                item = {
                    "key": spec.key,
                    "group": spec.group,
                    "label": spec.label,
                    "type": "str",
                    "value": value,
                    "default": None,
                    "min": None,
                    "max": spec.max,
                    "options": None,
                    "secret": True,
                    "editable": True,
                    "description": spec.description,
                    "unit": spec.unit,
                }
                view.append(item)
                continue
            configured = bool(getattr(settings, spec.env_attr, "") if spec.env_attr else "")
            value: Any = {"configured": configured}
        else:
            stored = rows.get(_db_key(spec))
            if stored is not None:
                try:
                    value = _coerce(_from_stored(spec, stored), spec)
                except ValueError:
                    value = get_runtime_value(db, spec.key)
            else:
                value = get_runtime_value(db, spec.key)
        item = {
            "key": spec.key,
            "group": spec.group,
            "label": spec.label,
            "type": spec.type,
            "value": value,
            "default": spec.default if not spec.secret else None,
            "min": spec.min if spec.type in {"int", "float"} else None,
            "max": spec.max if spec.type in {"int", "float"} else None,
            "options": list(spec.options) if spec.options else None,
            "secret": spec.secret,
            "editable": spec.editable and not spec.secret,
            "description": spec.description,
            "unit": spec.unit,
        }
        if overridable:
            item["overridable"] = sorted(overridable)
        view.append(item)
    return view
