import hashlib
import logging
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from time import perf_counter
from typing import Literal
from urllib.parse import urlsplit

import qrcode
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from qrcode.image.svg import SvgPathImage
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from . import presets as preset_service
from .auth import (
    Principal,
    authenticate_password,
    clear_ip_attempts,
    clear_login_failures,
    create_session,
    is_ip_locked,
    is_login_locked,
    normalize_username,
    principal_from_bearer,
    record_ip_attempt,
    record_login_failure,
    revoke_session,
)
from .cards import (
    decrypt_card_content,
    detect_format,
    encrypt_card_content,
    extract_card_from_png,
)
from .channels import ChannelMessage, registry
from .clawbot import register_clawbot_adapter
from .commands import memory_text
from .config import settings
from .db import SessionLocal, session_scope
from .media import list_media_metadata
from .mfa import (
    account_subject,
    admin_subject,
    consume_challenge,
    create_challenge,
    credential_for,
    enable_credential,
    enabled_credential,
    remove_credential,
    save_pending_secret,
    verify_second_factor,
    verify_subject_password,
)
from .migration import migrate_user_mode
from .model_routing import complete_with_routing
from .models import (
    Account,
    AuditLog,
    AuthSession,
    ChannelIdentity,
    ChannelInstance,
    CharacterCard,
    CommandPolicy,
    Conversation,
    MediaAsset,
    Memory,
    Message,
    MessageStatus,
    MfaChallenge,
    ModelGroup,
    ModelRoute,
    OutboxStatus,
    OutboxTask,
    PlatformConfig,
    PlatformProvider,
    Preset,
    SettingOverride,
    UsageRecord,
    User,
    UserProvider,
    UserSettings,
)
from .policy import resolve_user_mode
from .queueing import enqueue_sync
from .redaction import redact_error
from .runtime_settings import (
    get_runtime_value,
    list_overrides,
    set_override,
    settings_view,
    update_settings,
)
from .security import decrypt_secret, encrypt_secret, hash_password, verify_admin_token
from .services import ingest_channel_message, quota_status
from .tasks import replay_task
from .tool_execution import available_tool_names, parse_tool_allowlist
from .totp import generate_totp_secret, otpauth_uri
from .wecom import decrypt, parse_callback, verify_signature

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger(__name__)
app = FastAPI(
    title=settings.app_name,
    version="0.3.0",
    docs_url="/api/docs" if settings.app_env != "production" else None,
)
web = Path(__file__).resolve().parents[2] / "web"
app.mount("/static", StaticFiles(directory=web / "static"), name="static")
register_clawbot_adapter()


def db_dep():
    yield from session_scope()


def admin(
    x_admin_token: str | None = Header(None),
    authorization: str | None = Header(None),
    db: Session = Depends(db_dep),
):
    principal = principal_from_bearer(db, authorization)
    legacy_token_valid = verify_admin_token(x_admin_token)
    if legacy_token_valid and enabled_credential(db, admin_subject()):
        legacy_token_valid = False
    if not legacy_token_valid and not (principal and principal.role == "admin"):
        raise HTTPException(401, "invalid admin credentials")


def current_principal(
    authorization: str | None = Header(None), db: Session = Depends(db_dep)
) -> Principal:
    principal = principal_from_bearer(db, authorization)
    if not principal:
        raise HTTPException(401, "invalid or expired session")
    return principal


def current_user(principal: Principal = Depends(current_principal)) -> Principal:
    if principal.role != "user" or not principal.user_id:
        raise HTTPException(403, "user session required")
    return principal


def bridge_auth(authorization: str | None = Header(default=None)) -> None:
    """桥接服务只能以独立令牌写入消息，不能复用管理员令牌。"""
    token = settings.clawbot_bridge_token
    if not token:
        raise HTTPException(503, "channel bridge token is not configured")
    if authorization != f"Bearer {token}":
        raise HTTPException(401, "invalid channel bridge token")


@app.get("/health")
def health(db: Session = Depends(db_dep)):
    db.execute(select(1))
    return {"ok": True, "service": "wecom-ai-gateway", "version": "0.3.0"}


@app.get(settings.wecom_callback_path)
def verify_callback(msg_signature: str, timestamp: str, nonce: str, echostr: str):
    if not verify_signature(msg_signature, timestamp, nonce, echostr):
        raise HTTPException(403, "signature mismatch")
    return Response(decrypt(echostr), media_type="text/plain")


@app.post(settings.wecom_callback_path)
async def callback(request: Request, msg_signature: str, timestamp: str, nonce: str):
    event = parse_callback(await request.body(), msg_signature, timestamp, nonce)
    if event.event == "kf_msg_or_event" and event.token:
        if event.msg_id and event.status == "2":
            _mark_wecom_send_failed(event.msg_id, event.fail_reason)
        else:
            enqueue_sync(event.token, event.open_kfid)
    return Response("success", media_type="text/plain")


def _mark_wecom_send_failed(msg_id: str, fail_reason: str) -> None:
    """企业微信推送发送失败事件（Status=2）时，将对应出站消息标记失败。"""
    db = SessionLocal()
    try:
        row = db.scalar(
            select(Message).where(
                Message.channel == "wecom_kf",
                Message.external_message_id == msg_id,
                Message.direction == "outbound",
            )
        )
        if row and row.status in (MessageStatus.sent, MessageStatus.processing):
            row.status = MessageStatus.failed
            row.error = redact_error(fail_reason or "wecom send failed", 1000)
            db.commit()
            log.warning("企微发送失败事件 msg_id=%s reason=%s", msg_id, redact_error(fail_reason or "", 200))
    except Exception:
        log.exception("处理企微发送失败事件失败 msg_id=%s", msg_id)
    finally:
        db.close()


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return (web / "index.html").read_text(encoding="utf-8")


class LoginIn(BaseModel):
    username: str
    password: str


class MfaChallengeIn(BaseModel):
    challenge_token: str
    code: str


class MfaPasswordIn(BaseModel):
    password: str


class MfaCodeIn(BaseModel):
    code: str


class MfaDisableIn(MfaPasswordIn, MfaCodeIn):
    pass


class RegisterIn(LoginIn):
    display_name: str | None = None


class AccountProvisionIn(LoginIn):
    pass


@app.get("/api/auth/config")
def auth_config(db: Session = Depends(db_dep)):
    return {
        "registration_enabled": bool(get_runtime_value(db, "allow_public_registration"))
        and not settings.single_user_mode
        and resolve_user_mode(db, None) == "self_service",
        "announcement": get_runtime_value(db, "announcement"),
        "maintenance_mode": bool(get_runtime_value(db, "maintenance_mode")),
    }


@app.post("/api/auth/login")
def auth_login(body: LoginIn, request: Request, db: Session = Depends(db_dep)):
    normalized = normalize_username(body.username)
    is_admin_login = normalized == settings.admin_username.strip().lower()
    client_ip = request.client.host if request.client else ""
    if client_ip and not is_admin_login:
        if is_ip_locked(client_ip):
            raise HTTPException(429, "登录尝试过于频繁，请稍后再试")
        record_ip_attempt(client_ip)
    if is_login_locked(normalized):
        raise HTTPException(429, "登录失败次数过多，请稍后再试")
    try:
        result = authenticate_password(db, body.username, body.password)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not result:
        record_login_failure(normalized)
        raise HTTPException(401, "账号或密码错误")
    principal, account_id, subject = result
    if enabled_credential(db, subject):
        return {
            "mfa_required": True,
            "challenge_token": create_challenge(db, subject),
        }
    clear_login_failures(normalized)
    if client_ip:
        clear_ip_attempts(client_ip)
    token = create_session(
        db,
        role=principal.role,
        user_id=principal.user_id,
        account_id=account_id,
    )
    return {
        "token": token,
        "role": principal.role,
        "user_id": principal.user_id,
        "username": principal.username,
    }


@app.post("/api/auth/mfa/verify")
def auth_mfa_verify(
    body: MfaChallengeIn, request: Request, db: Session = Depends(db_dep)
):
    challenge_username = ""
    if body.challenge_token and len(body.challenge_token) <= 200:
        challenge = db.scalar(
            select(MfaChallenge).where(
                MfaChallenge.token_hash
                == hashlib.sha256(body.challenge_token.encode()).hexdigest()
            )
        )
        challenge_username = challenge.username if challenge else ""
    subject = consume_challenge(db, body.challenge_token, body.code)
    if not subject:
        if challenge_username:
            record_login_failure(challenge_username.strip().lower())
        raise HTTPException(401, "验证码或恢复码无效")
    clear_login_failures(subject.username.strip().lower())
    if request.client:
        clear_ip_attempts(request.client.host)
    token = create_session(
        db,
        role=subject.role,
        user_id=subject.user_id,
        account_id=subject.account_id,
    )
    db.add(
        AuditLog(
            user_id=subject.user_id,
            action="mfa.login",
            detail={"subject_type": subject.subject_type},
        )
    )
    db.commit()
    return {
        "token": token,
        "role": subject.role,
        "user_id": subject.user_id,
        "username": subject.username,
    }


