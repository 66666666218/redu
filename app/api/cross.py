"""跨平台同类资源号路由(2026-10-02)。

**为什么补这个文件**:`cross_accounts.list_cross_accounts` 此前**没有任何调用方**——
定时任务(`cross_account_tick`)把号收进 `cross_platform_accounts`,库里躺着一堆号
却**没有出口**(前端也没页面),用户根本看不到发现结果。这里补上查询 + 手动触发。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.db import get_db
from app.db.models import User
from app.services import cross_accounts

router = APIRouter()


@router.get("/api/cross/accounts")
def cross_account_list(platform: str = "", user: User = Depends(get_current_user),
                       db: Session = Depends(get_db)):
    """跨平台对标号列表(新→旧);`platform` 空=全部(zhihu/bilibili)。"""
    items = cross_accounts.list_cross_accounts(db, user.id, platform=platform)
    return {"count": len(items), "items": items}


@router.post("/api/cross/discover")
def cross_discover(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """手动跑一轮跨平台发现。

    ⚠️ **会真实访问外部平台**:知乎按资源词搜、B站按行业词搜用户;B站搜索接口对
    频率敏感(连发即 `-352`),所以一轮里每个请求之间都有 4 秒间隔、整轮约 6 个请求。
    **别连点**。
    """
    return cross_accounts.discover_cross_accounts(db, user.id)
