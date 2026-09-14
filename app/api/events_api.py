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
