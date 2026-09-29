"""热点建议路由:一键标记「已发」+ 建议回看列表(预测→下注→结算闭环的 API 面)。"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.db import get_db
from app.db.models import HotspotSuggestion, User

router = APIRouter()


class ActedIn(BaseModel):
    acted: bool = True


@router.post("/api/hotspot/suggestions/{sid}/acted")
def mark_acted(sid: int, payload: ActedIn | None = None,
               user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """一键标记某条建议「已发」(下注)。

    只有 acted 的建议 + 夸克 save_pv 结算,才构成 Agent 的"预测→结算"学习样本;
    没执行的建议不进样本(否则把"没发"误学成"发了没效果")。
    """
    row = db.scalar(select(HotspotSuggestion).where(
        HotspotSuggestion.id == sid, HotspotSuggestion.user_id == user.id))
    if row is None:
        raise HTTPException(404, "建议不存在")
    acted = bool(payload.acted) if payload else True
    row.acted = acted
    row.acted_at = datetime.now() if acted else None
    db.commit()
    return {"status": "ok", "id": row.id, "keyword": row.keyword, "acted": row.acted}


@router.get("/api/hotspot/suggestions")
def list_suggestions(limit: int = 50, acted: bool | None = None,
                     user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """建议回看列表(按时间倒序;?acted=true 只看已发,供效果结算分析)。"""
    q = select(HotspotSuggestion).where(HotspotSuggestion.user_id == user.id)
    if acted is not None:
        q = q.where(HotspotSuggestion.acted == acted)
    rows = db.scalars(q.order_by(HotspotSuggestion.created_at.desc()).limit(min(limit, 200))).all()
    return {"total": len(rows), "list": [{
        "id": r.id, "keyword": r.keyword, "kind": r.kind, "growth": r.growth,
        "platforms": r.platforms, "opportunity": r.opportunity,
        "resource_title": r.resource_title, "link": r.link, "plan": r.plan,
        "saves": r.saves, "saves_at": r.saves_at.isoformat() if r.saves_at else None,
        "acted": r.acted, "acted_at": r.acted_at.isoformat() if r.acted_at else None,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    } for r in rows]}
