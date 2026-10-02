"""迅雷群组路由(2026-10-02)。

出口补齐:群分享采进来后总得能看见、能手推。`xunlei_group` 的定时作业只管采+转,
这里提供"群列表 / 分享列表 / 手动采一轮 / 手动转存一批"四个口子。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.db import get_db
from app.db.models import User
from app.services import xunlei_group

router = APIRouter()


@router.get("/api/xunlei/groups")
def xunlei_groups(user: User = Depends(get_current_user)):
    """账号所在的迅雷群列表(实时拉一次,不落库)。"""
    items = xunlei_group.list_groups()
    return {"count": len(items), "items": items}


@router.get("/api/xunlei/shares")
def xunlei_share_list(status: str = "", limit: int = 200,
                      user: User = Depends(get_current_user),
                      db: Session = Depends(get_db)):
    """已采到的群分享列表(新→旧)。`status` 空=全部,可选 pending/ok/failed。"""
    items = xunlei_group.list_group_shares(db, user.id, status=status, limit=limit)
    return {"count": len(items), "items": items}


@router.post("/api/xunlei/sync")
def xunlei_sync_now(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """手动采一轮群消息(只登记 pending,**不转存**),秒级返回。"""
    return xunlei_group.sync_group_shares(db, user.id)


@router.post("/api/xunlei/transfer")
def xunlei_transfer_now(limit: int = 3, user: User = Depends(get_current_user),
                        db: Session = Depends(get_db)):
    """手动转存一批 pending 的群分享。

    ⚠️ **会真实写入你的迅雷盘**(转存 + 生成我方分享链),单条最慢约 1 分钟;
    `limit` 限死上限,别一次拉太大。
    """
    return xunlei_group.transfer_pending(db, user.id, limit=max(1, min(limit, 10)))
