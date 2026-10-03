"""热点事件路由:/api/events(Hotspot → Event 归并结果,跨平台共振/生命周期视图)。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.db import get_db
from app.db.models import User
from app.services import events

router = APIRouter()


@router.get("/api/events")
def events_list(status: str = "active", limit: int = Query(50, ge=1, le=200),
                user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return events.list_events(db, user.id, limit=limit, status=status)


@router.post("/api/events/assign")
def events_assign(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """手动触发一轮归属(调度每 15 分钟自动跑;此接口用于即时刷新)。"""
    return events.assign_tick(db, user.id)


@router.get("/api/source-health")
def source_health(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """数据源健康:每采集源 HEALTHY/DEGRADED/CIRCUIT_OPEN 三态+问题明细。"""
    from app.services.health import source_health as _health

    return _health(db, user.id)


@router.get("/api/source-health/peer")
def source_health_peer(user: User = Depends(get_current_user)):
    """**对端实例**探活(2026-10-03):另一侧部署(远程 hotspot)是否在线。

    本机看不到远端的 runs(两边库独立),微博/抖音/百度热榜归远端跑 —— 这个探活把
    "**远端整机失联**"与"**那些源本身没数据**"分开。用的是对端已有的公开 `/healthz`。
    """
    from app.services.health import peer_status

    return peer_status()


@router.get("/api/trending")
def trending_normalized(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """统一标准化快照视图(Normalization 出口):跨平台同构字段,新数据源无需改库。"""
    import datetime as _dt

    from sqlalchemy import select

    from app.db.models import BaiduHotItem, DouhotWord, WeiboHotItem, XianyuItem

    since = _dt.datetime.now() - _dt.timedelta(hours=6)
    out: list[dict] = []
    spec = (
        ("weibo", WeiboHotItem, "captured_at", "heat"),
        ("baidu", BaiduHotItem, "captured_at", "heat"),
        ("douhot", DouhotWord, "created_at", "score"),
    )
    for source, model, ts_col, val_col in spec:
        rows = db.scalars(select(model).where(
            model.user_id == user.id, getattr(model, ts_col) >= since
        ).order_by(getattr(model, ts_col).desc()).limit(60)).all()
        for r in rows:
            out.append({
                "source": source,
                "source_id": str(getattr(r, "item_id", "") or getattr(r, "id", "")),
                "title": str(getattr(r, "title", "")),
                "url": getattr(r, "url", None) or None,
                "rank": getattr(r, "rank", None),
                "hot_value": float(getattr(r, val_col, 0) or 0),
                "captured_at": getattr(r, ts_col).isoformat(sep=" ", timespec="seconds"),
            })
    rows = db.scalars(select(XianyuItem).where(
        XianyuItem.user_id == user.id, XianyuItem.created_at >= since
    ).order_by(XianyuItem.created_at.desc()).limit(60)).all()
    for r in rows:
        out.append({
            "source": "xianyu", "source_id": r.item_id, "title": r.title,
            "url": f"https://www.goofish.com/item?id={r.item_id}" if r.item_id else None,
            "rank": r.best_rank, "hot_value": float(r.hit_keywords or 0),
            "captured_at": r.created_at.isoformat(sep=" ", timespec="seconds"),
        })
    out.sort(key=lambda x: x["captured_at"], reverse=True)
    return {"count": len(out), "items": out[:200]}


@router.get("/api/source-health/trend")
def source_health_trend(days: int = 14, user: User = Depends(get_current_user),
                        db: Session = Depends(get_db)):
    """账号健康趋势(v2.11.0):近 N 天各采集源按天聚合的成功/失败/关键信号。

    关键信号(从 runs.detail 提取):微信读书额度耗尽(count/off)、Cookie 失效(-2012)、
    闲鱼滑块(XianyuVerify)——把"今天怎么又失败了"变成"这个月额度用了几次"。
    """
    from datetime import datetime, timedelta

    from sqlalchemy import func, select

    from app.db.models import RunRecord

    days = max(1, min(int(days or 14), 90))
    cutoff = datetime.now() - timedelta(days=days)
    rows = db.execute(
        select(func.date(RunRecord.started_at).label("d"), RunRecord.kind, RunRecord.status,
               func.count(RunRecord.id))
        .where(RunRecord.user_id == user.id, RunRecord.started_at >= cutoff,
               RunRecord.kind.notlike("hot_source%"))
        .group_by("d", RunRecord.kind, RunRecord.status)).all()
    by_day: dict[str, dict] = {}
    for d, kind, status, n in rows:
        e = by_day.setdefault(str(d), {"date": str(d), "kinds": {}})
        k = e["kinds"].setdefault(str(kind), {"success": 0, "partial": 0, "failed": 0, "skipped": 0})
        if str(status) in k:
            k[str(status)] += int(n)
    # 关键信号(按天计数)
    from sqlalchemy import or_ as _or

    sig_spec = (("wechat_quota", "wechat_listen", ("quota_skipped",)),
                ("cookie_expired", "wechat_listen", ("-2012", "WereadAuthError")),
                ("xianyu_verify", "xianyu", ("XianyuVerify",)))
    signals: dict[str, dict] = {}
    for name, kind, needles in sig_spec:
        cond = _or(*[RunRecord.detail.like(f"%{n}%") for n in needles])
        srows = db.execute(
            select(func.date(RunRecord.started_at).label("d"), func.count(RunRecord.id))
            .where(RunRecord.user_id == user.id, RunRecord.started_at >= cutoff,
                   RunRecord.kind == kind, cond)
            .group_by("d")).all()
        signals[name] = {str(d): int(n) for d, n in srows}
    out = sorted(by_day.values(), key=lambda e: e["date"])
    return {"days": days, "by_day": out, "signals": signals}