@app.post("/api/auth/register")
def auth_register(body: RegisterIn, db: Session = Depends(db_dep)):
    if (
        not bool(get_runtime_value(db, "allow_public_registration"))
        or settings.single_user_mode
        or resolve_user_mode(db, None) != "self_service"
    ):
        raise HTTPException(403, "当前平台未开放用户注册")
    try:
        username = normalize_username(body.username)
        password_hash = hash_password(body.password, min_length=int(get_runtime_value(db, "password_min_length")))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if username == settings.admin_username.strip().lower():
        raise HTTPException(409, "用户名已存在")
    if db.scalar(select(Account).where(Account.username == username)):
        raise HTTPException(409, "用户名已存在")
    user = User(display_name=(body.display_name or username).strip()[:255], mode="self_service")
    db.add(user)
    db.flush()
    account = Account(
        user_id=user.id,
        username=username,
        password_hash=password_hash,
        role="user",
    )
    db.add(account)
    db.flush()
    token = create_session(db, role="user", user_id=user.id, account_id=account.id)
    return {"token": token, "role": "user", "user_id": user.id, "username": username}


@app.post("/api/auth/logout")
def auth_logout(authorization: str | None = Header(None), db: Session = Depends(db_dep)):
    revoke_session(db, authorization)
    return {"ok": True}


@app.get("/api/auth/me")
def auth_me(principal: Principal = Depends(current_principal)):
    return {
        "role": principal.role,
        "user_id": principal.user_id,
        "username": principal.username,
    }


def _mfa_subject_for_principal(db: Session, principal: Principal):
    if principal.role == "admin":
        return admin_subject()
    if not principal.user_id:
        raise HTTPException(403, "不支持该账号类型")
    account = db.scalar(
        select(Account).where(
            Account.user_id == principal.user_id,
            Account.is_active.is_(True),
        )
    )
    if not account:
        raise HTTPException(404, "登录账号不存在")
    return account_subject(account)


@app.get("/api/auth/mfa/status")
def auth_mfa_status(
    principal: Principal = Depends(current_principal), db: Session = Depends(db_dep)
):
    subject = _mfa_subject_for_principal(db, principal)
    row = credential_for(db, subject.subject_type, subject.subject_id)
    return {
        "enabled": bool(row and row.enabled),
        "recovery_codes_remaining": len(row.recovery_code_hashes or []) if row and row.enabled else 0,
    }


@app.post("/api/auth/mfa/setup")
def auth_mfa_setup(
    body: MfaPasswordIn,
    principal: Principal = Depends(current_principal),
    db: Session = Depends(db_dep),
):
    subject = _mfa_subject_for_principal(db, principal)
    if not verify_subject_password(db, subject, body.password):
        raise HTTPException(400, "当前密码不正确")
    secret = generate_totp_secret()
    try:
        save_pending_secret(db, subject, secret)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    uri = otpauth_uri(secret, subject.username)
    image = qrcode.make(uri, image_factory=SvgPathImage)
    output = BytesIO()
    image.save(output)
    return {
        "secret": secret,
        "otpauth_uri": uri,
        "qr_svg": output.getvalue().decode(),
    }


@app.post("/api/auth/mfa/enable")
def auth_mfa_enable(
    body: MfaCodeIn,
    authorization: str | None = Header(None),
    principal: Principal = Depends(current_principal),
    db: Session = Depends(db_dep),
):
    subject = _mfa_subject_for_principal(db, principal)
    try:
        codes = enable_credential(db, subject, body.code)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    token_hash = hashlib.sha256(
        (authorization or "").removeprefix("Bearer ").strip().encode()
    ).hexdigest()
    if subject.account_id:
        db.execute(
            delete(AuthSession).where(
                AuthSession.account_id == subject.account_id,
                AuthSession.token_hash != token_hash,
            )
        )
    else:
        db.execute(
            delete(AuthSession).where(
                AuthSession.role == "admin",
                AuthSession.account_id.is_(None),
                AuthSession.token_hash != token_hash,
            )
        )
    db.add(
        AuditLog(
            user_id=subject.user_id,
            action="mfa.enable",
            detail={"subject_type": subject.subject_type},
        )
    )
    db.commit()
    return {"ok": True, "recovery_codes": codes}


@app.post("/api/auth/mfa/disable")
def auth_mfa_disable(
    body: MfaDisableIn,
    principal: Principal = Depends(current_principal),
    db: Session = Depends(db_dep),
):
    subject = _mfa_subject_for_principal(db, principal)
    if not verify_subject_password(db, subject, body.password):
        raise HTTPException(400, "当前密码不正确")
    if not verify_second_factor(db, subject, body.code):
        raise HTTPException(400, "验证码或恢复码无效")
    remove_credential(db, subject)
    db.add(
        AuditLog(
            user_id=subject.user_id,
            action="mfa.disable",
            detail={"subject_type": subject.subject_type},
        )
    )
    db.commit()
    return {"ok": True}


@app.get("/api/me/summary")
def my_summary(
    principal: Principal = Depends(current_user), db: Session = Depends(db_dep)
):
    assert principal.user_id is not None
    user_id = principal.user_id
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "user not found")

    def count(model, condition) -> int:
        return int(db.scalar(select(func.count()).select_from(model).where(condition)) or 0)

    tokens = db.scalar(
        select(
            func.coalesce(func.sum(UsageRecord.prompt_tokens + UsageRecord.completion_tokens), 0)
        ).where(UsageRecord.user_id == user_id)
    )
    media = db.scalar(
        select(func.count()).select_from(MediaAsset)
        .join(Message, Message.id == MediaAsset.message_id)
        .where(Message.user_id == user_id)
    )
    quota = quota_status(db, user_id, db.get(UserSettings, user_id))
    quota["alert_threshold"] = int(get_runtime_value(db, "quota_alert_threshold"))
    return {
        "user_id": user_id,
        "display_name": user.display_name,
        "mode": resolve_user_mode(db, user),
        "conversations": count(Conversation, Conversation.user_id == user_id),
        "memories": count(Memory, Memory.user_id == user_id),
        "media_assets": int(media or 0),
        "tokens_total": int(tokens or 0),
        "cards": count(CharacterCard, CharacterCard.user_id == user_id),
        "presets": count(Preset, Preset.user_id == user_id),
        "providers": count(UserProvider, UserProvider.user_id == user_id),
        "quota": quota,
    }


class SettingsUpdateIn(BaseModel):
    values: dict


class DisplayNameIn(BaseModel):
    display_name: str


class PlatformProviderIn(BaseModel):
    name: str
    provider_key: str = "openai-compatible"
    base_url: str
    api_key: str
    enabled: bool = True


class PlatformProviderUpdateIn(BaseModel):
    name: str | None = None
    provider_key: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    enabled: bool | None = None


class ModelRouteIn(BaseModel):
    provider_id: str
    model: str
    priority: int = 100
    enabled: bool = True


class ModelGroupIn(BaseModel):
    name: str
    enabled: bool = True
    is_default: bool = False
    routes: list[ModelRouteIn] = []


class UserModelGroupIn(BaseModel):
    model_group_id: str | None = None


def _platform_provider_view(row: PlatformProvider) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "provider_key": row.provider_key,
        "base_url": row.base_url,
        "enabled": row.enabled,
        "api_key_configured": bool(row.api_key_encrypted),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _model_group_view(db: Session, group: ModelGroup) -> dict:
    rows = db.execute(
        select(ModelRoute, PlatformProvider)
        .join(PlatformProvider, PlatformProvider.id == ModelRoute.provider_id)
        .where(ModelRoute.group_id == group.id)
        .order_by(ModelRoute.priority, ModelRoute.created_at, ModelRoute.id)
    ).all()
    return {
        "id": group.id,
        "name": group.name,
        "enabled": group.enabled,
        "is_default": group.is_default,
        "created_at": group.created_at,
        "updated_at": group.updated_at,
        "routes": [
            {
                "id": route.id,
                "provider_id": provider.id,
                "provider_name": provider.name,
                "provider_enabled": provider.enabled,
                "model": route.model,
                "priority": route.priority,
                "enabled": route.enabled,
            }
            for route, provider in rows
        ],
    }


