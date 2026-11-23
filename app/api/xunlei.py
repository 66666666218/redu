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


#: `transfer_pending` 给条目塞的**内部键**(见 `xunlei_group._source_only_item`)。
#: ⚠️ 它们只服务于推送点(标记"已推过"要按 id 回写),**不是对外契约** ——
#: 接口必须剥掉,否则会被前端当成字段用、以后就改不动了。
_PRIVATE_ITEM_KEYS = ("_row_id", "_reason")


@router.post("/api/xunlei/transfer")
def xunlei_transfer_now(limit: int = 3, user: User = Depends(get_current_user),
                        db: Session = Depends(get_db)):
    """手动转存一批 pending 的群分享。

    ⚠️ **会真实写入你的迅雷盘**(转存 + 生成我方分享链),单条最慢约 1 分钟;
    `limit` 限死上限,别一次拉太大。

    ★ 盘满时**不转存、只返回源链条目**(`source_only: true`)—— 这个接口**只转存不推送**,
    所以**不会**标记"已推送"(标记落在真正的推送点,见 `xunlei_group.mark_source_pushed`)。
    """
    out = xunlei_group.transfer_pending(db, user.id, limit=max(1, min(limit, 10)))
    items = out.get("items")
    if isinstance(items, list):
        out["items"] = [{k: v for k, v in it.items() if k not in _PRIVATE_ITEM_KEYS}
                        for it in items if isinstance(it, dict)]
    return out


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
    """盘空间用量(转存闸门就是按它判的:到 `XUNLEI_TRANSFER_MAX_USAGE_RATIO` 就整批不搬)。

    ⚠️ `fresh=True` **绕过服务里的 60 秒缓存** —— 这是**人按的那个按钮**:
    他刚清完盘再点"刷新",给他缓存里的旧数会被读成"清理没生效"。
    """
    from app.services import xunlei_transfer as xt

    info = xt.quota_info(fresh=True)
    if not info:
        return {"ok": False, "message": "取不到配额(凭据或网络问题)"}
    gb = 1024 ** 3
    #: ★ 2026-11-18:`limit` 是免费档兜底值时,`ratio`(可能 164099%)**不是**"用了多少"。
    #: 前端原来据此说「已达闸门,请先清理」—— 清多少都不会恢复,是**错动作**
    #: (实测四个迅雷号的 limit 全为 10 GiB,而主号 usage 已 16.03 TiB)。
    suspect = xt.limit_looks_like_fallback(info)
    out = {"ok": True, "ratio": round(info["ratio"], 4),
           "usage_text": f"{info['usage'] / gb / 1024:.2f}TB",
           "limit_text": f"{info['limit'] / gb / 1024:.2f}TB",
           "full": info["ratio"] >= 0.9, "limit_suspect": suspect}
    if suspect:
        out["note"] = ("配额读数不可信:limit 恒为 10 GiB 兜底值,而用量远大于它 —— "
                       "疑似会员降档,清盘不会恢复;需先确认该号的实际配额")
    return out


@router.get("/api/xunlei/resources")
def xunlei_resources(q: str = "", limit: int = 50,
                     user: User = Depends(get_current_user),
                     db: Session = Depends(get_db)):
    """**我方迅雷盘里的资源清单**(口令转存 + 扫盘),可按名字筛。

    ⚠️ 这是 2026-10-03 补的出口:此前 `xunlei_resources` **写入 2 处、读取 0 处** ——
    搬进来的资源在系统里**没有任何地方能看见**,presence「名字型」也匹配不到,等于白搬。
    """
    from sqlalchemy import select

    from app.db.models import XunleiResource

    stmt = select(XunleiResource).where(XunleiResource.user_id == user.id)
    if q.strip():
        stmt = stmt.where(XunleiResource.name.contains(q.strip()))
    rows = db.scalars(stmt.order_by(XunleiResource.synced_at.desc())
                      .limit(max(1, min(int(limit), 200)))).all()
    return {"total": len(rows), "list": [{
        "name": r.name, "kind": r.kind, "size": r.size, "parent": r.parent_name or "",
        "share_url": r.share_url or "", "pass_code": r.pass_code or "",
        "synced_at": r.synced_at.isoformat(sep=" ", timespec="seconds") if r.synced_at else "",
    } for r in rows]}


@router.get("/api/xunlei/cleanup/plan")
def xunlei_cleanup_plan(days: int = 7, user: User = Depends(get_current_user),
                        db: Session = Depends(get_db)):
    """**只看不删**:列出哪些转存文件夹已"一周没人再发"(用户口径 2026-10-03)。

    每个文件夹给出 `last_seen` 与**判据**(转存时间 / 外部又有人发),便于人工核对其合理性。
    ⚠️ 目录返回空时 `error` 会说明"可能是真为空、也可能是凭据/网络失败"(迅雷接口不区分)。
    """
    from app.services import xunlei_cleanup

    return xunlei_cleanup.plan(db, user.id, days=max(1, min(int(days), 90)))


@router.post("/api/xunlei/cleanup")
def xunlei_cleanup_run(days: int = 7, dry_run: bool = True,
                       user: User = Depends(get_current_user),
                       db: Session = Depends(get_db)):
    """执行清理:**移入回收站**(可恢复,非永久删)。

    `dry_run=false` 才真动手 —— 默认预览,删盘是难逆操作,不该被一个误点的 POST 触发。
    """
    from app.services import xunlei_cleanup

    return xunlei_cleanup.run_cleanup(db, user.id, days=max(1, min(int(days), 90)),
                                      dry_run=bool(dry_run))
