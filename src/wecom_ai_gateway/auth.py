"""管理后台的账号、密码与不透明会话认证。"""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .config import settings
from .models import Account, AuthSession, User
from .security import verify_admin_token, verify_password


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
    db.add(
        AuthSession(
            account_id=account_id,
            user_id=user_id,
            token_hash=_token_hash(token),
            role=role,
            expires_at=datetime.now(UTC) + timedelta(days=settings.auth_session_days),
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