def _validate_provider_fields(name: str, provider_key: str, base_url: str) -> tuple[str, str, str]:
    name = name.strip()[:120]
    provider_key = provider_key.strip()[:40]
    base_url = base_url.strip().rstrip("/")[:500]
    if not name or not provider_key:
        raise HTTPException(400, "name 与 provider_key 必填")
    if provider_key != "openai-compatible":
        raise HTTPException(400, "当前仅支持 openai-compatible")
    parsed = urlsplit(base_url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise HTTPException(400, "base_url 必须是无凭据的 https 地址")
    return name, provider_key, base_url


@app.get("/api/admin/model-providers", dependencies=[Depends(admin)])
def admin_model_providers(db: Session = Depends(db_dep)):
    rows = db.scalars(select(PlatformProvider).order_by(PlatformProvider.created_at))
    return [_platform_provider_view(row) for row in rows]


@app.post("/api/admin/model-providers", dependencies=[Depends(admin)])
def admin_create_model_provider(body: PlatformProviderIn, db: Session = Depends(db_dep)):
    name, provider_key, base_url = _validate_provider_fields(
        body.name, body.provider_key, body.base_url
    )
    if not body.api_key.strip():
        raise HTTPException(400, "api_key 必填")
    if db.scalar(select(PlatformProvider.id).where(PlatformProvider.name == name)):
        raise HTTPException(409, "供应商名称已存在")
    row = PlatformProvider(
        name=name,
        provider_key=provider_key,
        base_url=base_url,
        api_key_encrypted=encrypt_secret(body.api_key.strip()),
        enabled=body.enabled,
    )
    db.add(row)
    db.flush()
    db.add(AuditLog(action="model_provider.create", detail={"provider_id": row.id}))
    db.commit()
    db.refresh(row)
    return _platform_provider_view(row)


@app.put("/api/admin/model-providers/{provider_id}", dependencies=[Depends(admin)])
def admin_update_model_provider(
    provider_id: str,
    body: PlatformProviderUpdateIn,
    db: Session = Depends(db_dep),
):
    row = db.get(PlatformProvider, provider_id)
    if not row:
        raise HTTPException(404, "model provider not found")
    name, provider_key, base_url = _validate_provider_fields(
        body.name if body.name is not None else row.name,
        body.provider_key if body.provider_key is not None else row.provider_key,
        body.base_url if body.base_url is not None else row.base_url,
    )
    conflict = db.scalar(
        select(PlatformProvider.id).where(
            PlatformProvider.name == name, PlatformProvider.id != row.id
        )
    )
    if conflict:
        raise HTTPException(409, "供应商名称已存在")
    row.name = name
    row.provider_key = provider_key
    row.base_url = base_url
    if body.api_key is not None and body.api_key.strip():
        row.api_key_encrypted = encrypt_secret(body.api_key.strip())
    if body.enabled is not None:
        row.enabled = body.enabled
    db.add(AuditLog(action="model_provider.update", detail={"provider_id": row.id}))
    db.commit()
    db.refresh(row)
    return _platform_provider_view(row)


@app.delete("/api/admin/model-providers/{provider_id}", dependencies=[Depends(admin)])
def admin_delete_model_provider(provider_id: str, db: Session = Depends(db_dep)):
    row = db.get(PlatformProvider, provider_id)
    if not row:
        raise HTTPException(404, "model provider not found")
    db.delete(row)
    db.add(AuditLog(action="model_provider.delete", detail={"provider_id": provider_id}))
    db.commit()
    return {"ok": True}


def _save_model_group(db: Session, group: ModelGroup, body: ModelGroupIn) -> None:
    name = body.name.strip()[:120]
    if not name:
        raise HTTPException(400, "name 必填")
    targets: set[tuple[str, str]] = set()
    normalized: list[tuple[ModelRouteIn, str]] = []
    for route in body.routes:
        model = route.model.strip()[:160]
        if not model or not db.get(PlatformProvider, route.provider_id):
            raise HTTPException(400, "路由模型为空或供应商不存在")
        target = (route.provider_id, model)
        if target in targets:
            raise HTTPException(400, "同一组内不能重复配置相同供应商与模型")
        targets.add(target)
        normalized.append((route, model))
    conflict = db.scalar(
        select(ModelGroup.id).where(ModelGroup.name == name, ModelGroup.id != group.id)
    )
    if conflict:
        raise HTTPException(409, "模型组名称已存在")
    group.name = name
    group.enabled = body.enabled
    if body.is_default:
        db.execute(
            ModelGroup.__table__.update()
            .where(ModelGroup.id != group.id, ModelGroup.is_default.is_(True))
            .values(is_default=False)
        )
        # 部分唯一索引要求事务内也不能瞬时出现两个默认组：先落旧组清理，再设新组。
        db.flush()
    group.is_default = body.is_default
    db.execute(delete(ModelRoute).where(ModelRoute.group_id == group.id))
    for route, model in normalized:
        db.add(
            ModelRoute(
                group_id=group.id,
                provider_id=route.provider_id,
                model=model,
                priority=max(0, min(route.priority, 10000)),
                enabled=route.enabled,
            )
        )


@app.get("/api/admin/model-groups", dependencies=[Depends(admin)])
def admin_model_groups(db: Session = Depends(db_dep)):
    rows = db.scalars(select(ModelGroup).order_by(ModelGroup.created_at))
    return [_model_group_view(db, row) for row in rows]


@app.post("/api/admin/model-groups", dependencies=[Depends(admin)])
def admin_create_model_group(body: ModelGroupIn, db: Session = Depends(db_dep)):
    group = ModelGroup(name=body.name.strip()[:120] or "pending")
    db.add(group)
    db.flush()
    _save_model_group(db, group, body)
    db.add(AuditLog(action="model_group.create", detail={"group_id": group.id}))
    db.commit()
    db.refresh(group)
    return _model_group_view(db, group)


@app.put("/api/admin/model-groups/{group_id}", dependencies=[Depends(admin)])
def admin_update_model_group(
    group_id: str, body: ModelGroupIn, db: Session = Depends(db_dep)
):
    group = db.get(ModelGroup, group_id)
    if not group:
        raise HTTPException(404, "model group not found")
    _save_model_group(db, group, body)
    db.add(AuditLog(action="model_group.update", detail={"group_id": group.id}))
    db.commit()
    db.refresh(group)
    return _model_group_view(db, group)


@app.delete("/api/admin/model-groups/{group_id}", dependencies=[Depends(admin)])
def admin_delete_model_group(group_id: str, db: Session = Depends(db_dep)):
    group = db.get(ModelGroup, group_id)
    if not group:
        raise HTTPException(404, "model group not found")
    db.delete(group)
    db.add(AuditLog(action="model_group.delete", detail={"group_id": group_id}))
    db.commit()
    return {"ok": True}


@app.post("/api/admin/model-groups/{group_id}/test", dependencies=[Depends(admin)])
async def admin_test_model_group(group_id: str, db: Session = Depends(db_dep)):
    group = db.get(ModelGroup, group_id)
    if not group:
        raise HTTPException(404, "model group not found")
    started = perf_counter()
    try:
        result = await complete_with_routing(
            db,
            UserSettings(user_id="admin-connectivity-test"),
            [{"role": "user", "content": "请只回复 OK"}],
            temperature=0,
            max_tokens=16,
            timeout=float(get_runtime_value(db, "request_timeout_seconds")),
            group_id=group_id,
        )
    except Exception as exc:
        log.warning("模型组连通性测试失败 group=%s error=%s", group_id, redact_error(exc, 300))
        raise HTTPException(502, "模型组连通性测试失败，请检查供应商状态与服务日志") from exc
    latency_ms = round((perf_counter() - started) * 1000)
    db.add(
        AuditLog(
            action="model_group.test",
            detail={
                "group_id": group_id,
                "provider_name": result.provider_name,
                "model": result.model,
                "latency_ms": latency_ms,
            },
        )
    )
    db.commit()
    return {
        "ok": True,
        "provider_name": result.provider_name,
        "model": result.model,
        "route_id": result.route_id,
        "latency_ms": latency_ms,
    }


@app.put("/api/admin/users/{user_id}/model-group", dependencies=[Depends(admin)])
def admin_set_user_model_group(
    user_id: str, body: UserModelGroupIn, db: Session = Depends(db_dep)
):
    if not db.get(User, user_id):
        raise HTTPException(404, "user not found")
    user_settings = _user_settings_for(db, user_id)
    group = None
    if body.model_group_id is not None:
        group = db.get(ModelGroup, body.model_group_id)
        if not group or not group.enabled:
            raise HTTPException(400, "模型组不存在或未启用")
    user_settings.model_group_id = group.id if group else None
    if group:
        effective_group = group
    else:
        effective_group = db.scalar(
            select(ModelGroup).where(
                ModelGroup.is_default.is_(True), ModelGroup.enabled.is_(True)
            )
        )
    db.add(
        AuditLog(
            action="user.model_group",
            user_id=user_id,
            detail={"model_group_id": user_settings.model_group_id},
        )
    )
    db.commit()
    return {
        "ok": True,
        "model_group_id": user_settings.model_group_id,
        "effective_group_name": effective_group.name if effective_group else None,
    }


@app.get("/api/admin/settings", dependencies=[Depends(admin)])
def admin_settings(db: Session = Depends(db_dep)):
    return {"settings": settings_view(db)}


@app.get("/api/admin/tools", dependencies=[Depends(admin)])
def admin_tools(db: Session = Depends(db_dep)):
    labels = {
        "get_current_time": "当前日期与时间",
        "get_weather": "当前天气与短期预报",
    }
    allowed = parse_tool_allowlist(str(get_runtime_value(db, "tools_allowed")))
    return {
        "enabled": bool(get_runtime_value(db, "tools_enabled")),
        "allowed": sorted(allowed),
        "max_calls": int(get_runtime_value(db, "tool_max_calls")),
        "timeout_seconds": int(get_runtime_value(db, "tool_timeout_seconds")),
        "catalog": [
            {"name": name, "label": labels[name], "read_only": True}
            for name in sorted(available_tool_names())
        ],
    }


@app.put("/api/admin/settings", dependencies=[Depends(admin)])
def admin_update_settings(body: SettingsUpdateIn, db: Session = Depends(db_dep)):
    errors = update_settings(db, body.values)
    if errors:
        raise HTTPException(400, {"errors": errors})
    db.add(AuditLog(action="settings.update", detail={"count": len(body.values)}))
    db.commit()
    return {"ok": True}


class SettingOverrideIn(BaseModel):
    scope_type: str  # user | channel
    scope_id: str
    key: str
    value: object


@app.get("/api/admin/settings/overrides", dependencies=[Depends(admin)])
def admin_setting_overrides(db: Session = Depends(db_dep)):
    return {
        "overrides": [
            {
                "id": row.id,
                "scope_type": row.scope_type,
                "scope_id": row.scope_id,
                "key": row.key,
                "value": row.value,
                "updated_at": row.updated_at,
            }
            for row in list_overrides(db)
        ]
    }


@app.put("/api/admin/settings/overrides", dependencies=[Depends(admin)])
def admin_set_setting_override(body: SettingOverrideIn, db: Session = Depends(db_dep)):
    try:
        row = set_override(db, body.scope_type, body.scope_id, body.key, body.value)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    db.add(
        AuditLog(
            action="settings.override",
            detail={"scope_type": body.scope_type, "scope_id": body.scope_id, "key": body.key},
        )
    )
    db.commit()
    return {"ok": True, "id": row.id}


@app.delete("/api/admin/settings/overrides/{override_id}", dependencies=[Depends(admin)])
def admin_delete_setting_override(override_id: str, db: Session = Depends(db_dep)):
    row = db.get(SettingOverride, override_id)
    if not row:
        raise HTTPException(404, "override not found")
    db.delete(row)
    db.add(
        AuditLog(
            action="settings.override_remove",
            detail={"scope_type": row.scope_type, "scope_id": row.scope_id, "key": row.key},
        )
    )
    db.commit()
    return {"ok": True}


@app.get("/api/admin/audit-logs", dependencies=[Depends(admin)])
def admin_audit_logs(
    db: Session = Depends(db_dep),
    limit: int = 100,
    action: str = "",
    user_id: str = "",
):
    query = select(AuditLog)
    if action:
        query = query.where(AuditLog.action.contains(action))
    if user_id:
        query = query.where(AuditLog.user_id == user_id)
    rows = db.scalars(
        query.order_by(AuditLog.created_at.desc()).limit(min(limit, 500))
    )
    return [
        {
            "id": row.id,
            "user_id": row.user_id,
            "action": row.action,
            "detail": row.detail,
            "created_at": row.created_at,
        }
        for row in rows
    ]


# ================= 用户自助中心（/api/me/*） =================

class CardCreateIn(BaseModel):
    name: str
    content: str = ""


class CardUpdateIn(BaseModel):
    content: str


class CardImportIn(BaseModel):
    name: str
    png_base64: str


class PresetSaveIn(BaseModel):
    name: str


class MemoryAddIn(BaseModel):
    content: str


class ProviderIn(BaseModel):
    provider_key: str
    base_url: str
    api_key: str
    models: list[str] = []


class ProviderUpdateIn(BaseModel):
    provider_key: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    models: list[str] | None = None


class PasswordChangeIn(BaseModel):
    old_password: str
    new_password: str


def _card_meta(card: CharacterCard) -> dict:
    return {
        "id": card.id,
        "name": card.name,
        "format": card.format,
        "active": card.active,
        "created_at": card.created_at,
        "updated_at": card.updated_at,
    }


def _user_settings_for(db: Session, user_id: str) -> UserSettings:
    row = db.get(UserSettings, user_id)
    if not row:
        row = UserSettings(user_id=user_id)
        db.add(row)
        db.flush()
    return row


@app.get("/api/me/cards")
def my_cards(principal: Principal = Depends(current_user), db: Session = Depends(db_dep)):
    assert principal.user_id is not None
    rows = db.scalars(
        select(CharacterCard)
        .where(CharacterCard.user_id == principal.user_id)
        .order_by(CharacterCard.created_at)
    )
    result = []
    for row in rows:
        meta = _card_meta(row)
        preview = ""
        if row.content_encrypted:
            try:
                preview = decrypt_card_content(row.content_encrypted)[:60]
            except Exception:  # noqa: BLE001 - 密钥轮换时预览为空
                preview = ""
        meta["content_preview"] = preview
        result.append(meta)
    return result


@app.get("/api/me/cards/{card_id}")
def my_card_detail(
    card_id: str,
    principal: Principal = Depends(current_user),
    db: Session = Depends(db_dep),
):
    assert principal.user_id is not None
    card = db.scalar(
        select(CharacterCard).where(
            CharacterCard.id == card_id, CharacterCard.user_id == principal.user_id
        )
    )
    if not card:
        raise HTTPException(404, "card not found")
    content = ""
    if card.content_encrypted:
        try:
            content = decrypt_card_content(card.content_encrypted)
        except Exception:  # noqa: BLE001
            content = ""
    return {**_card_meta(card), "content": content}


@app.post("/api/me/cards")
def my_card_create(
    body: CardCreateIn,
    principal: Principal = Depends(current_user),
    db: Session = Depends(db_dep),
):
    assert principal.user_id is not None
    name = body.name.strip()[:120]
    if not name:
        raise HTTPException(400, "name is required")
    limit = int(get_runtime_value(db, "max_cards_per_user"))
    count = db.scalar(
        select(func.count()).select_from(CharacterCard).where(
            CharacterCard.user_id == principal.user_id
        )
    )
    if count >= limit:
        raise HTTPException(400, f"角色卡数量已达上限（{limit} 张）")
    fmt = detect_format(body.content) if body.content else "soul_md"
    content_encrypted = encrypt_card_content(body.content[: int(get_runtime_value(db, "card_max_chars"))]) if body.content else None
    card = CharacterCard(
        user_id=principal.user_id,
        name=name,
        format=fmt,
        content_encrypted=content_encrypted,
        active=False,
    )
    db.add(card)
    db.commit()
    return _card_meta(card)


@app.put("/api/me/cards/{card_id}")
def my_card_update(
    card_id: str,
    body: CardUpdateIn,
    principal: Principal = Depends(current_user),
    db: Session = Depends(db_dep),
):
    assert principal.user_id is not None
    card = db.scalar(
        select(CharacterCard).where(
            CharacterCard.id == card_id, CharacterCard.user_id == principal.user_id
        )
    )
    if not card:
        raise HTTPException(404, "card not found")
    card.content_encrypted = encrypt_card_content(
        body.content[: int(get_runtime_value(db, "card_max_chars"))]
    )
    card.format = detect_format(body.content)
    db.commit()
    return _card_meta(card)


@app.post("/api/me/cards/{card_id}/activate")
def my_card_activate(
    card_id: str,
    principal: Principal = Depends(current_user),
    db: Session = Depends(db_dep),
):
    assert principal.user_id is not None
    card = db.scalar(
        select(CharacterCard).where(
            CharacterCard.id == card_id, CharacterCard.user_id == principal.user_id
        )
    )
    if not card:
        raise HTTPException(404, "card not found")
    db.execute(
        CharacterCard.__table__.update()
        .where(CharacterCard.user_id == principal.user_id, CharacterCard.active.is_(True))
        .values(active=False)
    )
    card.active = True
    _user_settings_for(db, principal.user_id).active_card_id = card.id
    db.commit()
    return {"ok": True}


@app.delete("/api/me/cards/{card_id}")
def my_card_delete(
    card_id: str,
    principal: Principal = Depends(current_user),
    db: Session = Depends(db_dep),
):
    assert principal.user_id is not None
    card = db.scalar(
        select(CharacterCard).where(
            CharacterCard.id == card_id, CharacterCard.user_id == principal.user_id
        )
    )
    if not card:
        raise HTTPException(404, "card not found")
    db.delete(card)
    db.commit()
    return {"ok": True}


@app.post("/api/me/cards/import")
def my_card_import(
    body: CardImportIn,
    principal: Principal = Depends(current_user),
    db: Session = Depends(db_dep),
):
    assert principal.user_id is not None
    name = body.name.strip()[:120]
    if not name:
        raise HTTPException(400, "name is required")
    try:
        import base64

        data = base64.b64decode(body.png_base64)
        content = extract_card_from_png(data)
    except Exception as exc:
        raise HTTPException(400, "无法解析 PNG 角色卡") from exc
    if not content:
        raise HTTPException(400, "PNG 中未发现角色卡内容")
    limit = int(get_runtime_value(db, "max_cards_per_user"))
    count = db.scalar(
        select(func.count()).select_from(CharacterCard).where(
            CharacterCard.user_id == principal.user_id
        )
    )
    if count >= limit:
        raise HTTPException(400, f"角色卡数量已达上限（{limit} 张）")
    fmt = detect_format(content)
    card = CharacterCard(
        user_id=principal.user_id,
        name=name,
        format=fmt,
        content_encrypted=encrypt_card_content(content[: int(get_runtime_value(db, "card_max_chars"))]),
        active=False,
    )
    db.add(card)
    db.commit()
    return _card_meta(card)


@app.get("/api/me/presets")
def my_presets(principal: Principal = Depends(current_user), db: Session = Depends(db_dep)):
    assert principal.user_id is not None
    return [
        {"id": row.id, "name": row.name, "created_at": row.created_at}
        for row in preset_service.list_presets(db, principal.user_id)
    ]


@app.post("/api/me/presets")
def my_preset_save(
    body: PresetSaveIn,
    principal: Principal = Depends(current_user),
    db: Session = Depends(db_dep),
):
    assert principal.user_id is not None
    name = body.name.strip()[:120]
    if not name:
        raise HTTPException(400, "name is required")
    existing = preset_service.list_presets(db, principal.user_id)
    limit = int(get_runtime_value(db, "max_presets_per_user"))
    if not any(row.name == name for row in existing) and len(existing) >= limit:
        raise HTTPException(400, f"预设数量已达上限（{limit} 个）")
    settings_row = _user_settings_for(db, principal.user_id)
    preset_service.save_preset(
        db, principal.user_id, name, preset_service.snapshot_settings(settings_row)
    )
    db.commit()
    return {"ok": True, "name": name}


@app.post("/api/me/presets/{preset_id}/apply")
def my_preset_apply(
    preset_id: str,
    principal: Principal = Depends(current_user),
    db: Session = Depends(db_dep),
):
    assert principal.user_id is not None
    row = db.scalar(
        select(Preset).where(Preset.id == preset_id, Preset.user_id == principal.user_id)
    )
    if not row:
        raise HTTPException(404, "preset not found")
    preset_service.apply_snapshot(db, _user_settings_for(db, principal.user_id), row.config)
    db.commit()
    return {"ok": True}


@app.delete("/api/me/presets/{preset_id}")
def my_preset_delete(
    preset_id: str,
    principal: Principal = Depends(current_user),
    db: Session = Depends(db_dep),
):
    assert principal.user_id is not None
    row = db.scalar(
        select(Preset).where(Preset.id == preset_id, Preset.user_id == principal.user_id)
    )
    if not row:
        raise HTTPException(404, "preset not found")
    db.delete(row)
    db.commit()
    return {"ok": True}


@app.get("/api/me/memories")
def my_memories(principal: Principal = Depends(current_user), db: Session = Depends(db_dep)):
    assert principal.user_id is not None
    rows = db.scalars(
        select(Memory).where(Memory.user_id == principal.user_id).order_by(Memory.created_at)
    )
    return [
        {"id": row.id, "content": memory_text(row), "created_at": row.created_at}
        for row in rows
    ]


@app.post("/api/me/memories")
def my_memory_add(
    body: MemoryAddIn,
    principal: Principal = Depends(current_user),
    db: Session = Depends(db_dep),
):
    assert principal.user_id is not None
    limit = int(get_runtime_value(db, "max_memories_per_user"))
    count = db.scalar(
        select(func.count()).select_from(Memory).where(Memory.user_id == principal.user_id)
    )
    if count >= limit:
        raise HTTPException(400, f"记忆条数已达上限（{limit} 条）")
    max_chars = int(get_runtime_value(db, "memory_max_chars"))
    from .security import encrypt_secret

    row = Memory(
        user_id=principal.user_id,
        content="",
        content_encrypted=encrypt_secret(body.content[:max_chars]),
    )
    db.add(row)
    db.commit()
    return {"ok": True, "id": row.id}


@app.delete("/api/me/memories/{memory_id}")
def my_memory_delete(
    memory_id: str,
    principal: Principal = Depends(current_user),
    db: Session = Depends(db_dep),
):
    assert principal.user_id is not None
    row = db.scalar(
        select(Memory).where(Memory.id == memory_id, Memory.user_id == principal.user_id)
    )
    if not row:
        raise HTTPException(404, "memory not found")
    db.delete(row)
    db.commit()
    return {"ok": True}


@app.post("/api/me/memories/clear")
def my_memory_clear(
    principal: Principal = Depends(current_user), db: Session = Depends(db_dep)
):
    assert principal.user_id is not None
    db.execute(delete(Memory).where(Memory.user_id == principal.user_id))
    db.commit()
    return {"ok": True}


@app.get("/api/me/providers")
def my_providers(principal: Principal = Depends(current_user), db: Session = Depends(db_dep)):
    assert principal.user_id is not None
    rows = db.scalars(
        select(UserProvider)
        .where(UserProvider.user_id == principal.user_id)
        .order_by(UserProvider.created_at)
    )
    return [
        {
            "id": p.id,
            "provider_key": p.provider_key,
            "base_url": p.base_url,
            "models": p.models,
            "is_default": p.is_default,
            "created_at": p.created_at,
        }
        for p in rows
    ]


@app.post("/api/me/providers")
def my_provider_add(
    body: ProviderIn,
    principal: Principal = Depends(current_user),
    db: Session = Depends(db_dep),
):
    assert principal.user_id is not None
    if not body.provider_key.strip() or not body.api_key.strip():
        raise HTTPException(400, "provider_key 与 api_key 必填")
    if not body.base_url.startswith("https://"):
        raise HTTPException(400, "base_url 必须是 https 地址")
    limit = int(get_runtime_value(db, "max_providers_per_user"))
    count = db.scalar(
        select(func.count()).select_from(UserProvider).where(
            UserProvider.user_id == principal.user_id
        )
    )
    if count >= limit:
        raise HTTPException(400, f"自带供应商已达上限（{limit} 个）")
    from .security import encrypt_secret

    row = UserProvider(
        user_id=principal.user_id,
        provider_key=body.provider_key.strip()[:80],
        base_url=body.base_url.strip()[:500],
        api_key_encrypted=encrypt_secret(body.api_key),
        models=[m.strip()[:160] for m in body.models if m.strip()][:50],
    )
    db.add(row)
    db.commit()
    return {"ok": True, "id": row.id}


@app.delete("/api/me/providers/{provider_id}")
def my_provider_delete(
    provider_id: str,
    principal: Principal = Depends(current_user),
    db: Session = Depends(db_dep),
):
    assert principal.user_id is not None
    row = db.scalar(
        select(UserProvider).where(
            UserProvider.id == provider_id, UserProvider.user_id == principal.user_id
        )
    )
    if not row:
        raise HTTPException(404, "provider not found")
    settings_row = db.get(UserSettings, principal.user_id)
    if settings_row and settings_row.provider_key == f"byok:{row.id}":
        settings_row.provider_key = None
    db.delete(row)
    db.commit()
    return {"ok": True}


@app.post("/api/me/password")
def my_password_change(
    body: PasswordChangeIn,
    authorization: str | None = Header(None),
    principal: Principal = Depends(current_user),
    db: Session = Depends(db_dep),
):
    assert principal.user_id is not None
    from .security import verify_password

    account = db.scalar(
        select(Account).where(Account.user_id == principal.user_id, Account.is_active.is_(True))
    )
    if not account or not verify_password(body.old_password, account.password_hash):
        raise HTTPException(400, "原密码不正确")
    account.password_hash = hash_password(
        body.new_password, min_length=int(get_runtime_value(db, "password_min_length"))
    )
    db.commit()
    # 撤销该账号其他会话（保留当前会话）
    token = (authorization or "").removeprefix("Bearer ").strip()
    from .auth import _token_hash

    db.execute(
        delete(AuthSession).where(
            AuthSession.account_id == account.id,
            AuthSession.token_hash != _token_hash(token),
        )
    )
    db.commit()
    return {"ok": True}


@app.get("/api/me/sessions")
def my_sessions(principal: Principal = Depends(current_user), db: Session = Depends(db_dep)):
    assert principal.user_id is not None
    rows = db.scalars(
        select(AuthSession)
        .where(AuthSession.user_id == principal.user_id)
        .order_by(AuthSession.created_at.desc())
    )
    return [
        {"id": row.id, "role": row.role, "expires_at": row.expires_at, "created_at": row.created_at}
        for row in rows
    ]


@app.post("/api/me/sessions/revoke-all")
def my_sessions_revoke_all(
    authorization: str | None = Header(None),
    principal: Principal = Depends(current_user),
    db: Session = Depends(db_dep),
):
    assert principal.user_id is not None
    from .auth import _token_hash

    token = (authorization or "").removeprefix("Bearer ").strip()
    db.execute(
        delete(AuthSession).where(
            AuthSession.user_id == principal.user_id,
            AuthSession.token_hash != _token_hash(token),
        )
    )
    db.commit()
    return {"ok": True}


@app.get("/api/me/usage")
def my_usage(
    principal: Principal = Depends(current_user),
    db: Session = Depends(db_dep),
    days: int = 7,
):
    assert principal.user_id is not None
    start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
        days=max(1, min(days, 30)) - 1
    )
    rows = db.execute(
        select(
            func.date(UsageRecord.created_at).label("day"),
            func.coalesce(
                func.sum(UsageRecord.prompt_tokens + UsageRecord.completion_tokens), 0
            ).label("tokens"),
        )
        .where(UsageRecord.user_id == principal.user_id, UsageRecord.created_at >= start)
        .group_by("day")
        .order_by("day")
    )
    return [{"date": day, "tokens": int(tokens)} for day, tokens in rows]


