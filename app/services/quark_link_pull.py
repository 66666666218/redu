"""把**远程夸克口令链**的成果同步回本地(2026-10-14)。

## 为什么需要它(用户口径:「有一个解开的就用哪个解开的链接」)
夸克口令那条链(`quark_kouling`)**需要签名服务**,所以它在**远程(hotspot 侧)**跑;
而抖音推广卡片是**本机生成的**(`douyin_leads.push_leads`)。`remote_sync` 是**单向**
(本机→远程,而且它的契约写着"只写远程、不读不改本机业务数据")⇒ 远程搬成的 `our_url`
**从来没有回到本地**。实测代价:**远程 236 条已搬成 vs 本地只看得到 64 条** ——
也就是说卡片上那批**夸克口令**永远显示"没有链",哪怕我们早把资源搬进盘了。

## 方向与边界(和 `remote_sync` 相反,所以单独立一个模块而不是改它)
- **只读远程、只写本地的 `douyin_leads.our_url` / `moved_at`**;不碰别的表、不碰别的列;
- **只补空位**:本地那条已经 `our_url` 非空就**不覆盖** —— 本机自己搬的那条更贴近本机口径
  (与阅读数"只许往高走"、`my_pan_urls` 回落同一条纪律:**不知道就不动,别拿远程盖本地**);
- 幂等:同一批数据跑几次结果一样(第二次就没有"空位"可补了)。

## 为什么不用 `remote_sync` 顺带做
那个模块的**单一方向**是它的安全边界(它自己也写着"只写远程"). 加一条反向会把这个契约
变成"看情况",而契约一旦有例外,后面每个人都得先读一遍才知道边界在哪。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from app.db.models import DouyinLead
from app.utils import get_logger

logger = get_logger(__name__)

#: 一次最多取多少条远程结果(防"远程表突然很大"时把本机拉爆;实测目前 200~300 量级)
_MAX_ROWS = 5000


def _remote_rows(settings) -> list[dict]:
    """读远程 `douyin_leads` 里**已搬成**的行(只读)。"""
    from sqlalchemy import text

    from app.services.remote_sync import _tunnel_engine

    eng = _tunnel_engine(settings)
    with eng.connect() as conn:
        rows = conn.execute(text(
            "SELECT aweme_id, our_url, moved_at FROM douyin_leads "
            "WHERE user_id = 1 AND our_url IS NOT NULL AND our_url <> '' "
            f"LIMIT {_MAX_ROWS}")).all()
    out = []
    for aid, url, moved in rows:
        aid = str(aid or "").strip()
        url = str(url or "").strip()
        if aid and url:
            out.append({"aweme_id": aid, "our_url": url, "moved_at": moved})
    return out


def pull_once(session, settings=None, remote_rows=None) -> dict:
    """把远程"已搬成"的链补进本地空位。返回 `{"remote", "updated"}`。

    `remote_rows` 是给测试用的接缝(不传就走 SSH 隧道真读远程)—— 与
    `weread_app_token.refresh(verify=...)` 同一手法:把 I/O 挡在注入点外,判据本身可单测。
    """
    if settings is None:
        from config.settings import get_settings

        settings = get_settings()
    rows = remote_rows(settings) if remote_rows else _remote_rows(settings)
    if not rows:
        return {"remote": 0, "updated": 0}

    by_aid = {r["aweme_id"]: r for r in rows}
    updated = 0
    # 按块查本地(避免 IN 列表过长):只挑**本地还没有链**的那些
    aids = list(by_aid)
    for i in range(0, len(aids), 500):
        chunk = aids[i:i + 500]
        for row in session.scalars(select(DouyinLead).where(
                DouyinLead.aweme_id.in_(chunk),
                (DouyinLead.our_url == "") | (DouyinLead.our_url.is_(None)))):
            src = by_aid.get(str(row.aweme_id))
            if not src:
                continue
            row.our_url = str(src["our_url"])[:500]
            if src.get("moved_at") and row.moved_at is None:
                row.moved_at = src["moved_at"]      # 远程的"首次搬成时刻"比本机知道得早
            updated += 1
    if updated:
        session.commit()
        logger.info("夸克链成果回传:远程 %d 条已搬成,本地补上 %d 条", len(rows), updated)
    return {"remote": len(rows), "updated": updated}


def quark_link_pull_tick(settings=None) -> int:
    """定时:把远程已搬成的链补回本地。返回补上的条数(**失败不抛**,只记日志)。"""
    from app.db import get_session_local
    from app.db.models import User
    from app.services.tenant_base import _record_run

    if settings is None:
        from config.settings import get_settings

        settings = get_settings()
    db = get_session_local()()
    total = 0
    try:
        for (uid,) in db.execute(select(User.id).where(User.enabled.is_(True))).all():
            try:
                got = pull_once(db, settings)
                total += int(got.get("updated") or 0)
                # ⚠️ **数字进运行记录**:「补上 0 条」与「这条路没接上」必须能分开看
                #    (本仓的老毛病:静默的 0 长得像"今天本来就没有")
                _record_run(db, uid, "quark_link_pull", "success",
                            f"远程已搬成{got.get('remote', 0)} 本地补上{got.get('updated', 0)}")
            except Exception as exc:  # noqa: BLE001 - 一条用户失败不影响其余
                db.rollback()
                logger.warning("夸克链成果回传失败(用户 %s):%s", uid, str(exc)[:140])
                _record_run(db, uid, "quark_link_pull", "failed", str(exc)[:160])
            db.commit()
    finally:
        db.close()
    return total


__all__ = ["pull_once", "quark_link_pull_tick"]
