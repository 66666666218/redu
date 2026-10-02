"""运维路由:/healthz 健康检查(始终返回 200,含数据库自查)。"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends

from app import APP_VERSION
from app.auth import get_current_user
from app.db.database import db_status
from app.db.models import User
from config.settings import get_settings

router = APIRouter()


@router.get("/healthz")
def healthz() -> dict:
    """健康检查(含数据库自查)。故意**始终返回 200**,数据库状况看 `db` 字段。"""
    return {"status": "ok", "version": APP_VERSION, "time": datetime.now().isoformat(), "db": db_status()}


@router.get("/api/instance")
def instance_info(user: User = Depends(get_current_user)) -> dict:
    """**本实例的板块归属**(2026-10-02):前端据此标出"哪些板块不归这台机器采"。

    背景:两端部署**各有独立数据库**,本机不采微博/抖音/百度 —— 但那些页面照常能打开,
    里面是 **3 天前的旧数据**(实测 73~82 小时),不标出来会被误以为在更新。
    板块→归属 的映射与调度器护栏**同源**(`scheduler._SECTION_ROLE`),不另立一份。
    """
    from app.services.scheduler import _SECTION_ROLE, _section_allowed

    settings = get_settings()
    return {
        "scheduler_role": (getattr(settings, "scheduler_role", "all") or "all").lower(),
        "sections": {sec: {"owner": owner, "owned": _section_allowed(sec, settings)}
                     for sec, owner in _SECTION_ROLE.items()},
    }