@app.get("/api/admin/sessions", dependencies=[Depends(admin)])
def admin_sessions(
    db: Session = Depends(db_dep),
    limit: int = 100,
    user_id: str = "",
):
    query = select(AuthSession)
    if user_id:
        query = query.where(AuthSession.user_id == user_id)
    rows = db.scalars(query.order_by(AuthSession.created_at.desc()).limit(min(limit, 500)))
    accounts = {
        account.id: account.username
        for account in db.scalars(select(Account))
    }
    return {
        "sessions": [
            {
                "id": row.id,
                "user_id": row.user_id,
                "account_username": accounts.get(row.account_id) if row.account_id else None,
                "role": row.role,
                "expires_at": row.expires_at,
                "created_at": row.created_at,
            }
            for row in rows
        ]
    }


@app.post("/api/admin/sessions/{session_id}/revoke", dependencies=[Depends(admin)])
def admin_revoke_session(session_id: str, db: Session = Depends(db_dep)):
    row = db.get(AuthSession, session_id)
    if not row:
        raise HTTPException(404, "session not found")
    db.delete(row)
    db.add(AuditLog(action="session.revoke", detail={"session_id": session_id}))
    db.commit()
    return {"ok": True}


@app.post("/api/admin/users/{user_id}/sessions/revoke-all", dependencies=[Depends(admin)])
def admin_revoke_all_user_sessions(user_id: str, db: Session = Depends(db_dep)):
    if not db.get(User, user_id):
        raise HTTPException(404, "user not found")
    count = db.query(AuthSession).filter(AuthSession.user_id == user_id).delete()
    db.add(AuditLog(action="session.revoke_all", detail={"user_id": user_id, "count": count}))
    db.commit()
    return {"ok": True, "revoked": count}


