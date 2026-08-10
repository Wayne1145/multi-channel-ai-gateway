"""邮件告警。

默认关闭（alert_email_enabled=False），关闭或 SMTP 配置不完整时只写日志，
不会抛出异常影响业务路径。触发点：任务进入死信。
"""

import logging
import smtplib
from email.message import EmailMessage

from .db import SessionLocal
from .runtime_settings import get_runtime_value
from .security import decrypt_secret

log = logging.getLogger(__name__)


def send_alert(subject: str, body: str) -> bool:
    db = SessionLocal()
    try:
        if not bool(get_runtime_value(db, "alert_email_enabled")):
            log.info("告警（邮件未启用，仅记录）：%s", subject)
            return False
        host = str(get_runtime_value(db, "smtp_host") or "").strip()
        recipient = str(get_runtime_value(db, "alert_email_recipient") or "").strip()
        user = str(get_runtime_value(db, "smtp_user") or "").strip()
        sender = str(get_runtime_value(db, "smtp_from") or user).strip()
        port = int(get_runtime_value(db, "smtp_port"))
        encrypted_password = str(get_runtime_value(db, "smtp_password") or "")
        try:
            password = decrypt_secret(encrypted_password)
        except Exception:  # noqa: BLE001 - 未配置或密钥轮换时按未配置处理
            password = ""
        if not (host and recipient and user and password):
            log.warning("告警未发送（SMTP 配置不完整）：%s", subject)
            return False
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = sender
        message["To"] = recipient
        message.set_content(body)
        with smtplib.SMTP_SSL(host, port, timeout=15) as server:
            server.login(user, password)
            server.send_message(message)
        log.info("告警邮件已发送：%s", subject)
        return True
    except Exception:
        log.exception("告警邮件发送失败：%s", subject)
        return False
    finally:
        db.close()
