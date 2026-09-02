"""数据访问层(Repository)——对应 CLAUDE.md 的 Controller/Service/Repository 分层。

把散落在各 service 的 `db.scalars(select(...))` 收敛到按聚合暴露的方法,
让业务层读的是"意图"(取某用户的微博序列),而不是裸 SQL。
仅做**数据查询**,不含业务规则(判涨/去重/排序逻辑仍留在 service)。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    AlertRecord,
    DouhotWatch,
    DouhotWatchSnap,
    DouhotWord,
    RunRecord,
    User,
    WeiboHotItem,
    WeiboTrend,
    XianyuDaily,
    XianyuItem,
)


# ---- 用户 ----
def get_user(db: Session, user_id: int) -> User | None:
    return db.get(User, user_id)


def list_users(db: Session, q: str = "") -> list[User]:
    stmt = select(User).order_by(User.id)
    if q:
        stmt = stmt.where(User.username.contains(q) | User.email.contains(q))
    return db.scalars(stmt).all()


def list_enabled_users(db: Session) -> list[User]:
    return db.scalars(select(User).where(User.enabled.is_(True))).all()


# ---- 微博(热搜)----
def weibo_items(db: Session, user_id: int, limit: int | None = None, desc: bool = True) -> list[WeiboHotItem]:
    """某用户微博热搜条目(可按时间倒序取最近 limit 条)。"""
    stmt = select(WeiboHotItem).where(WeiboHotItem.user_id == user_id)
    stmt = stmt.order_by(WeiboHotItem.id.desc()) if desc else stmt.order_by(WeiboHotItem.id.asc())
    if limit:
        stmt = stmt.limit(limit)
    return db.scalars(stmt).all()


def weibo_rising(db: Session, user_id: int, limit: int = 20) -> list[WeiboTrend]:
    return db.scalars(
        select(WeiboTrend).where(WeiboTrend.user_id == user_id).order_by(WeiboTrend.id.desc()).limit(limit)
    ).all()


def weibo_heat_series(db: Session, user_id: int) -> dict[str, list[tuple[datetime, int]]]:
    """每热搜词的 (captured_at, heat) 序列,按采集时间升序。"""
    series: dict[str, list[tuple[datetime, int]]] = {}
    for r in weibo_items(db, user_id, desc=False):
        series.setdefault(r.title, []).append((r.captured_at, r.heat))
    return series


# ---- 抖音(内容词)----
def douhot_words(db: Session, user_id: int, limit: int | None = None, desc: bool = True) -> list[DouhotWord]:
    stmt = select(DouhotWord).where(DouhotWord.user_id == user_id)
    stmt = stmt.order_by(DouhotWord.id.desc()) if desc else stmt.order_by(DouhotWord.id.asc())
    if limit:
        stmt = stmt.limit(limit)
    return db.scalars(stmt).all()


def douhot_top_words(db: Session, user_id: int, limit: int = 100) -> list[DouhotWord]:
    return db.scalars(
        select(DouhotWord).where(DouhotWord.user_id == user_id).order_by(DouhotWord.score.desc()).limit(limit)
    ).all()


# ---- 闲鱼 ----
def xianyu_items(db: Session, user_id: int, limit: int = 30) -> list[XianyuItem]:
    return db.scalars(
        select(XianyuItem).where(XianyuItem.user_id == user_id)
        .order_by(XianyuItem.hit_keywords.desc(), XianyuItem.best_rank.asc()).limit(limit)
    ).all()


def xianyu_daily_by_date(db: Session, user_id: int, snap_date: str) -> list[XianyuDaily]:
    return db.scalars(
        select(XianyuDaily).where(XianyuDaily.user_id == user_id, XianyuDaily.snap_date == snap_date)
    ).all()


def get_xianyu_daily(db: Session, user_id: int, item_id: str, snap_date: str) -> XianyuDaily | None:
    return db.scalar(
        select(XianyuDaily).where(
            XianyuDaily.user_id == user_id, XianyuDaily.item_id == item_id, XianyuDaily.snap_date == snap_date
        )
    )


def xianyu_want_series(db: Session, user_id: int) -> dict[str, list[tuple[str, float]]]:
    series: dict[str, list[tuple[str, float]]] = {}
    rows = db.scalars(
        select(XianyuDaily).where(XianyuDaily.user_id == user_id).order_by(XianyuDaily.snap_date.asc())
    ).all()
    for r in rows:
        series.setdefault(r.title or r.item_id, []).append((r.snap_date, r.want_count))
    return series


# ---- 关键词监控 ----
def list_watches(db: Session, user_id: int) -> list[DouhotWatch]:
    return db.scalars(select(DouhotWatch).where(DouhotWatch.user_id == user_id)).all()


def get_watch(db: Session, user_id: int, list_type: str, keyword: str) -> DouhotWatch | None:
    return db.scalar(
        select(DouhotWatch).where(
            DouhotWatch.user_id == user_id, DouhotWatch.list_type == list_type, DouhotWatch.keyword == keyword
        )
    )


def watch_snap_series(db: Session, user_id: int, keyword: str, since: datetime | None = None) -> list[DouhotWatchSnap]:
    stmt = select(DouhotWatchSnap).where(
        DouhotWatchSnap.user_id == user_id, DouhotWatchSnap.keyword == keyword
    )
    if since:
        stmt = stmt.where(DouhotWatchSnap.captured_at >= since)
    return db.scalars(stmt.order_by(DouhotWatchSnap.id.asc())).all()


# ---- 运行/告警统计 ----
def failed_runs(db: Session, limit: int = 50) -> list[RunRecord]:
    return db.scalars(select(RunRecord).where(RunRecord.status == "failed").order_by(RunRecord.id.desc()).limit(limit)).all()


def recent_alerts(db: Session, user_id: int, limit: int = 30) -> list[AlertRecord]:
    return db.scalars(
        select(AlertRecord).where(AlertRecord.user_id == user_id).order_by(AlertRecord.id.desc()).limit(limit)
    ).all()