@app.delete("/api/admin/users/{user_id}/mfa", dependencies=[Depends(admin)])
def admin_reset_user_mfa(user_id: str, db: Session = Depends(db_dep)):
    account = db.scalar(select(Account).where(Account.user_id == user_id))
    if not account:
        raise HTTPException(404, "account not found")
    subject = account_subject(account)
    if not credential_for(db, subject.subject_type, subject.subject_id):
        raise HTTPException(404, "mfa not configured")
    remove_credential(db, subject)
    db.add(
        AuditLog(
            user_id=user_id,
            action="mfa.admin_reset",
            detail={"subject_type": "account"},
        )
    )
    db.commit()
    return {"ok": True}


@app.get("/api/admin/stats", dependencies=[Depends(admin)])
def stats(db: Session = Depends(db_dep)):
    return {
        "users": db.scalar(select(func.count()).select_from(User)),
        "messages": db.scalar(select(func.count()).select_from(Message)),
        "failed": db.scalar(
            select(func.count()).select_from(Message).where(Message.status == MessageStatus.failed)
        ),
        "tokens": db.scalar(
            select(func.coalesce(func.sum(UsageRecord.prompt_tokens + UsageRecord.completion_tokens), 0))
        ),
        "mode": resolve_user_mode(db, None),
        "single_user_mode": settings.single_user_mode,
    }


