"""付费群会员管理路由:/api/members(按入群时间+周期自动算到期,提醒运营者续费/踢人)。"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.db import get_db
from app.db.models import User
from app.services import members

router = APIRouter()


class MemberIn(BaseModel):
    nickname: str
    joined_at: str            # "2026-09-01" 或完整 datetime
    group_name: str = ""
    wechat_id: str = ""
    cycle_days: int = 30
    note: str = ""


@router.get("/api/members")
def members_list(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return members.list_members(db, user.id)


@router.post("/api/members")
def members_add(body: MemberIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not body.nickname.strip():
        raise HTTPException(400, "昵称不能为空")
    try:
        joined = datetime.fromisoformat(body.joined_at.strip())
    except ValueError as exc:
        raise HTTPException(400, "入群时间格式应为 YYYY-MM-DD") from exc
    # 上限 9000-01-01:MySQL DATETIME 允许到 9999,但 `joined_at + timedelta(days=cycle_days)`
    # 会 OverflowError,一行脏数据即永久毒化 GET /api/members 与每日 renewal_tick
    # (后者遍历全平台 active 成员,单成员异常会让所有租户的续费提醒作业天天中断)。
    if not (datetime(1970, 1, 1) <= joined < datetime(9000, 1, 1)):
        raise HTTPException(400, "入群时间超出合理范围")
    m = members.add_member(db, user.id, body.nickname, joined,
                           group_name=body.group_name, wechat_id=body.wechat_id,
                           cycle_days=body.cycle_days, note=body.note)
    return {"id": m.id, "saved": True}


@router.post("/api/members/{member_id}/renew")
def members_renew(member_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not members.renew(db, user.id, member_id):
        raise HTTPException(404, "成员不存在")
    return {"renewed": True}


@router.post("/api/members/{member_id}/status")
def members_status(member_id: int, body: dict, user: User = Depends(get_current_user),
                   db: Session = Depends(get_db)):
    status = str(body.get("status") or "")
    if not members.set_status(db, user.id, member_id, status):
        raise HTTPException(400, "无效状态或成员不存在")
    return {"status": status}


@router.delete("/api/members/{member_id}")
def members_delete(member_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not members.delete_member(db, user.id, member_id):
        raise HTTPException(404, "成员不存在")
    return {"deleted": True}
