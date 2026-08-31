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


def test_login_page_contains_fragment_based_account_activation_flow():
    html = (ROOT / "web/index.html").read_text(encoding="utf-8")
    js = (ROOT / "web/static/app.js").read_text(encoding="utf-8")

    assert 'id="confirm-password-input"' in html
    assert 'id="activation-hint"' in html
    assert 'location.hash.startsWith("#activate=")' in js
    assert 'history.replaceState(null, "", location.pathname + location.search)' in js
    assert '"/api/auth/activate"' in js
    assert "activation_token" in js
    assert "confirm_password" in js


def test_login_page_contains_fragment_based_password_reset_flow():
    html = (ROOT / "web/index.html").read_text(encoding="utf-8")
    js = (ROOT / "web/static/app.js").read_text(encoding="utf-8")

    assert "/static/app.js?v=12" in html
    assert 'location.hash.startsWith("#reset=")' in js
    assert '"/api/auth/reset-password"' in js
    assert "reset_token" in js
    assert "设置新密码" in js
    assert "&& !resetToken" in js
    assert '$("username-input").disabled = true' in js


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
    assert 'id="me-identity-tbody"' in html
    assert 'id="me-identity-bind-code"' in html
    assert 'id="me-identity-merge"' in html
    assert '"/api/me/identities"' in js
    assert '"/api/me/identities/bind-code"' in js
    assert '"/api/me/identities/merge-preview"' in js
    assert 'id="me-knowledge-tbody"' in html
    assert 'id="me-knowledge-file"' in html
    assert 'id="me-knowledge-url"' in html
    assert '"/api/me/knowledge"' in js
    assert '"/api/me/knowledge/upload"' in js
    assert '"/api/me/knowledge/url"' in js
    # 用户列表身份/账号列与改名
    assert 'identities' in js
    assert "account_username" in js
    assert "display-name-save" in html
    assert "display-name-input-admin" in html
    assert "/display-name" in js
    # 角色卡内容预览
    assert "content_preview" in js


def test_model_routing_view_and_user_assignment_wiring():
    html = (ROOT / "web/index.html").read_text(encoding="utf-8")
    js = (ROOT / "web/static/app.js").read_text(encoding="utf-8")

    assert 'data-view="model-routing"' in html
    assert 'id="view-model-routing"' in html
    assert 'id="model-provider-tbody"' in html
    assert 'id="model-group-list"' in html
    assert 'id="model-group-modal"' in html
    assert 'id="user-model-group"' in html
    assert 'id="user-model-group-save"' in html
    assert '"/api/admin/model-providers"' in js
    assert '"/api/admin/model-groups"' in js
    assert "/model-group" in js
    assert "/test" in js
    # 平台密钥只能从密码输入框写入，不得渲染任何密钥值或密文字段。
    assert 'id="model-provider-key" type="password"' in html
    assert "api_key_encrypted" not in js


def test_restricted_tool_settings_group_is_wired():
    html = (ROOT / "web/index.html").read_text(encoding="utf-8")
    js = (ROOT / "web/static/app.js").read_text(encoding="utf-8")

    assert "/static/app.js?v=12" in html
    assert 'id="mfa-code-input"' in html
    assert 'id="me-mfa-setup"' in html
    assert 'id="admin-mfa-reset"' in html
    assert 'tool: "受限工具"' in js
    assert '"/api/auth/mfa/verify"' in js
    assert '"/api/auth/mfa/setup"' in js
    assert '"/api/auth/mfa/enable"' in js
    assert '"/api/auth/mfa/disable"' in js
    assert "recovery_codes" in js
    assert '"tool"' in js


def test_mobile_portrait_layout_keeps_navigation_and_topbar_visible():
    html = (ROOT / "web/index.html").read_text(encoding="utf-8")
    css = (ROOT / "web/static/app.css").read_text(encoding="utf-8")

    assert "/static/app.css?v=5" in html
    assert "@media (max-width: 600px)" in css
    assert "position: sticky" in css
    assert "grid-template-columns: minmax(0, 1fr) auto" in css
    assert "scrollbar-width: none" in css
    assert "padding: 14px 12px 48px" in css


def test_channel_instance_ui_shows_truthful_lifecycle_metadata_and_refreshes():
    js = (ROOT / "web/static/app.js").read_text(encoding="utf-8")

    assert "CHANNEL_STATUS_LABELS" in js
    assert "last_online_at" in js
    assert "last_error" in js
    assert "last_checked_at" in js
    assert "instanceRefreshTimer" in js
    assert "15000" in js