"""预警规则路由:/api/alerts/*。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.db import get_db, repository
from app.db.models import User
from app.services import alert_service
from app.api.deps import AlertRuleIn

router = APIRouter()


@router.get("/api/alerts/rules")
def alerts_rules(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return alert_service.list_rules(db, user.id)


@router.post("/api/alerts/rules")
def alerts_rule_add(payload: AlertRuleIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        r = alert_service.add_rule(
            db, user.id, payload.section, payload.rule_type,
            payload.metric, payload.threshold, payload.keyword, payload.alert_time,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"id": r.id, "section": r.section, "rule_type": r.rule_type}


@router.delete("/api/alerts/rules/{rule_id}")
def alerts_rule_del(rule_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return {"deleted": alert_service.delete_rule(db, user.id, rule_id)}


@router.get("/api/alerts/list")
def alerts_list(limit: int = 30, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = repository.recent_alerts(db, user.id, limit)
    return [{"keyword": r.keyword, "reason": r.reason, "section": r.section, "time": r.triggered_at.isoformat()} for r in rows]
