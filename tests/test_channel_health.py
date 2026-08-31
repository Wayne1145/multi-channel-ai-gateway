from datetime import UTC, datetime

import pytest

from wecom_ai_gateway.channel_health import apply_channel_status, reconcile_channel_instance
from wecom_ai_gateway.main import ChannelStatusIn, _channel_instance_view, receive_channel_status
from wecom_ai_gateway.models import ChannelInstance, OutboxTask
from wecom_ai_gateway.worker import execute_task


class FakeAdapter:
    def __init__(self, state: dict):
        self.state = state

    async def instance_status(self, instance_id: str) -> dict:
        return dict(self.state)


def _instance(db, status: str = "online", *, desired_running: bool = True) -> ChannelInstance:
    row = ChannelInstance(
        channel="wechat_clawbot",
        instance_name="测试微信",
        login_state={"status": status},
        status=status,
        desired_running=desired_running,
        config={},
    )
    db.add(row)
    db.flush()
    return row


def test_channel_status_transition_records_timestamps_and_durable_alert(db):
    row = _instance(db)
    now = datetime.now(UTC)

    changed = apply_channel_status(
        db,
        row,
        {"status": "error", "error": "ConnectError token=must-not-leak"},
        now=now,
    )
    db.flush()

    assert changed is True
    assert row.status == "error"
    assert row.status_updated_at == now
    assert row.last_checked_at == now
    assert row.last_error_at == now
    assert row.last_error == "ConnectError"
    assert row.status_revision == 1
    task = db.query(OutboxTask).filter_by(task_type="alert").one()
    assert task.dedupe_key == f"channel-status:{row.id}:1"
    assert "must-not-leak" not in str(task.payload)
    assert task.payload["kind"] == "channel_offline"


def test_repeated_same_status_does_not_duplicate_alert(db):
    row = _instance(db)
    apply_channel_status(db, row, {"status": "error", "error": "ConnectError"})
    apply_channel_status(db, row, {"status": "error", "error": "ConnectError"})
    db.flush()

    assert db.query(OutboxTask).filter_by(task_type="alert").count() == 1
    assert row.status_revision == 1


def test_recovery_records_online_time_and_recovery_alert(db):
    row = _instance(db, "error")
    row.status_revision = 4
    now = datetime.now(UTC)

    apply_channel_status(
        db,
        row,
        {"status": "online", "account_id": "safe-bot@im.bot"},
        now=now,
    )
    db.flush()

    assert row.status == "online"
    assert row.last_online_at == now
    assert row.last_error is None
    assert row.status_revision == 5
    task = db.query(OutboxTask).filter_by(task_type="alert").one()
    assert task.payload["kind"] == "channel_recovered"


@pytest.mark.anyio
async def test_reconcile_reads_bridge_truth_and_maps_pending_login(db):
    row = _instance(db, "error")
    adapter = FakeAdapter(
        {
            "status": "pending_login",
            "qrcode_url": "https://qr.example/fresh",
        }
    )

    state = await reconcile_channel_instance(db, row, adapter=adapter)

    assert state["status"] == "pending_login"
    assert row.status == "logging_in"
    assert row.login_state["qrcode_url"] == "https://qr.example/fresh"


@pytest.mark.anyio
async def test_reconcile_skips_explicitly_stopped_instance(db):
    row = _instance(db, "offline", desired_running=False)

    class MustNotCall:
        async def instance_status(self, instance_id: str) -> dict:
            raise AssertionError("显式停止实例不应被轮询改成重连状态")

    state = await reconcile_channel_instance(db, row, adapter=MustNotCall())

    assert state == {"status": "offline"}
    assert row.status == "offline"


@pytest.mark.anyio
async def test_reconcile_checks_desired_instance_even_when_snapshot_is_offline(db):
    """offline 是实时快照，不再兼作“用户已停止”的控制意图。"""
    row = _instance(db, "offline", desired_running=True)

    state = await reconcile_channel_instance(
        db,
        row,
        adapter=FakeAdapter({"status": "online", "account_id": "restored@im.bot"}),
    )

    assert state["status"] == "online"
    assert row.status == "online"


@pytest.mark.anyio
async def test_reconcile_does_not_apply_online_result_after_user_stops_during_request(db):
    row = _instance(db, "online", desired_running=True)

    class StopDuringRequest:
        async def instance_status(self, instance_id: str) -> dict:
            row.desired_running = False
            db.commit()
            return {"status": "online"}

    state = await reconcile_channel_instance(db, row, adapter=StopDuringRequest())

    assert state == {"status": "offline"}
    assert row.desired_running is False


@pytest.mark.anyio
async def test_reconcile_marks_bridge_transport_failure_as_reconnecting(db):
    class BrokenAdapter:
        async def instance_status(self, instance_id: str) -> dict:
            raise RuntimeError("bridge token=must-not-leak")

    row = _instance(db)

    state = await reconcile_channel_instance(db, row, adapter=BrokenAdapter())

    assert state == {"status": "reconnecting", "error": "BridgeUnavailable"}
    assert row.status == "reconnecting"
    assert row.last_error == "BridgeUnavailable"
    assert "must-not-leak" not in str(row.login_state)


@pytest.mark.anyio
async def test_alert_task_sends_sanitized_transition_email(db, monkeypatch):
    sent: list[tuple[str, str]] = []

    def fake_send(subject: str, body: str) -> bool:
        sent.append((subject, body))
        return True

    monkeypatch.setattr("wecom_ai_gateway.alert.send_alert", fake_send)
    task = OutboxTask(
        task_type="alert",
        dedupe_key="channel-status:test:1",
        payload={
            "kind": "channel_offline",
            "instance_id": "safe-id",
            "instance_name": "测试微信",
            "old_status": "online",
            "new_status": "error",
            "error": "BridgeUnavailable",
        },
    )

    await execute_task(task)

    assert len(sent) == 1
    assert "测试微信" in sent[0][0]
    assert "BridgeUnavailable" in sent[0][1]


def test_channel_instance_view_exposes_safe_lifecycle_metadata(db):
    row = _instance(db, "error")
    now = datetime.now(UTC)
    row.status_updated_at = now
    row.last_checked_at = now
    row.last_online_at = now
    row.last_error_at = now
    row.last_error = "ConnectError"

    view = _channel_instance_view(row)

    assert view["last_error"] == "ConnectError"
    assert view["last_online_at"] == now
    assert view["last_checked_at"] == now
    assert "login_state" not in view


def test_internal_status_callback_uses_transition_tracking_and_alerts(db):
    row = _instance(db, "online")

    receive_channel_status(
        row.id,
        ChannelStatusIn(status="error", error="ConnectError token=must-not-leak"),
        db,
    )

    assert row.status == "error"
    assert row.status_revision == 1
    assert row.last_error == "ConnectError"
    assert db.query(OutboxTask).filter_by(task_type="alert").count() == 1
