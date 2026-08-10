"""邮件告警测试：默认关闭、配置不完整不发送、完整配置发送、write_only 加密存储。"""

from wecom_ai_gateway.alert import send_alert
from wecom_ai_gateway.models import PlatformConfig
from wecom_ai_gateway.runtime_settings import get_runtime_value, update_settings


def test_alert_disabled_by_default_logs_only(db, caplog):
    import logging

    with caplog.at_level(logging.INFO):
        sent = send_alert("测试告警", "body")
    assert sent is False
    assert any("未启用" in record.message for record in caplog.records)


def test_alert_incomplete_smtp_config_skips_send(db, caplog):
    update_settings(
        db,
        {
            "alert_email_enabled": True,
            "alert_email_recipient": "ops@example.com",
            "smtp_host": "smtp.example.com",
            "smtp_user": "bot",
            # 故意不配置 smtp_password
            "smtp_port": 465,
            "smtp_from": "bot@example.com",
        },
    )
    import logging

    with caplog.at_level(logging.WARNING):
        sent = send_alert("测试告警", "body")
    assert sent is False
    assert any("配置不完整" in record.message for record in caplog.records)


def test_alert_sends_when_configured(db, monkeypatch):
    update_settings(
        db,
        {
            "alert_email_enabled": True,
            "alert_email_recipient": "ops@example.com",
            "smtp_host": "smtp.example.com",
            "smtp_user": "bot",
            "smtp_password": "secret-pass",
            "smtp_port": 465,
            "smtp_from": "bot@example.com",
        },
    )

    class FakeSMTP:
        def __init__(self, *args, **kwargs):
            self.logged_in = None

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def login(self, user, password):
            self.logged_in = (user, password)

        def send_message(self, message):
            self.sent_message = message

    import wecom_ai_gateway.alert as alert_module

    monkeypatch.setattr(alert_module.smtplib, "SMTP_SSL", FakeSMTP)
    sent = send_alert("死信告警", "task=1")
    assert sent is True


def test_write_only_secret_stored_encrypted(db):
    update_settings(db, {"smtp_password": "plain-secret"})
    row = db.get(PlatformConfig, "smtp_password")
    assert row is not None
    stored = row.value
    assert "plain-secret" not in str(stored)
    from wecom_ai_gateway.security import decrypt_secret

    assert decrypt_secret(stored) == "plain-secret"
    # 留空 PUT 不覆盖已有值
    update_settings(db, {"smtp_password": ""})
    assert get_runtime_value(db, "smtp_password") == stored


def test_write_only_secret_never_in_settings_view(db):
    update_settings(db, {"smtp_password": "plain-secret"})
    from wecom_ai_gateway.runtime_settings import settings_view

    view = {item["key"]: item for item in settings_view(db)}
    secret = view["smtp_password"]
    assert secret["value"] == {"configured": True}
    assert "plain-secret" not in str(view)
