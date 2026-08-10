from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_admin_user_modal_can_assign_or_reset_login_account():
    html = (ROOT / "web/index.html").read_text(encoding="utf-8")
    js = (ROOT / "web/static/app.js").read_text(encoding="utf-8")

    assert 'id="account-username"' in html
    assert 'id="account-password"' in html
    assert 'id="account-save"' in html
    assert "/api/admin/users/${userId}/account" in js
    assert 'method: "PUT"' in js


def test_qrcode_ui_uses_protected_blob_endpoint_only():
    js = (ROOT / "web/static/app.js").read_text(encoding="utf-8")

    assert "/qrcode" in js
    assert ".blob()" in js
    assert "window.open" not in js
    assert "qrcode_url" not in js


def test_settings_view_navigation_and_save_wiring():
    html = (ROOT / "web/index.html").read_text(encoding="utf-8")
    js = (ROOT / "web/static/app.js").read_text(encoding="utf-8")

    assert 'data-view="settings"' in html
    assert 'id="view-settings"' in html
    assert 'id="settings-groups"' in html
    assert 'id="settings-save"' in html
    assert 'id="login-announcement"' in html
    assert 'id="quota-box"' in html
    assert '"/api/admin/settings"' in js
    assert '"settings-save"' in js
    assert "announcement" in js
    # 媒体大小上限界面按 MB 显示，保存时必须换算回字节，否则会把 20MB 存成 20 字节
    assert 's.key === "media_max_size_bytes" ? n * 1024 * 1024 : n' in js
    # 数据保留非零时保存前二次确认
    assert 'window.confirm(' in js
    assert "message_retention_days" in js
    # 审计日志页面
    assert 'data-view="audit"' in html
    assert 'id="view-audit"' in html
    assert 'id="audit-tbody"' in html
    assert '"/api/admin/audit-logs' in js
    # 我的设置（用户自助中心）
    assert 'data-view="me-settings"' in html
    assert 'id="view-me-settings"' in html
    assert 'id="me-card-tbody"' in html
    assert 'id="me-provider-tbody"' in html
    assert '"/api/me/cards' in js
    assert '"/api/me/password' in js
    assert '"/api/me/sessions/revoke-all' in js
    assert '"/api/me/usage' in js