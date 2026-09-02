"""运维路由:/healthz 健康检查(始终返回 200,含数据库自查)。"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter

from app.db.database import db_status

router = APIRouter()


@router.get("/healthz")
def healthz() -> dict:
    """健康检查(含数据库自查)。故意**始终返回 200**,数据库状况看 `db` 字段。"""
    return {"status": "ok", "version": "2.0.0", "time": datetime.now().isoformat(), "db": db_status()}
