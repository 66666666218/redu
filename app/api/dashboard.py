"""用户数据与采集频率路由:dashboard / platform-agent / xianyu / schedules / push-timeline。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.db import get_db
from app.db.models import User
from app.services import schedule_service, tenant
from app.api.deps import ScheduleIn
from app.utils import get_logger

logger = get_logger(__name__)

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


@router.get("/api/push-timeline")
def push_timeline_get(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """推送时段表:各推送类型的发送时刻(HH:MM)与生效星期(0=周日…6=周六)。

    取代原先写死在 settings 里的 7 条推送 cron(见 app/services/push_timeline.py)。
    """
    from app.services import push_timeline

    try:
        cfg = push_timeline.load(db)
    except Exception as exc:  # noqa: BLE001 - 读配置失败不该 500,回落默认值更有用
        logger.exception("读取推送时段失败")
        raise HTTPException(500, f"读取推送时段失败:{exc}") from exc
    return {"kinds": [{"key": k, **v} for k, v in cfg["kinds"].items()]}


@router.put("/api/push-timeline")
def push_timeline_put(body: dict, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """保存推送时段(整表覆盖)。

    body: `{"kinds":[{"key":"daily","times":["08:00"],"days":[1,2,3,4,5],"enabled":true}, ...]}`
    非法时刻/星期会被丢弃而非整表报废;`times` 清空等价于关掉该类推送。
    """
    from app.services import push_timeline

    items = (body or {}).get("kinds")
    if not isinstance(items, list):
        raise HTTPException(400, "kinds 需为数组")
    payload = {}
    for it in items:
        if isinstance(it, dict) and it.get("key"):
            payload[str(it["key"])] = {"times": it.get("times"), "days": it.get("days"),
                                       "enabled": it.get("enabled", True)}
    try:
        cfg = push_timeline.save(db, {"kinds": payload})
    except Exception as exc:  # noqa: BLE001 - 落库失败给出可读原因而不是裸 500
        logger.exception("保存推送时段失败")
        raise HTTPException(500, f"保存推送时段失败:{exc}") from exc
    return {"kinds": [{"key": k, **v} for k, v in cfg["kinds"].items()]}
