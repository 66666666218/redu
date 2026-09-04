"""多租户服务共享 helper:`_base` / `_record_run`。

独立成模块是为了让 `tenant.py`(采集编排)/`xianyu_analytics.py`/`keyword_watch.py`
各自引用,避免它们相互 import 造成循环依赖。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from config.settings import Settings, get_settings
from app.db.models import RunRecord


def _base(settings: Settings | None) -> Settings:
    return settings or get_settings()


def _record_run(session: Session, user_id: int, kind: str, status: str, detail: str = "") -> None:
    session.add(
        RunRecord(
            user_id=user_id,
            run_id=datetime.now().strftime("%Y%m%d%H%M%S"),
            kind=kind,
            status=status,
            started_at=datetime.now(),
            detail=detail,
        )
    )


def verify_cooldown_active(session: Session, user_id: int, settings: Settings) -> bool:
    """闲鱼人机验证后冷却:最近 `xianyu_cooldown_minutes` 内触发过 `XianyuVerify` → 本轮应跳过。

    防止闲鱼 Cookie/IP 被标记后仍每轮去撞滑块(反复 `FAIL_SYS_USER_VALIDATE` 会加重风控),
    改为静默等待冷却期过后再试。
    """
    minutes = getattr(settings, "xianyu_cooldown_minutes", 0)
    if not minutes:
        return False
    cutoff = datetime.now() - timedelta(minutes=minutes)
    row = session.scalar(
        select(RunRecord)
        .where(
            RunRecord.user_id == user_id,
            RunRecord.kind.in_(["xianyu", "xianyu_deep"]),
            RunRecord.detail.contains("XianyuVerify"),
            RunRecord.started_at >= cutoff,
        )
        .order_by(RunRecord.id.desc())
    )
    return row is not None
