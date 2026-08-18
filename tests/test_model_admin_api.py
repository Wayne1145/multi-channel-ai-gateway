"""管理员平台模型供应商与模型组 API 测试。"""

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from wecom_ai_gateway.db import SessionLocal
from wecom_ai_gateway.main import app
from wecom_ai_gateway.model_routing import RoutedCompletion
from wecom_ai_gateway.models import AuditLog, ModelGroup, PlatformProvider
from wecom_ai_gateway.security import decrypt_secret

client = TestClient(app)
HEADERS = {"X-Admin-Token": "test-admin-token"}


def test_platform_provider_api_encrypts_key_and_never_returns_it(db):
    assert client.get("/api/admin/model-providers").status_code == 401
    created = client.post(
        "/api/admin/model-providers",
        headers=HEADERS,
        json={
            "name": "DeepSeek 主线路",
            "provider_key": "openai-compatible",
            "base_url": "https://api.deepseek.example/v1",
            "api_key": "secret-platform-key",
            "enabled": True,
        },
    )
    assert created.status_code == 200
    body = created.json()
    assert body["name"] == "DeepSeek 主线路"
    assert body["api_key_configured"] is True
    assert "secret-platform-key" not in created.text
    assert "api_key" not in body

    listed = client.get("/api/admin/model-providers", headers=HEADERS)
    assert listed.status_code == 200
    assert "secret-platform-key" not in listed.text
    assert "api_key_encrypted" not in listed.text

    db = SessionLocal()
    row = db.get(PlatformProvider, body["id"])
    assert row.api_key_encrypted != "secret-platform-key"
    assert decrypt_secret(row.api_key_encrypted) == "secret-platform-key"
    assert db.query(AuditLog).filter_by(action="model_provider.create").count() == 1
    db.close()


def test_provider_update_keeps_existing_key_when_api_key_is_omitted(db):
    provider_id = client.post(
        "/api/admin/model-providers",
        headers=HEADERS,
        json={
            "name": "可更新线路",
            "provider_key": "openai-compatible",
            "base_url": "https://old.example/v1",
            "api_key": "keep-this-key",
        },
    ).json()["id"]

    updated = client.put(
        f"/api/admin/model-providers/{provider_id}",
        headers=HEADERS,
        json={"base_url": "https://new.example/v1", "enabled": False},
    )
    assert updated.status_code == 200
    assert updated.json()["base_url"] == "https://new.example/v1"
    assert updated.json()["enabled"] is False

    db = SessionLocal()
    assert decrypt_secret(db.get(PlatformProvider, provider_id).api_key_encrypted) == "keep-this-key"
    db.close()


def test_model_group_routes_and_default_are_managed_atomically(db):
    first = client.post(
        "/api/admin/model-providers",
        headers=HEADERS,
        json={
            "name": "线路一",
            "provider_key": "openai-compatible",
            "base_url": "https://one.example/v1",
            "api_key": "key-one",
        },
    ).json()
    second = client.post(
        "/api/admin/model-providers",
        headers=HEADERS,
        json={
            "name": "线路二",
            "provider_key": "openai-compatible",
            "base_url": "https://two.example/v1",
            "api_key": "key-two",
        },
    ).json()

    group = client.post(
        "/api/admin/model-groups",
        headers=HEADERS,
        json={
            "name": "客服高可用组",
            "enabled": True,
            "is_default": True,
            "routes": [
                {"provider_id": second["id"], "model": "backup-model", "priority": 20},
                {"provider_id": first["id"], "model": "primary-model", "priority": 10},
            ],
        },
    )
    assert group.status_code == 200
    payload = group.json()
    assert payload["is_default"] is True
    assert [route["model"] for route in payload["routes"]] == [
        "primary-model",
        "backup-model",
    ]
    assert all("api_key" not in route for route in payload["routes"])

    other = client.post(
        "/api/admin/model-groups",
        headers=HEADERS,
        json={
            "name": "第二默认组",
            "enabled": True,
            "is_default": True,
            "routes": [
                {"provider_id": second["id"], "model": "new-default", "priority": 1}
            ],
        },
    )
    assert other.status_code == 200

    db = SessionLocal()
    defaults = db.query(ModelGroup).filter_by(is_default=True).all()
    assert len(defaults) == 1
    assert defaults[0].name == "第二默认组"
    db.close()


