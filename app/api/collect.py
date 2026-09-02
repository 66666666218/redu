"""采集触发与关键词监控路由:collect / xianyu collect-deep / douhot watch。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.db import get_db
from app.db.models import User
from app.services import tenant

router = APIRouter()


@router.post("/api/collect/{platform}")
def collect(platform: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if platform not in ("weibo", "xianyu", "douhot"):
        raise HTTPException(400, "不支持的平台")
    try:
        runner = {"weibo": tenant.run_weibo, "xianyu": tenant.run_xianyu, "douhot": tenant.run_douhot}[platform]
        result = runner(db, user.id)
        # 采集成功后触发飞书实时提醒;失败不影响采集结果返回
        try:
            from app.services.feishu import run_feishu_keyword_alerts, run_feishu_realtime

            run_feishu_realtime(platform, user.id)
            if platform == "douhot":
                run_feishu_keyword_alerts(user.id)
        except Exception:  # noqa: BLE001
            pass
        return result
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"采集失败:{exc}") from exc


@router.post("/api/xianyu/collect-deep")
def xianyu_collect_deep(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        return tenant.run_xianyu_deep(db, user.id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"采集失败:{exc}") from exc


@router.post("/api/douhot/watch")
def douhot_watch_add(payload: dict, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return tenant.add_douhot_watch(db, user.id, str(payload.get("list_type", "word")), str(payload.get("keyword", "")))


@router.get("/api/douhot/watch")
def douhot_watch_list(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return tenant.list_douhot_watch(db, user.id)


@router.get("/api/douhot/watch-analytics")
def douhot_watch_analytics(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return tenant.douhot_watch_analytics(db, user.id)
