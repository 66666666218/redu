"""多租户服务共享 helper:`_base` / `_record_run`。

独立成模块是为了让 `tenant.py`(采集编排)/`xianyu_analytics.py`/`keyword_watch.py`
各自引用,避免它们相互 import 造成循环依赖。
"""
from __future__ import annotations

from datetime import datetime

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