@app.get("/api/admin/usage/trend", dependencies=[Depends(admin)])
def usage_trend(db: Session = Depends(db_dep), days: int = 7):
    days = max(1, min(days, 90))
    start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)
    rows = db.execute(
        select(
            func.date(UsageRecord.created_at).label("day"),
            func.sum(UsageRecord.prompt_tokens + UsageRecord.completion_tokens).label("tokens"),
        )
        .where(UsageRecord.created_at >= start)
        .group_by(func.date(UsageRecord.created_at))
        .order_by(func.date(UsageRecord.created_at))
    ).all()
    by_day = {str(r.day): int(r.tokens or 0) for r in rows}
    return [
        {
            "date": (start + timedelta(days=i)).date().isoformat(),
            "tokens": int(by_day.get((start + timedelta(days=i)).date().isoformat(), 0)),
        }
        for i in range(days)
    ]


@app.get("/api/admin/users", dependencies=[Depends(admin)])
def users(db: Session = Depends(db_dep), limit: int = 50):
    rows = db.execute(
        select(User, UserSettings, Account)
        .outerjoin(UserSettings)
        .outerjoin(Account, Account.user_id == User.id)
        .order_by(User.created_at.desc())
        .limit(min(limit, 200))
    ).all()
    identities_map: dict[str, list[ChannelIdentity]] = {}
    model_group_names = {
        group.id: group.name for group in db.scalars(select(ModelGroup))
    }
    for identity, uid in db.execute(
        select(ChannelIdentity, ChannelIdentity.user_id)
    ).all():
        identities_map.setdefault(uid, []).append(identity)
    result = []
    for u, s, a in rows:
        id_list = []
        for identity in identities_map.get(u.id, []):
            try:
                external = decrypt_secret(identity.external_id_encrypted)
            except Exception:  # noqa: BLE001 - 密钥轮换等情况下仅显示未知
                external = ""
            masked = _mask_external_id(external)
            id_list.append({"channel": identity.channel, "masked": masked})
        result.append(
            {
                "id": u.id,
                "display_name": u.display_name,
                "blocked": u.is_blocked,
                "created_at": u.created_at,
                "model": s.model if s else None,
                "model_group_id": s.model_group_id if s else None,
                "model_group_name": model_group_names.get(s.model_group_id) if s else None,
                "mode": u.mode,
                "effective_mode": resolve_user_mode(db, u),
                "account_username": a.username if a else None,
                "identities": id_list,
            }
        )
    return result


def _mask_external_id(external_id: str) -> str:
    """渠道外部 ID 脱敏：保留头部与尾部少量字符，中间打码。"""
    if not external_id:
        return "未知"
    if len(external_id) <= 8:
        return "****" + external_id[-2:]
    return external_id[:4] + "****" + external_id[-4:]


@app.put("/api/admin/users/{user_id}/display-name", dependencies=[Depends(admin)])
def user_display_name(user_id: str, body: DisplayNameIn, db: Session = Depends(db_dep)):
    u = db.get(User, user_id)
    if not u:
        raise HTTPException(404, "user not found")
    name = (body.display_name or "").strip()[:255]
    u.display_name = name or None
    db.add(AuditLog(action="user.rename", detail={"user_id": user_id}))
    db.commit()
    return {"ok": True, "display_name": u.display_name}


@app.put("/api/admin/users/{user_id}/account", dependencies=[Depends(admin)])
def provision_user_account(
    user_id: str, body: AccountProvisionIn, db: Session = Depends(db_dep)
):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "user not found")
    try:
        username = normalize_username(body.username)
        password_hash = hash_password(body.password, min_length=int(get_runtime_value(db, "password_min_length")))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if username == settings.admin_username.strip().lower():
        raise HTTPException(409, "用户名已存在")
    conflict = db.scalar(
        select(Account).where(Account.username == username, Account.user_id != user_id)
    )
    if conflict:
        raise HTTPException(409, "用户名已存在")
    account = db.scalar(select(Account).where(Account.user_id == user_id))
    if account:
        account.username = username
        account.password_hash = password_hash
        account.is_active = True
        db.execute(delete(AuthSession).where(AuthSession.account_id == account.id))
    else:
        account = Account(
            user_id=user_id,
            username=username,
            password_hash=password_hash,
            role="user",
            is_active=True,
        )
        db.add(account)
    db.commit()
    return {"user_id": user_id, "username": username, "is_active": True}


@app.post("/api/admin/users/{user_id}/block", dependencies=[Depends(admin)])
def block_user(user_id: str, blocked: bool = True, db: Session = Depends(db_dep)):
    u = db.get(User, user_id)
    if not u:
        raise HTTPException(404, "user not found")
    u.is_blocked = blocked
    if blocked:
        db.execute(delete(AuthSession).where(AuthSession.user_id == user_id))
    db.commit()
    return {"ok": True, "blocked": u.is_blocked}


@app.get("/api/admin/messages", dependencies=[Depends(admin)])
def messages(db: Session = Depends(db_dep), limit: int = 100):
    rows = db.scalars(select(Message).order_by(Message.created_at.desc()).limit(min(limit, 500)))
    return [
        {
            "id": m.id,
            "user_id": m.user_id,
            "direction": m.direction,
            "type": m.message_type,
            "status": m.status,
            "content": m.content,
            "created_at": m.created_at,
            "error": m.error,
        }
        for m in rows
    ]


@app.get("/api/admin/tasks/dead", dependencies=[Depends(admin)])
def dead_tasks(db: Session = Depends(db_dep), limit: int = 100):
    rows = db.scalars(
        select(OutboxTask)
        .where(OutboxTask.status == OutboxStatus.dead)
        .order_by(OutboxTask.updated_at.desc())
        .limit(min(limit, 500))
    )
    return [
        {
            "id": task.id,
            "type": task.task_type,
            "attempts": task.attempts,
            "payload": task.payload,
            "error": task.last_error,
            "updated_at": task.updated_at,
        }
        for task in rows
    ]


@app.get("/api/admin/media", dependencies=[Depends(admin)])
def media_metadata(db: Session = Depends(db_dep), limit: int = 100):
    """媒体元数据审计：不含 storage_key/URL 等可能携带凭据的字段。"""
    return list_media_metadata(db, limit=limit)


@app.post("/api/admin/tasks/{task_id}/replay", dependencies=[Depends(admin)])
def replay_dead_task(task_id: str):
    if not replay_task(task_id):
        raise HTTPException(409, "task is missing or not dead")
    return {"ok": True, "task_id": task_id}


class ModeIn(BaseModel):
    mode: str | None = None  # self_service | managed | null(清除用户覆盖)


class PolicyIn(BaseModel):
    command: str
    channel: str | None = None  # None=全部渠道
    allowed: bool = True
    silent_block: bool = False
    blocked_strategy: Literal["redirect_to_ai", "ignore"] = "redirect_to_ai"


class ChannelInstanceIn(BaseModel):
    """仅保存公开实例配置；登录态和会话凭据只能由桥接服务加密写入。"""

    channel: Literal["wechat_clawbot"]
    instance_name: str
    owner_user_id: str | None = None
    config: dict = {}


class ChannelMessageIn(BaseModel):
    """桥接服务投递的规范化入站消息；端点只面向受保护的内部网络。"""

    sender_id: str
    external_message_id: str
    message_type: str = "text"
    content: str | None = None
    media: list[dict] = []
    raw: dict = {}


class ChannelStatusIn(BaseModel):
    """桥接服务上报的非敏感实例状态；额外字段会被忽略。"""

    status: Literal["pending_login", "online", "offline", "error"]
    qrcode_url: str | None = None
    account_id: str | None = None
    error: str | None = None