def test_model_group_rejects_unknown_provider_and_duplicate_target(db):
    missing = client.post(
        "/api/admin/model-groups",
        headers=HEADERS,
        json={
            "name": "错误组",
            "is_default": True,
            "routes": [{"provider_id": "missing", "model": "m", "priority": 1}],
        },
    )
    assert missing.status_code == 400

    provider = client.post(
        "/api/admin/model-providers",
        headers=HEADERS,
        json={
            "name": "重复测试线路",
            "provider_key": "openai-compatible",
            "base_url": "https://duplicate.example/v1",
            "api_key": "key",
        },
    ).json()
    duplicate = client.post(
        "/api/admin/model-groups",
        headers=HEADERS,
        json={
            "name": "重复路由组",
            "routes": [
                {"provider_id": provider["id"], "model": "same", "priority": 1},
                {"provider_id": provider["id"], "model": "same", "priority": 2},
            ],
        },
    )
    assert duplicate.status_code == 400


def test_model_group_connectivity_test_returns_selected_route_without_usage_record(db):
    provider = client.post(
        "/api/admin/model-providers",
        headers=HEADERS,
        json={
            "name": "连通性测试线路",
            "provider_key": "openai-compatible",
            "base_url": "https://connectivity.example/v1",
            "api_key": "test-key",
        },
    ).json()
    group = client.post(
        "/api/admin/model-groups",
        headers=HEADERS,
        json={
            "name": "待测试组",
            "routes": [
                {"provider_id": provider["id"], "model": "test-model", "priority": 10}
            ],
        },
    ).json()
    completion = AsyncMock(
        return_value=RoutedCompletion(
            content="ok",
            provider_name="连通性测试线路",
            provider_key="openai-compatible",
            model="test-model",
            group_id=group["id"],
            route_id=group["routes"][0]["id"],
        )
    )

    with patch("wecom_ai_gateway.main.complete_with_routing", completion):
        response = client.post(
            f"/api/admin/model-groups/{group['id']}/test", headers=HEADERS
        )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["provider_name"] == "连通性测试线路"
    assert response.json()["model"] == "test-model"
    assert response.json()["latency_ms"] >= 0
    assert completion.await_args.kwargs["group_id"] == group["id"]

    from wecom_ai_gateway.models import UsageRecord

    db = SessionLocal()
    assert db.query(UsageRecord).count() == 0
    assert db.query(AuditLog).filter_by(action="model_group.test").count() == 1
    db.close()


def test_admin_assigns_and_clears_user_model_group(db):
    from wecom_ai_gateway.models import User, UserSettings

    provider = client.post(
        "/api/admin/model-providers",
        headers=HEADERS,
        json={
            "name": "用户组线路",
            "provider_key": "openai-compatible",
            "base_url": "https://user-group.example/v1",
            "api_key": "key",
        },
    ).json()
    group = client.post(
        "/api/admin/model-groups",
        headers=HEADERS,
        json={
            "name": "用户专属组",
            "routes": [{"provider_id": provider["id"], "model": "m", "priority": 1}],
        },
    ).json()
    db = SessionLocal()
    user = User()
    db.add(user)
    db.flush()
    db.add(UserSettings(user_id=user.id))
    db.commit()
    user_id = user.id
    db.close()

    assigned = client.put(
        f"/api/admin/users/{user_id}/model-group",
        headers=HEADERS,
        json={"model_group_id": group["id"]},
    )
    assert assigned.status_code == 200
    assert assigned.json()["model_group_id"] == group["id"]
    assert assigned.json()["effective_group_name"] == "用户专属组"

    cleared = client.put(
        f"/api/admin/users/{user_id}/model-group",
        headers=HEADERS,
        json={"model_group_id": None},
    )
    assert cleared.status_code == 200
    assert cleared.json()["model_group_id"] is None
    assert client.put(
        f"/api/admin/users/{user_id}/model-group",
        headers=HEADERS,
        json={"model_group_id": "missing"},
    ).status_code == 400
