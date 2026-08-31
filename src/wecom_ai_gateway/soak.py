"""ClawBot 长时间稳定性采样器。

作为独立一次性容器运行；只输出脱敏 JSONL，不修改业务状态或打印账号凭据。
"""

import argparse
import asyncio
import json
import time
from datetime import UTC, datetime

from sqlalchemy import select

from .channels import registry
from .clawbot import register_clawbot_adapter
from .db import SessionLocal
from .models import ChannelInstance

_HEALTHY = {"online"}


def should_monitor_instance(desired_running: bool) -> bool:
    """长稳仅评估用户明确要求运行的实例。"""
    return bool(desired_running)


def update_unhealthy_since(
    previous: dict[str, float],
    states: dict[str, str],
    *,
    now: float,
) -> dict[str, float]:
    result: dict[str, float] = {}
    for instance_id, status in states.items():
        if status not in _HEALTHY:
            result[instance_id] = previous.get(instance_id, now)
    return result


def sustained_unhealthy(
    unhealthy_since: dict[str, float],
    *,
    now: float,
    grace_seconds: float,
) -> list[str]:
    return sorted(
        instance_id
        for instance_id, started_at in unhealthy_since.items()
        if now - started_at >= grace_seconds
    )


async def collect_snapshot() -> list[dict]:
    register_clawbot_adapter()
    adapter = registry.get("wechat_clawbot")
    db = SessionLocal()
    try:
        rows = list(
            db.scalars(
                select(ChannelInstance).where(ChannelInstance.channel == "wechat_clawbot")
                .where(ChannelInstance.desired_running.is_(True))
            )
        )
        result = []
        for row in rows:
            try:
                state = await adapter.instance_status(row.id)
                status = str(state.get("status") or "offline")
                error = str(state.get("error") or "")[:120]
            except Exception:  # noqa: BLE001 - 采样器只能输出异常类型，不能输出请求详情
                status = "bridge_unavailable"
                error = "BridgeUnavailable"
            result.append(
                {
                    "instance_id": row.id,
                    "instance_name": row.instance_name,
                    "status": status,
                    "error": error,
                }
            )
        return result
    finally:
        db.close()


async def run_monitor(
    *,
    duration_seconds: float,
    interval_seconds: float,
    unhealthy_grace_seconds: float,
) -> int:
    started = time.monotonic()
    unhealthy_since: dict[str, float] = {}
    violations: set[str] = set()
    samples = 0
    while True:
        now = time.monotonic()
        snapshot = await collect_snapshot()
        states = {row["instance_id"]: row["status"] for row in snapshot}
        unhealthy_since = update_unhealthy_since(unhealthy_since, states, now=now)
        violations.update(
            sustained_unhealthy(
                unhealthy_since,
                now=now,
                grace_seconds=unhealthy_grace_seconds,
            )
        )
        samples += 1
        print(
            json.dumps(
                {
                    "timestamp": datetime.now(UTC).isoformat(),
                    "sample": samples,
                    "instances": snapshot,
                    "sustained_unhealthy_count": len(violations),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        elapsed = time.monotonic() - started
        if elapsed >= duration_seconds:
            break
        await asyncio.sleep(min(interval_seconds, max(duration_seconds - elapsed, 0)))
    print(
        json.dumps(
            {
                "summary": "completed",
                "samples": samples,
                "violating_instance_ids": sorted(violations),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 2 if violations else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="ClawBot 脱敏长稳采样")
    parser.add_argument("--duration-hours", type=float, default=72)
    parser.add_argument("--interval-seconds", type=float, default=30)
    parser.add_argument("--unhealthy-grace-seconds", type=float, default=300)
    args = parser.parse_args()
    if args.duration_hours < 0 or args.interval_seconds <= 0 or args.unhealthy_grace_seconds < 0:
        parser.error("时间参数无效")
    raise SystemExit(
        asyncio.run(
            run_monitor(
                duration_seconds=args.duration_hours * 3600,
                interval_seconds=args.interval_seconds,
                unhealthy_grace_seconds=args.unhealthy_grace_seconds,
            )
        )
    )


if __name__ == "__main__":
    main()