def _channel_instance_view(instance: ChannelInstance) -> dict:
    """管理端只返回非敏感元数据，绝不返回 session_encrypted/login_state 原文。"""
    result = {
        "id": instance.id,
        "channel": instance.channel,
        "instance_name": instance.instance_name,
        "owner_user_id": instance.owner_user_id,
        "status": instance.status,
        "config": instance.config,
        "created_at": instance.created_at,
        "updated_at": instance.updated_at,
    }
    safe_login = {
        key: value
        for key, value in (instance.login_state or {}).items()
        if key in {"status", "qrcode_url", "account_id", "error"}
    }
    if safe_login.get("qrcode_url"):
        safe_login["qrcode_available"] = True
        safe_login.pop("qrcode_url", None)
    if safe_login:
        result["login"] = safe_login
    return result


@app.get("/api/admin/channel-instances", dependencies=[Depends(admin)])
def channel_instances(db: Session = Depends(db_dep)):
    rows = db.scalars(select(ChannelInstance).order_by(ChannelInstance.created_at.desc()))
    return [_channel_instance_view(instance) for instance in rows]


@app.post("/api/admin/channel-instances", dependencies=[Depends(admin)])
def create_channel_instance(body: ChannelInstanceIn, db: Session = Depends(db_dep)):
    if not body.instance_name.strip():
        raise HTTPException(400, "instance_name is required")
    if body.owner_user_id and not db.get(User, body.owner_user_id):
        raise HTTPException(404, "owner user not found")
    try:
        registry.get(body.channel)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    instance = ChannelInstance(
        channel=body.channel,
        instance_name=body.instance_name.strip()[:120],
        owner_user_id=body.owner_user_id,
        config=body.config,
        login_state={},
        status="offline",
    )
    db.add(instance)
    db.commit()
    db.refresh(instance)
    return _channel_instance_view(instance)


async def _change_channel_instance_status(instance_id: str, action: Literal["start", "stop"]) -> dict:
    """桥接调用完成后才写状态，失败时保存可见但不含敏感信息的 error 状态。"""
    db = SessionLocal()
    try:
        instance = db.get(ChannelInstance, instance_id)
        if not instance:
            raise HTTPException(404, "channel instance not found")
        try:
            adapter = registry.get(instance.channel)
            if action == "start":
                instance.status = "logging_in"
                db.commit()
                bridge_state = await adapter.start_instance(instance.id)
                safe_login = {
                    key: value
                    for key, value in (bridge_state or {}).items()
                    if key in {"status", "qrcode_url", "account_id", "error"}
                }
                instance.login_state = safe_login
                instance.status = (
                    "online" if safe_login.get("status") == "online" else "logging_in"
                )
            else:
                await adapter.stop_instance(instance.id)
                instance.status = "offline"
                instance.login_state = {}
            db.commit()
        except Exception as exc:
            db.rollback()
            instance = db.get(ChannelInstance, instance_id)
            if instance:
                instance.status = "error"
                db.commit()
            log.error(
                "渠道实例操作失败 id=%s action=%s error=%s",
                instance_id,
                action,
                redact_error(exc, 300),
            )
            raise HTTPException(502, "channel bridge operation failed") from exc
        return _channel_instance_view(instance)
    finally:
        db.close()


@app.post("/api/admin/channel-instances/{instance_id}/start", dependencies=[Depends(admin)])
async def start_channel_instance(instance_id: str):
    return await _change_channel_instance_status(instance_id, "start")


@app.post("/api/admin/channel-instances/{instance_id}/stop", dependencies=[Depends(admin)])
async def stop_channel_instance(instance_id: str):
    return await _change_channel_instance_status(instance_id, "stop")


class MyChannelInstanceIn(BaseModel):
    instance_name: str


def _owned_instance(db: Session, instance_id: str, user_id: str) -> ChannelInstance:
    instance = db.scalar(
        select(ChannelInstance).where(
            ChannelInstance.id == instance_id,
            ChannelInstance.owner_user_id == user_id,
        )
    )
    if not instance:
        raise HTTPException(404, "channel instance not found")
    return instance


