"""微信渠道用户的短时一次性后台账号激活。"""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .models import Account, AccountActivationToken, User
from .security import hash_password

ACTIVATION_LIFETIME = timedelta(minutes=15)


class ActivationError(ValueError):
    """激活凭证无效或用户不具备激活资格。"""


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_activation_token(db: Session, user_id: str) -> str:
    """签发高熵激活令牌；同用户旧令牌立即失效。"""
    db.execute(delete(AccountActivationToken).where(AccountActivationToken.user_id == user_id))
    token = secrets.token_urlsafe(32)
    db.add(
        AccountActivationToken(
            user_id=user_id,
            token_hash=_token_hash(token),
            expires_at=datetime.now(UTC) + ACTIVATION_LIFETIME,
        )
    )
    db.commit()
    return token


def activate_account(
    db: Session,
    token: str,
    username: str,
    password: str,
    *,
    password_min_length: int = 10,
) -> Account:
    """原子消费激活令牌，并为令牌所属的原微信用户创建登录账号。"""
    if not token or len(token) > 200:
        raise ActivationError("激活链接无效或已过期，请在微信中重新发送 /account。")
    row = db.scalar(
        select(AccountActivationToken)
        .where(AccountActivationToken.token_hash == _token_hash(token))
        .with_for_update()
    )
    if not row:
        raise ActivationError("激活链接无效或已过期，请在微信中重新发送 /account。")
    expires_at = row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at <= datetime.now(UTC):
        db.delete(row)
        db.commit()
        raise ActivationError("激活链接无效或已过期，请在微信中重新发送 /account。")
    user = db.get(User, row.user_id)
    if not user or user.is_blocked:
        raise ActivationError("当前用户无法激活后台账号。")
    if db.scalar(select(Account).where(Account.user_id == user.id)):
        db.delete(row)
        db.commit()
        raise ActivationError("当前用户已经有登录账号，请直接登录或联系管理员重置密码。")

    # 高成本 scrypt 只在令牌、时效和用户资格都通过后执行，避免随机请求消耗 CPU。
    password_hash = hash_password(password, min_length=password_min_length)

    account = Account(
        user_id=user.id,
        username=username,
        password_hash=password_hash,
        role="user",
        is_active=True,
    )
    db.add(account)
    db.delete(row)
    db.flush()
    return account