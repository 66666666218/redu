"""多租户服务共享 helper:`_base` / `_record_run` / 闲鱼令牌回写。

独立成模块是为了让 `tenant.py`(采集编排)/`xianyu_analytics.py`/`keyword_watch.py`
各自引用,避免它们相互 import 造成循环依赖。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from config.settings import Settings, get_settings
from app.db.models import RunRecord
from app.utils import get_logger

logger = get_logger(__name__)


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


def persist_refreshed_cookie(session: Session, user_id: int, client: object) -> bool:
    """把闲鱼客户端运行中刷新的 Cookie(`_m_h5_tk` 等)回写到该用户配置。

    `XianyuClient` 每轮从存储的 Cookie 重新播种,网关运行中下发的令牌此前只活在
    内存 session,下轮又要从旧令牌起步、多吃一次令牌往返;回写后下轮直接用新令牌。
    仅当 `_m_h5_tk` 确有变化才写;导出失败/未配置一律静默跳过,不影响采集主流程。
    """
    from app.services import cookie_store

    try:
        fresh = client.cookie_header()
    except Exception:  # noqa: BLE001
        return False
    if not fresh:
        return False

    def _token(s: str) -> str:
        m = re.search(r"_m_h5_tk=([0-9a-f]{32})_", s or "")
        return m.group(1) if m else ""

    current = cookie_store.get_cookie(session, user_id, "goofish")
    if not current or _token(fresh) == _token(current):
        return False
    cookie_store.set_cookie(session, user_id, "goofish", fresh)
    logger.info("闲鱼令牌已刷新,回写用户 %s 的 Cookie", user_id)
    return True