def _qrcode_response(instance: ChannelInstance) -> Response:
    qrcode_url = (instance.login_state or {}).get("qrcode_url")
    if not qrcode_url:
        raise HTTPException(404, "login qrcode is not available")
    parsed = urlsplit(qrcode_url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise HTTPException(404, "login qrcode is not available")
    output = BytesIO()
    qrcode.make(qrcode_url, image_factory=SvgPathImage).save(output)
    return Response(
        output.getvalue(),
        media_type="image/svg+xml",
        headers={"Cache-Control": "no-store, private"},
    )


@app.get("/api/admin/channel-instances/{instance_id}/qrcode", dependencies=[Depends(admin)])
def admin_channel_instance_qrcode(instance_id: str, db: Session = Depends(db_dep)):
    instance = db.get(ChannelInstance, instance_id)
    if not instance:
        raise HTTPException(404, "channel instance not found")
    return _qrcode_response(instance)


@app.get("/api/me/channel-instances")
def my_channel_instances(
    principal: Principal = Depends(current_user), db: Session = Depends(db_dep)
):
    assert principal.user_id is not None
    rows = db.scalars(
        select(ChannelInstance)
        .where(ChannelInstance.owner_user_id == principal.user_id)
        .order_by(ChannelInstance.created_at.desc())
    )
    return [_channel_instance_view(row) for row in rows]


@app.post("/api/me/channel-instances")
def create_my_channel_instance(
    body: MyChannelInstanceIn,
    principal: Principal = Depends(current_user),
    db: Session = Depends(db_dep),
):
    assert principal.user_id is not None
    if not bool(get_runtime_value(db, "allow_user_clawbot_instances")):
        raise HTTPException(403, "当前平台未开放用户自助创建实例")
    name = body.instance_name.strip()[:120]
    if not name:
        raise HTTPException(400, "instance_name is required")
    instance = ChannelInstance(
        channel="wechat_clawbot",
        instance_name=name,
        owner_user_id=principal.user_id,
        config={},
        login_state={},
        status="offline",
    )
    db.add(instance)
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(409, "实例名称已存在") from exc
    db.refresh(instance)
    return _channel_instance_view(instance)


@app.post("/api/me/channel-instances/{instance_id}/start")
async def start_my_channel_instance(
    instance_id: str,
    principal: Principal = Depends(current_user),
    db: Session = Depends(db_dep),
):
    assert principal.user_id is not None
    _owned_instance(db, instance_id, principal.user_id)
    result = await _change_channel_instance_status(instance_id, "start")
    result.get("login", {}).pop("qrcode_url", None)
    return result


@app.post("/api/me/channel-instances/{instance_id}/stop")
async def stop_my_channel_instance(
    instance_id: str,
    principal: Principal = Depends(current_user),
    db: Session = Depends(db_dep),
):
    assert principal.user_id is not None
    _owned_instance(db, instance_id, principal.user_id)
    return await _change_channel_instance_status(instance_id, "stop")


@app.get("/api/me/channel-instances/{instance_id}/qrcode")
def my_channel_instance_qrcode(
    instance_id: str,
    principal: Principal = Depends(current_user),
    db: Session = Depends(db_dep),
):
    assert principal.user_id is not None
    instance = _owned_instance(db, instance_id, principal.user_id)
    return _qrcode_response(instance)


@app.post("/api/internal/channel-instances/{instance_id}/messages", dependencies=[Depends(bridge_auth)])
def receive_channel_message(instance_id: str, body: ChannelMessageIn, db: Session = Depends(db_dep)):
    """受保护的桥接入口：复用统一身份映射与消息 Outbox，不暴露给终端用户。"""
    instance = db.get(ChannelInstance, instance_id)
    if not instance:
        raise HTTPException(404, "channel instance not found")
    if instance.status != "online":
        raise HTTPException(409, "channel instance is not online")
    message = ingest_channel_message(
        db,
        ChannelMessage(
            channel=instance.channel,
            instance_id=instance.id,
            sender_id=body.sender_id,
            external_message_id=body.external_message_id,
            message_type=body.message_type,
            content=body.content,
            media=body.media,
            raw=body.raw,
        ),
    )
    db.commit()
    return {"ok": True, "accepted": message is not None, "message_id": message.id if message else None}


@app.post("/api/internal/channel-instances/{instance_id}/status", dependencies=[Depends(bridge_auth)])
def receive_channel_status(instance_id: str, body: ChannelStatusIn, db: Session = Depends(db_dep)):
    """接受桥接侧生命周期回调；仅保存白名单状态，不接受任何会话凭据。"""
    instance = db.get(ChannelInstance, instance_id)
    if not instance:
        raise HTTPException(404, "channel instance not found")
    state = body.model_dump(exclude_none=True)
    qrcode_url = state.get("qrcode_url")
    if qrcode_url:
        parsed = urlsplit(qrcode_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            state.pop("qrcode_url", None)
    instance.login_state = state
    instance.status = "logging_in" if body.status == "pending_login" else body.status
    db.commit()
    db.refresh(instance)
    return _channel_instance_view(instance)


@app.get("/api/admin/mode", dependencies=[Depends(admin)])
def get_platform_mode(db: Session = Depends(db_dep)):
    row = db.get(PlatformConfig, "mode")
    return {
        "platform_mode": settings.platform_mode,
        "configured_mode": (row.value or {}).get("mode") if row else None,
        "single_user_mode": settings.single_user_mode,
    }


@app.post("/api/admin/mode", dependencies=[Depends(admin)])
def set_platform_mode(body: ModeIn, db: Session = Depends(db_dep)):
    if body.mode not in {None, "self_service", "managed"}:
        raise HTTPException(400, "mode must be self_service|managed|null")
    if body.mode is None:
        existing = db.get(PlatformConfig, "mode")
        if existing:
            db.delete(existing)
    else:
        row = db.get(PlatformConfig, "mode")
        if row:
            row.value = {"mode": body.mode}
        else:
            db.add(PlatformConfig(key="mode", value={"mode": body.mode}))
    db.commit()
    return {"ok": True, "mode": body.mode}


@app.get("/api/admin/users/{user_id}/mode", dependencies=[Depends(admin)])
def get_user_mode(user_id: str, db: Session = Depends(db_dep)):
    u = db.get(User, user_id)
    if not u:
        raise HTTPException(404, "user not found")
    return {"user_id": user_id, "mode": u.mode, "effective_mode": resolve_user_mode(db, u)}


@app.post("/api/admin/users/{user_id}/mode", dependencies=[Depends(admin)])
def set_user_mode(user_id: str, body: ModeIn, db: Session = Depends(db_dep)):
    u = db.get(User, user_id)
    if not u:
        raise HTTPException(404, "user not found")
    if body.mode not in {None, "self_service", "managed"}:
        raise HTTPException(400, "mode must be self_service|managed|null")
    summary = migrate_user_mode(db, u, body.mode)
    db.commit()
    return {"ok": True, "user_id": user_id, "mode": u.mode, "migration": summary}


@app.get("/api/admin/users/{user_id}/cards", dependencies=[Depends(admin)])
def user_cards(user_id: str, db: Session = Depends(db_dep)):
    if not db.get(User, user_id):
        raise HTTPException(404, "user not found")
    rows = db.scalars(
        select(CharacterCard).where(CharacterCard.user_id == user_id).order_by(CharacterCard.created_at)
    )
    # 只返回元数据：卡内容入库即密文，管理端不提供解密出口
    return [
        {
            "id": c.id,
            "name": c.name,
            "format": c.format,
            "active": c.active,
            "created_at": c.created_at,
            "updated_at": c.updated_at,
        }
        for c in rows
    ]


class CardImportIn(BaseModel):
    """角色卡导入：content 直接给文本（SOUL.md 或 SillyTavern JSON）；
    png_base64 给 PNG 内嵌卡数据（v2/v3 自动提取）。"""

    name: str
    content: str | None = None
    png_base64: str | None = None


@app.post("/api/admin/users/{user_id}/cards/import", dependencies=[Depends(admin)])
def import_user_card(user_id: str, body: CardImportIn, db: Session = Depends(db_dep)):
    """管理员替用户导入角色卡（不读卡内容，仅落库加密）。"""
    if not db.get(User, user_id):
        raise HTTPException(404, "user not found")
    name = body.name.strip()[:120]
    if not name:
        raise HTTPException(400, "name is required")
    if body.content and body.png_base64:
        raise HTTPException(400, "provide content or png_base64, not both")
    raw_content = body.content
    if body.png_base64:
        try:
            import base64

            raw_content = extract_card_from_png(base64.b64decode(body.png_base64))
        except (ValueError, TypeError):
            raise HTTPException(400, "png_base64 is not valid base64") from None
        if not raw_content:
            raise HTTPException(400, "PNG 中未找到 SillyTavern 角色卡（chara/ccv3 chunk）")
    if not raw_content or not raw_content.strip():
        raise HTTPException(400, "content is required")
    content = raw_content.strip()[:20000]
    fmt = detect_format(content)
    exists = db.scalar(
        select(CharacterCard).where(CharacterCard.user_id == user_id, CharacterCard.name == name)
    )
    if exists:
        # 同名导入视为更新：保留激活状态与所有权
        exists.format = fmt
        exists.content_encrypted = encrypt_card_content(content)
        card = exists
    else:
        card = CharacterCard(
            user_id=user_id, name=name, format=fmt, content_encrypted=encrypt_card_content(content)
        )
        db.add(card)
        db.flush()
    db.commit()
    return {
        "ok": True,
        "card": {
            "id": card.id,
            "name": card.name,
            "format": card.format,
            "active": card.active,
            "created_at": card.created_at,
            "updated_at": card.updated_at,
        },
    }


@app.get("/api/admin/users/{user_id}/policies", dependencies=[Depends(admin)])
def user_policies(user_id: str, db: Session = Depends(db_dep)):
    rows = db.scalars(
        select(CommandPolicy)
        .where(or_(CommandPolicy.user_id == user_id, CommandPolicy.user_id.is_(None)))
        .order_by(CommandPolicy.command, CommandPolicy.user_id, CommandPolicy.channel)
    )
    return [
        {
            "id": p.id,
            "user_id": p.user_id,
            "channel": p.channel,
            "command": p.command,
            "allowed": p.allowed,
            "silent_block": p.silent_block,
            "blocked_strategy": p.blocked_strategy,
        }
        for p in rows
    ]


@app.get("/api/admin/users/{user_id}/presets", dependencies=[Depends(admin)])
def user_presets(user_id: str, db: Session = Depends(db_dep)):
    """预设元数据：只暴露名称与更新时间，不暴露快照内容。"""
    if not db.get(User, user_id):
        raise HTTPException(404, "user not found")
    rows = db.scalars(
        select(Preset).where(Preset.user_id == user_id).order_by(Preset.created_at)
    )
    return [
        {
            "id": p.id,
            "name": p.name,
            "created_at": p.created_at,
            "updated_at": p.updated_at,
        }
        for p in rows
    ]


@app.get("/api/admin/users/{user_id}/providers", dependencies=[Depends(admin)])
def user_providers(user_id: str, db: Session = Depends(db_dep)):
    """BYOK 元数据：绝不返回 api_key_encrypted 或任何密钥字段。"""
    if not db.get(User, user_id):
        raise HTTPException(404, "user not found")
    rows = db.scalars(
        select(UserProvider).where(UserProvider.user_id == user_id).order_by(UserProvider.created_at)
    )
    return [
        {
            "id": p.id,
            "provider_key": p.provider_key,
            "base_url": p.base_url,
            "models": p.models,
            "is_default": p.is_default,
            "created_at": p.created_at,
            "updated_at": p.updated_at,
        }
        for p in rows
    ]


@app.get("/api/admin/users/{user_id}/detail", dependencies=[Depends(admin)])
def user_detail(user_id: str, db: Session = Depends(db_dep)):
    """用户详情统计：会话/记忆/媒体/今日用量，均为聚合数字。"""
    u = db.get(User, user_id)
    if not u:
        raise HTTPException(404, "user not found")
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    conversations = db.scalar(
        select(func.count()).select_from(Conversation).where(Conversation.user_id == user_id)
    )
    memories = db.scalar(
        select(func.count()).select_from(Memory).where(Memory.user_id == user_id)
    )
    media = db.scalar(
        select(func.count()).select_from(MediaAsset)
        .join(Message, Message.id == MediaAsset.message_id)
        .where(Message.user_id == user_id)
    )
    tokens_today = db.scalar(
        select(func.coalesce(func.sum(UsageRecord.prompt_tokens + UsageRecord.completion_tokens), 0))
        .where(UsageRecord.user_id == user_id, UsageRecord.created_at >= today)
    )
    account = db.scalar(select(Account).where(Account.user_id == user_id))
    mfa_subject = account_subject(account) if account else None
    mfa_enabled = bool(mfa_subject and enabled_credential(db, mfa_subject))
    return {
        "user_id": user_id,
        "conversations": conversations or 0,
        "memories": memories or 0,
        "media_assets": media or 0,
        "tokens_today": int(tokens_today or 0),
        "mfa_enabled": mfa_enabled,
    }


def _upsert_policy(db: Session, body: PolicyIn, user_id: str | None) -> CommandPolicy:
    command = body.command.lstrip("/").lower()
    if not command:
        raise HTTPException(400, "command is required")
    row = db.scalar(
        select(CommandPolicy).where(
            CommandPolicy.user_id == user_id,
            CommandPolicy.channel == body.channel,
            CommandPolicy.command == command,
        )
    )
    if row:
        row.allowed = body.allowed
        row.silent_block = body.silent_block
        row.blocked_strategy = body.blocked_strategy
    else:
        row = CommandPolicy(
            user_id=user_id,
            channel=body.channel,
            command=command,
            allowed=body.allowed,
            silent_block=body.silent_block,
            blocked_strategy=body.blocked_strategy,
        )
        db.add(row)
    db.flush()
    return row


@app.post("/api/admin/users/{user_id}/policies", dependencies=[Depends(admin)])
def set_user_policy(user_id: str, body: PolicyIn, db: Session = Depends(db_dep)):
    if not db.get(User, user_id):
        raise HTTPException(404, "user not found")
    row = _upsert_policy(db, body, user_id)
    db.commit()
    return {"ok": True, "policy_id": row.id}


@app.post("/api/admin/policies", dependencies=[Depends(admin)])
def set_platform_policy(body: PolicyIn, db: Session = Depends(db_dep)):
    row = _upsert_policy(db, body, None)
    db.commit()
    return {"ok": True, "policy_id": row.id}


@app.delete("/api/admin/policies/{policy_id}", dependencies=[Depends(admin)])
def delete_policy(policy_id: str, db: Session = Depends(db_dep)):
    row = db.get(CommandPolicy, policy_id)
    if not row:
        raise HTTPException(404, "policy not found")
    db.delete(row)
    db.commit()
    return {"ok": True, "deleted": policy_id}
