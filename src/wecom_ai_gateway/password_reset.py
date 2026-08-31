"""可信聊天渠道签发的短时一次性后台密码重置。"""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .models import Account, AuthSession, MfaChallenge, PasswordResetToken, User
from .security import hash_password

RESET_LIFETIME = timedelta(minutes=15)


class PasswordResetError(ValueError):
    """重置凭证无效或账号不可重置。"""


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_password_reset_token(db: Session, user_id: str) -> str:
    user = db.get(User, user_id)
    account = db.scalar(
        select(Account).where(Account.user_id == user_id, Account.is_active.is_(True))
    )
    if not user or user.is_blocked or not account:
        raise PasswordResetError("当前微信身份没有可重置的后台账号。")
    db.execute(delete(PasswordResetToken).where(PasswordResetToken.user_id == user_id))
    token = secrets.token_urlsafe(32)
    db.add(
        PasswordResetToken(
            user_id=user_id,
            account_id=account.id,
            token_hash=_token_hash(token),
            expires_at=datetime.now(UTC) + RESET_LIFETIME,
        )
    )
    db.commit()
    return token


def consume_password_reset(
    db: Session,
    token: str,
    password: str,
    *,
    password_min_length: int = 10,
) -> Account:
    """校验并消费凭证；无效请求不会先执行高成本密码哈希。"""
    if not token or len(token) > 200:
        raise PasswordResetError("密码重置链接无效或已过期，请在微信中重新发送 /account reset。")
    row = db.scalar(
        select(PasswordResetToken)
        .where(PasswordResetToken.token_hash == _token_hash(token))
        .with_for_update()
    )
    if not row:
        raise PasswordResetError("密码重置链接无效或已过期，请在微信中重新发送 /account reset。")
    expires_at = row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at <= datetime.now(UTC):
        db.delete(row)
        db.commit()
        raise PasswordResetError("密码重置链接无效或已过期，请在微信中重新发送 /account reset。")
    user = db.get(User, row.user_id)
    account = db.get(Account, row.account_id)
    if (
        not user
        or user.is_blocked
        or not account
        or not account.is_active
        or account.user_id != user.id
    ):
        raise PasswordResetError("当前账号无法重置密码。")

    password_hash = hash_password(password, min_length=password_min_length)
    account.password_hash = password_hash
    db.execute(delete(AuthSession).where(AuthSession.account_id == account.id))
    db.execute(delete(MfaChallenge).where(MfaChallenge.account_id == account.id))
    db.delete(row)
    db.flush()
    return account
