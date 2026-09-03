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


@router.get("/api/platform/{platform}")
def platform_view(platform: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """某板块独立页数据:最新榜单 + 每词智能体趋势/预测。"""
    if platform not in ("weibo", "xianyu", "douhot", "baidu"):
        raise HTTPException(400, "不支持的平台")
    from app.services.keyword_watch import platform_view as _view

    return _view(db, user.id, platform)


@router.get("/api/cross/rising")
def cross_rising(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """跨平台共同上升(≥2板块)关键词 + 各板块预测。"""
    from app.services.cross_platform import rising_across

    return rising_across(db, user.id, min_platforms=2)


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
