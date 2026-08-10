"""管理后台的账号、密码与不透明会话认证。"""

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .config import settings
from .db import SessionLocal
from .models import Account, AuthSession, User
from .queueing import redis_client
from .runtime_settings import get_runtime_value
from .security import verify_admin_token, verify_password

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Principal:
    role: str
    user_id: str | None
    username: str


def normalize_username(value: str) -> str:
    username = value.strip().lower()
    if not 3 <= len(username) <= 64:
        raise ValueError("用户名长度必须为 3–64 个字符")
    if not all(ch.isalnum() or ch in {"_", "-", "."} for ch in username):
        raise ValueError("用户名只能包含字母、数字、点、下划线和连字符")
    return username


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(
    db: Session,
    *,
    role: str,
    user_id: str | None,
    account_id: str | None,
) -> str:
    token = secrets.token_urlsafe(32)
    days = int(get_runtime_value(db, "auth_session_days"))
    db.add(
        AuthSession(
            account_id=account_id,
            user_id=user_id,
            token_hash=_token_hash(token),
            role=role,
            expires_at=datetime.now(UTC) + timedelta(days=days),
        )
    )
    db.commit()
    return token


def login(db: Session, username: str, password: str) -> tuple[str, Principal] | None:
    normalized = normalize_username(username)
    if normalized == settings.admin_username.strip().lower() and verify_admin_token(password):
        token = create_session(db, role="admin", user_id=None, account_id=None)
        return token, Principal(role="admin", user_id=None, username=settings.admin_username)
    account = db.scalar(select(Account).where(Account.username == normalized, Account.is_active.is_(True)))
    if (
        not account
        or not verify_password(password, account.password_hash)
        or not (user := db.get(User, account.user_id))
        or user.is_blocked
    ):
        return None
    token = create_session(
        db,
        role=account.role,
        user_id=account.user_id,
        account_id=account.id,
    )
    return token, Principal(role=account.role, user_id=account.user_id, username=account.username)


def _login_lock_key(username: str) -> str:
    return f"login:fail:{username}"


def is_login_locked(username: str) -> bool:
    """Redis 不可用时按未锁定处理（fail-open），避免 Redis 故障锁死全部登录。"""
    try:
        redis = redis_client()
        attempts = int(redis.get(_login_lock_key(username)) or 0)
        max_attempts = int(get_runtime_value(SessionLocal(), "login_max_attempts"))
        return attempts >= max_attempts
    except Exception:  # noqa: BLE001 - Redis 故障或 DB 不可用时不阻塞登录
        return False


def record_login_failure(username: str) -> None:
    try:
        redis = redis_client()
        attempts = redis.incr(_login_lock_key(username))
        lock_minutes = int(get_runtime_value(SessionLocal(), "login_lock_minutes"))
        if attempts == 1:
            redis.expire(_login_lock_key(username), lock_minutes * 60)
    except Exception:  # noqa: BLE001 - 登录失败计数丢失不影响正确性
        log.warning("记录登录失败计数失败 username=%s", username)


def clear_login_failures(username: str) -> None:
    try:
        redis_client().delete(_login_lock_key(username))
    except Exception:  # noqa: BLE001 - 清理失败只影响下一次计数起点
        log.debug("清除登录失败计数失败 username=%s", username)


def principal_from_bearer(db: Session, authorization: str | None) -> Principal | None:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        return None
    now = datetime.now(UTC)
    row = db.scalar(
        select(AuthSession).where(
            AuthSession.token_hash == _token_hash(token),
            AuthSession.expires_at > now,
        )
    )
    if not row:
        return None
    username = settings.admin_username
    if row.account_id:
        account = db.get(Account, row.account_id)
        user = db.get(User, account.user_id) if account else None
        if not account or not account.is_active or not user or user.is_blocked:
            return None
        username = account.username
    return Principal(role=row.role, user_id=row.user_id, username=username)


def revoke_session(db: Session, authorization: str | None) -> None:
    if authorization and authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ").strip()
        db.execute(delete(AuthSession).where(AuthSession.token_hash == _token_hash(token)))
        db.commit()