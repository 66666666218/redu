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
