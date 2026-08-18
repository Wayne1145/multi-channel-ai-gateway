"""TOTP MFA 凭据、短时登录挑战与恢复码生命周期。"""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .config import settings
from .models import Account, AuthSession, MfaChallenge, MfaCredential
from .security import decrypt_secret, encrypt_secret, verify_admin_token, verify_password
from .totp import generate_recovery_codes, recovery_code_hash, verify_totp

CHALLENGE_TTL_MINUTES = 5
CHALLENGE_MAX_ATTEMPTS = 5


@dataclass(frozen=True)
class MfaSubject:
    subject_type: str
    subject_id: str
    account_id: str | None
    user_id: str | None
    role: str
    username: str


def admin_subject() -> MfaSubject:
    # 管理员展示名可通过环境变量修改；MFA 主体必须保持稳定，不能因改名而绕过。
    return MfaSubject("admin", "primary-admin", None, None, "admin", settings.admin_username)


def account_subject(account: Account) -> MfaSubject:
    return MfaSubject("account", account.id, account.id, account.user_id, account.role, account.username)


def credential_for(
    db: Session, subject_type: str, subject_id: str, *, for_update: bool = False
) -> MfaCredential | None:
    query = select(MfaCredential).where(
            MfaCredential.subject_type == subject_type,
            MfaCredential.subject_id == subject_id,
        )
    if for_update:
        query = query.with_for_update()
    return db.scalar(query)


def enabled_credential(db: Session, subject: MfaSubject) -> MfaCredential | None:
    row = credential_for(db, subject.subject_type, subject.subject_id)
    return row if row and row.enabled else None


def verify_subject_password(db: Session, subject: MfaSubject, password: str) -> bool:
    if subject.subject_type == "admin":
        return verify_admin_token(password)
    account = db.get(Account, subject.account_id)
    return bool(account and account.is_active and verify_password(password, account.password_hash))


def create_challenge(db: Session, subject: MfaSubject) -> str:
    token = secrets.token_urlsafe(32)
    db.execute(delete(MfaChallenge).where(MfaChallenge.expires_at <= datetime.now(UTC)))
    # 同一主体每次密码验证后只保留一个活跃挑战，缩小被盗挑战的利用窗口。
    db.execute(
        delete(MfaChallenge).where(
            MfaChallenge.subject_type == subject.subject_type,
            MfaChallenge.subject_id == subject.subject_id,
        )
    )
    db.add(
        MfaChallenge(
            token_hash=hashlib.sha256(token.encode()).hexdigest(),
            subject_type=subject.subject_type,
            subject_id=subject.subject_id,
            account_id=subject.account_id,
            user_id=subject.user_id,
            role=subject.role,
            username=subject.username,
            expires_at=datetime.now(UTC) + timedelta(minutes=CHALLENGE_TTL_MINUTES),
        )
    )
    db.commit()
    return token


def _challenge(db: Session, token: str) -> MfaChallenge | None:
    if not token or len(token) > 200:
        return None
    row = db.scalar(
        select(MfaChallenge).where(
            MfaChallenge.token_hash == hashlib.sha256(token.encode()).hexdigest()
        ).with_for_update()
    )
    if not row:
        return None
    expires_at = row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at <= datetime.now(UTC) or row.attempts >= CHALLENGE_MAX_ATTEMPTS:
        db.delete(row)
        db.commit()
        return None
    return row


def consume_challenge(db: Session, token: str, code: str) -> MfaSubject | None:
    row = _challenge(db, token)
    if not row:
        return None
    credential = credential_for(db, row.subject_type, row.subject_id, for_update=True)
    if not credential or not credential.enabled:
        db.delete(row)
        db.commit()
        return None

    accepted = False
    if 6 <= len(code.strip()) <= 32:
        counter = verify_totp(
            decrypt_secret(credential.secret_encrypted),
            code.strip(),
            last_counter=credential.last_totp_counter,
        )
        if counter is not None:
            credential.last_totp_counter = counter
            accepted = True
        else:
            digest = recovery_code_hash(code)
            hashes = list(credential.recovery_code_hashes or [])
            if digest in hashes:
                hashes.remove(digest)
                credential.recovery_code_hashes = hashes
                accepted = True

    if not accepted:
        row.attempts += 1
        if row.attempts >= CHALLENGE_MAX_ATTEMPTS:
            db.delete(row)
        db.commit()
        return None

    subject = MfaSubject(
        row.subject_type,
        row.subject_id,
        row.account_id,
        row.user_id,
        row.role,
        row.username,
    )
    db.delete(row)
    db.commit()
    return subject


def save_pending_secret(db: Session, subject: MfaSubject, secret: str) -> MfaCredential:
    row = credential_for(db, subject.subject_type, subject.subject_id)
    if row and row.enabled:
        raise ValueError("多因素认证已启用")
    if not row:
        row = MfaCredential(subject_type=subject.subject_type, subject_id=subject.subject_id)
        db.add(row)
    row.secret_encrypted = encrypt_secret(secret)
    row.enabled = False
    row.recovery_code_hashes = []
    row.last_totp_counter = None
    db.commit()
    return row


def enable_credential(db: Session, subject: MfaSubject, code: str) -> list[str]:
    row = credential_for(db, subject.subject_type, subject.subject_id)
    if not row or row.enabled:
        raise ValueError("没有待确认的多因素认证设置")
    counter = verify_totp(decrypt_secret(row.secret_encrypted), code.strip())
    if counter is None:
        raise ValueError("验证码无效")
    codes = generate_recovery_codes()
    row.enabled = True
    row.last_totp_counter = counter
    row.recovery_code_hashes = [recovery_code_hash(code) for code in codes]
    db.execute(
        delete(MfaChallenge).where(
            MfaChallenge.subject_type == subject.subject_type,
            MfaChallenge.subject_id == subject.subject_id,
        )
    )
    db.commit()
    return codes


def verify_second_factor(db: Session, subject: MfaSubject, code: str) -> bool:
    row = credential_for(db, subject.subject_type, subject.subject_id, for_update=True)
    if row and not row.enabled:
        row = None
    if not row:
        return False
    counter = verify_totp(
        decrypt_secret(row.secret_encrypted),
        code.strip(),
        last_counter=row.last_totp_counter,
    )
    if counter is not None:
        row.last_totp_counter = counter
        db.commit()
        return True
    digest = recovery_code_hash(code)
    hashes = list(row.recovery_code_hashes or [])
    if digest not in hashes:
        return False
    hashes.remove(digest)
    row.recovery_code_hashes = hashes
    db.commit()
    return True


def remove_credential(db: Session, subject: MfaSubject) -> None:
    db.execute(
        delete(MfaChallenge).where(
            MfaChallenge.subject_type == subject.subject_type,
            MfaChallenge.subject_id == subject.subject_id,
        )
    )
    db.execute(
        delete(MfaCredential).where(
            MfaCredential.subject_type == subject.subject_type,
            MfaCredential.subject_id == subject.subject_id,
        )
    )
    if subject.account_id:
        db.execute(delete(AuthSession).where(AuthSession.account_id == subject.account_id))
    else:
        db.execute(delete(AuthSession).where(AuthSession.role == "admin", AuthSession.account_id.is_(None)))
    db.commit()
