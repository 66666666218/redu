"""用户数据与采集频率路由:dashboard / platform-agent / xianyu / schedules。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.db import get_db
from app.db.models import User
from app.services import schedule_service, tenant
from app.api.deps import ScheduleIn

router = APIRouter()


@router.get("/api/dashboard")
def dashboard(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return tenant.dashboard(db, user.id)


@router.get("/api/platform-agent")
def platform_agent(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """多平台智能体预测:微博/闲鱼热点趋势 + 预测(与抖音同一套逻辑)。"""
    return tenant.platform_agent(db, user.id)


@router.get("/api/xianyu/daily")
def xianyu_daily(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return tenant.xianyu_daily(db, user.id)


@router.get("/api/xianyu/analytics")
def xianyu_analytics(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return tenant.xianyu_analytics(db, user.id)


@router.get("/api/schedules")
def schedules_list(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return schedule_service.list_schedules(db, user.id)


@router.put("/api/schedules/{section}")
def schedules_set(section: str, body: ScheduleIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        return schedule_service.set_schedule(db, user.id, section, body.interval_minutes, body.enabled)
    except schedule_service.ScheduleError as exc:
        raise HTTPException(400, str(exc)) from exc
