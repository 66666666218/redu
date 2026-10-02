"""迅雷群组路由(2026-10-02)。

出口补齐:群分享采进来后总得能看见、能手推。`xunlei_group` 的定时作业只管采+转,
这里提供"群列表 / 分享列表 / 手动采一轮 / 手动转存一批"四个口子。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.db import get_db
from app.db.models import User
from app.services import xunlei_captcha, xunlei_group, xunlei_kouling

router = APIRouter()


class KoulingBody(BaseModel):
    """口令解析入参。"""

    kouling: str
    transfer: bool = False          # True = 解析到分享链后**直接转存入库**(会写你的盘)


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


@router.post("/api/xunlei/kouling")
def xunlei_resolve_kouling(body: KoulingBody, user: User = Depends(get_current_user),
                           db: Session = Depends(get_db)):
    """**口令解析** —— 抖音标题里《…》包的那串口令 → 真实资源入口。

    `transfer=false` 只解析(纯读,安全);`transfer=true` 解析到分享链后**直接转存入库**
    (⚠️ 会写你的迅雷盘);解析到**群**时只加群,资源交给群采集轮。
    """
    if body.transfer:
        return xunlei_kouling.ingest(db, user.id, body.kouling)
    return xunlei_kouling.resolve(body.kouling)


@router.get("/api/xunlei/captcha")
def xunlei_captcha_status(user: User = Depends(get_current_user)):
    """captcha 续期状态(上次铸造时间 / 最近错误 / 冷却秒数)。"""
    return xunlei_captcha.status()


@router.post("/api/xunlei/captcha/refresh")
def xunlei_captcha_refresh(user: User = Depends(get_current_user)):
    """手动补铸一枚 captcha(会开一次无头浏览器,约 15~30 秒)。

    正常情况下**不需要手动调** —— 转存遇到 `captcha_invalid` 会自动补铸重试;
    这个口子留给"想提前确认凭据还活着"的场合。
    """
    ok = xunlei_captcha.refresh(force=True)
    return {"ok": ok, **xunlei_captcha.status()}


@router.get("/api/xunlei/quota")
def xunlei_quota(user: User = Depends(get_current_user)):
    """盘空间用量(转存闸门就是按它判的:到 `XUNLEI_TRANSFER_MAX_USAGE_RATIO` 就整批不搬)。"""
    from app.services import xunlei_transfer as xt

    info = xt.quota_info()
    if not info:
        return {"ok": False, "message": "取不到配额(凭据或网络问题)"}
    gb = 1024 ** 3
    return {"ok": True, "ratio": round(info["ratio"], 4),
            "usage_text": f"{info['usage'] / gb / 1024:.2f}TB",
            "limit_text": f"{info['limit'] / gb / 1024:.2f}TB",
            "full": info["ratio"] >= 0.9}
