"""渠道实例实时状态对账与持久告警。"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import ChannelInstance
from .redaction import redact_error
from .tasks import add_task

_PUBLIC_KEYS = {"status", "qrcode_url", "account_id", "error"}
_ERROR_STATUSES = {"error", "offline", "reconnecting"}


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _safe_state(raw: dict[str, Any]) -> dict[str, Any]:
    state = {key: value for key, value in raw.items() if key in _PUBLIC_KEYS}
    status = str(state.get("status") or "offline")
    if status not in {"pending_login", "online", "offline", "error", "reconnecting"}:
        status = "error"
    state["status"] = status
    if "error" in state:
        state["error"] = type(state["error"]).__name__ if isinstance(state["error"], Exception) else str(
            state["error"]
        ).split()[0][:120]
    return state


def _public_status(status: str) -> str:
    return "logging_in" if status == "pending_login" else status


def _add_transition_alert(
    db: Session,
    instance: ChannelInstance,
    *,
    old_status: str,
    new_status: str,
) -> None:
    kind = "channel_recovered" if new_status == "online" else "channel_offline"
    add_task(
        db,
        "alert",
        f"channel-status:{instance.id}:{instance.status_revision}",
        {
            "kind": kind,
            "instance_id": instance.id,
            "instance_name": instance.instance_name,
            "channel": instance.channel,
            "old_status": old_status,
            "new_status": new_status,
            "error": instance.last_error,
        },
    )


def apply_channel_status(
    db: Session,
    instance: ChannelInstance,
    raw_state: dict[str, Any],
    *,
    now: datetime | None = None,
) -> bool:
    """把 Bridge 公开状态写入数据库；只有状态转换才生成一次告警。"""
    now = now or _utcnow()
    # 串行化回调、定时对账和启停操作，避免相同 revision 告警或旧状态覆盖新意图。
    locked = db.scalar(
        select(ChannelInstance).where(ChannelInstance.id == instance.id).with_for_update()
    )
    if locked is not None:
        instance = locked
    state = _safe_state(raw_state)
    old_status = instance.status
    new_status = _public_status(str(state["status"]))
    changed = old_status != new_status

    instance.last_checked_at = now
    instance.login_state = state
    instance.status = new_status
    if changed:
        instance.status_revision = int(instance.status_revision or 0) + 1
        instance.status_updated_at = now
    if new_status == "online":
        instance.last_online_at = now
        instance.last_error = None
    elif new_status in _ERROR_STATUSES:
        instance.last_error_at = now
        instance.last_error = str(state.get("error") or "BridgeUnavailable")[:120]
        state["error"] = instance.last_error
        instance.login_state = state

    if changed and (new_status == "online" or new_status in _ERROR_STATUSES):
        _add_transition_alert(db, instance, old_status=old_status, new_status=new_status)
    return changed


async def reconcile_channel_instance(
    db: Session,
    instance: ChannelInstance,
    *,
    adapter=None,
) -> dict[str, Any]:
    """从 Bridge 获取单实例真实状态；网络失败表现为可重试状态，不泄露异常正文。"""
    if not instance.desired_running:
        return {"status": "offline"}
    if adapter is None:
        from .channels import registry

        adapter = registry.get(instance.channel)
    try:
        state = await adapter.instance_status(instance.id)
    except Exception as exc:  # noqa: BLE001 - 对账失败需要转成公开状态
        # 只记录异常类型；异常正文可能包含 URL、token 或底层连接详情。
        _ = redact_error(exc, 120)
        state = {"status": "reconnecting", "error": "BridgeUnavailable"}
    # 网络等待期间用户可能停止实例；刷新控制意图后再应用返回状态。
    db.refresh(instance)
    if not instance.desired_running:
        return {"status": "offline"}
    apply_channel_status(db, instance, state)
    db.commit()
    return _safe_state(state)


async def reconcile_all_channel_instances(db: Session) -> int:
    rows = list(
        db.scalars(
            select(ChannelInstance).where(ChannelInstance.channel == "wechat_clawbot")
        )
    )
    for row in rows:
        await reconcile_channel_instance(db, row)
    return len(rows)
