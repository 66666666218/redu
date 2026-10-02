"""网盘资源发现(2026-10-02):按资源词去**公开平台**搜,把别人贴出来的盘链转成我方分享链。

**与抖音那条链的分工 —— 两种内容形态**:
  · **口令型**(抖音):推广号把《群名》写进**标题** → 得先**口令解析**成分享链才能用;
  · **直链型**(知乎):回答里**直接把夸克/百度盘链贴出来** → 拿到就能转存,**连解析都省了**。

**实测**(2026-10-02,拿资源库的 5 个资源词搜知乎):87 条结果里 **3 条带直接盘链**
(`pan.quark.cn/s/…`、`pan.baidu.com/s/…`)。命中率不高,但**成本接近零** ——
不用登录新平台、不用开浏览器,走的就是 `cross_accounts` 里那条已经在跑的知乎搜索。

链路:资源词 → 搜知乎 → 抽盘链 → **按链接平台分发转存**(夸克/百度/迅雷)→ 入库 → 推飞书。

⚠️ **限速**:知乎搜索有日限且按频率风控,所以**逐词之间要间隔**(`_REQ_GAP`)。
"""
from __future__ import annotations

import re
import time

from sqlalchemy import select

from app.db.models import DiscoveredPanLink
from app.utils import get_logger

logger = get_logger(__name__)

_REQ_GAP = 4.0          # 逐词间隔(秒)——与 cross_accounts 同一套限速口径
_TAG_RE = re.compile(r"<[^>]+>")


def _clean(text: str) -> str:
    """清知乎返回里的杂质:`<em>` 高亮标签 + **结构化残留**。

    实测 snippet 常长这样:
    `最近爆火的花少2人格测试 [{'content': '这个太好玩了，<em>花少2` ——
    后面那截是接口把评论/正文结构直接 dump 出来了,展示时要在 `[{` 处截断。
    """
    t = _TAG_RE.sub("", text or "")
    t = re.split(r"\[\{|\{'|\{\"", t)[0]          # 结构化残留 → 到此为止
    return " ".join(t.split()).strip()


def _kind_of(url: str) -> str:
    """按域名判这条链属于哪个盘(决定用哪个客户端转存)。"""
    u = (url or "").lower()
    if "quark.cn" in u:
        return "quark"
    if "baidu.com" in u:
        return "baidu"
    if "xunlei.com" in u:
        return "xunlei"
    return ""


def transfer_pan_url(session, user_id: int, pan_url: str, settings=None,
                     snippet: str = "") -> dict:
    """**按链分发转存**:夸克走 `QuarkTransfer`、百度走 `BaiduPanClient`、迅雷走 `xunlei_transfer`。

    返回 `{"status", "our_url", "code", "message"}`;任何失败都返回结构化结果,不抛。

    ⚠️ **百度链要提取码**:知乎回答里常把码写在附近,所以把整段文字传进来一起找。
    """
    from app.services.cookie_store import get_cookie

    kind = _kind_of(pan_url)
    if not kind:
        return {"status": "skipped", "message": f"不认识的盘链:{pan_url[:60]}",
                "our_url": "", "code": ""}
    try:
        if kind == "quark":
            from app.services.quark_transfer import QuarkTransfer

            ck = (get_cookie(session, user_id, "quark")     # 按用户配的
                  or getattr(settings, "quark_cookie", "") or "").strip()   # .env 兜底
            if not ck:
                # ⚠️ **可重试**:配上 Cookie 就能搬 —— 标 pending 而不是终态,
                # 否则"发现过一次"就把这条资源永久钉死(2026-10-02 实测踩过)
                return {"status": "pending", "message": "未配夸克 Cookie(配上后会自动重试)",
                        "our_url": "", "code": ""}
            quark = QuarkTransfer(ck, fid_store=getattr(settings, "quark_fid_store", "") or None)
            res = quark.transfer_and_share(
                pan_url,
                save_dir=getattr(settings, "quark_save_dir", "") or "/来自发现",
                password=getattr(settings, "quark_share_password", "") or "")
            return {"status": "ok" if res.get("share_url") else "failed",
                    "our_url": str(res.get("share_url") or ""),
                    "code": str(res.get("password") or ""),   # ⚠️ 夸克返回的键是 password
                    "message": "" if res.get("share_url") else "转存未返回我方链"}

        if kind == "baidu":
            from app.services.baidupan_transfer import BaiduPanClient, extract_pwd

            ck = get_cookie(session, user_id, "baidupan")
            if not ck:
                return {"status": "pending", "message": "未配百度网盘 Cookie(配上后会自动重试)",
                        "our_url": "", "code": ""}
            pwd = extract_pwd(snippet or "", pan_url) or ""
            res = BaiduPanClient(ck).transfer_and_share(pan_url, password=pwd)
            return {"status": "ok" if res.get("share_url") else "failed",
                    "our_url": str(res.get("share_url") or ""),
                    "code": str(res.get("password") or ""),   # ⚠️ 百度返回的键也是 password
                    "message": "" if res.get("share_url") else "转存未返回我方链"}

        from app.services import xunlei_transfer as xt

        res = xt.transfer_and_share(pan_url)
        return {"status": "ok" if res.get("status") == "ok" else "failed",
                "our_url": str(res.get("share_url") or ""),
                "code": str(res.get("code") or ""),
                "message": str(res.get("message") or "")[:200]}
    except Exception as exc:  # noqa: BLE001 - 单链失败不该炸整轮
        logger.warning("盘链转存失败 %s:%s", pan_url[:50], exc)
        return {"status": "failed", "message": str(exc)[:200], "our_url": "", "code": ""}


def find_candidates(session, user_id: int, keywords: list[str], limit: int = 20,
                    settings=None) -> list[dict]:
    """按资源词搜知乎 → 挑出**带直接盘链**的内容。

    返回 `[{platform, origin_url, title, author, source_url}]`(按 `origin_url` 去重)。
    """
    from app.services.cookie_store import get_cookie
    from app.services.cross_accounts import _search_zhihu

    ck = (get_cookie(session, user_id, "zhihu") or "").strip()
    if not ck:
        logger.info("网盘发现跳过:未配知乎 Cookie(这条路靠它搜)")
        return []
    found: dict[str, dict] = {}
    for i, kw in enumerate(keywords):
        if i:
            time.sleep(_REQ_GAP)                    # 限速:逐词之间必须隔开
        for r in _search_zhihu(ck, kw, limit):
            url = str(r.get("pan_link") or "").strip()
            if not url or url in found:
                continue
            snippet = _clean(r.get("snippet") or "")
            found[url] = {"platform": "zhihu", "origin_url": url,
                          "title": snippet[:255] or kw[:60],
                          "author": str(r.get("name") or "")[:64],
                          "source_url": str(r.get("url") or "")[:500]}
    return list(found.values())


def sync(session, user_id: int, settings=None) -> dict:
    """找一轮 → 转存 → 入库。返回 `{"status", "found", "ok", "skipped", "failed", "items"}`。"""
    from config.settings import get_settings

    settings = settings or get_settings()
    from app.services.douyin_leads import search_keywords   # 同一套"资源词"来源

    top = int(getattr(settings, "pan_discovery_keywords", 5) or 5)
    keywords = search_keywords(session, user_id, top, settings)
    if not keywords:
        return {"status": "no_keywords", "found": 0, "ok": 0, "skipped": 0,
                "pending": 0, "failed": 0, "items": []}

    # ⚠️ **只把"已成功 / 终态不搬"的算作已知** —— 把 pending/failed 也算进去的话,
    # 那条链就被永久钉死、再也不会重试(2026-10-02 实测踩过:缺 Cookie 跳过的那条
    # 明明配上 Cookie 就能搬,却因为"发现过了"再也不动)。
    known = set(session.scalars(select(DiscoveredPanLink.origin_url).where(
        DiscoveredPanLink.user_id == user_id,
        DiscoveredPanLink.status.in_(("ok", "skipped"))).distinct()).all())
    cands = [c for c in find_candidates(session, user_id, keywords, settings=settings)
             if c["origin_url"] not in known]
    # 已存在的行(含 pending/failed)→ **更新而不是再插一条**:否则重试撞唯一键
    exist = {r.origin_url: r for r in session.scalars(select(DiscoveredPanLink).where(
        DiscoveredPanLink.user_id == user_id)).all()}
    budget = int(getattr(settings, "pan_discovery_transfer_limit", 3) or 0)
    ok = skipped = failed = pending = 0
    items: list[dict] = []
    for c in cands:
        if budget <= 0:
            status, message, our, code = "pending", "本轮转存额度用完", "", ""
        else:
            res = transfer_pan_url(session, user_id, c["origin_url"], settings, c["title"])
            status, message = res["status"], res["message"]
            our, code = res["our_url"], res["code"]
            if status == "ok":
                budget -= 1
        row = exist.get(c["origin_url"])
        if row is None:
            row = DiscoveredPanLink(user_id=user_id, origin_url=c["origin_url"][:500])
            session.add(row)
            exist[c["origin_url"]] = row
        row.platform, row.title = c["platform"], c["title"][:255]
        row.author, row.source_url = c["author"], c["source_url"]
        row.status, row.message = status, message[:200]
        row.our_url, row.pass_code = our[:500], code[:32]
        session.flush()                     # 同轮去重靠它(见 cross_accounts 的教训)
        if status == "ok":
            ok += 1
            items.append({"title": c["title"], "author": c["author"],
                          "source_url": c["source_url"], "share_url": our, "code": code})
        elif status == "skipped":
            skipped += 1
        elif status == "pending":
            pending += 1
        elif status == "failed":
            failed += 1
    session.commit()
    logger.info("网盘发现:词 %d 个 → 候选 %d 条 → 转存成功 %d", len(keywords), len(cands), ok)
    return {"status": "ok", "found": len(cands), "ok": ok, "skipped": skipped,
            "pending": pending, "failed": failed, "items": items}


def push_items(items: list[dict], settings) -> bool:
    """把新转存的资源推飞书(**知乎专属群**,未配回落主群)。版式与公众号一致:四列网格。"""
    if not items:
        return False
    from app.services.feishu_client import webhook_for

    webhook = webhook_for(settings, "zhihu")
    if not webhook:
        return False
    from app.services.feishu._cards import _col_set_row, _md_safe, strip_others
    from app.services.feishu_client import FeishuClient

    brand = (getattr(settings, "brand_name", "") or "").strip()
    elements: list[dict] = [{"tag": "div", "text": {"tag": "lark_md", "content":
        f"公开平台(知乎)新发现 **{len(items)}** 个资源,已转存成我方分享链:"}},
        _col_set_row([("**作者**", 3), ("**资源**", 5), ("**我方分享链**", 4)], grey=True)]
    for it in items:
        elements.append(_col_set_row([
            (_md_safe(it.get("author") or "—"), 3),
            (_md_safe(strip_others(it.get("title") or "")[:40]), 5),
            (f"[▶ 打开]({_md_safe(it.get('share_url') or '')})"
             + (f" 🔑{_md_safe(it.get('code') or '')}" if it.get("code") else ""), 4)]))
    card = {"config": {"wide_screen_mode": True},
            "header": {"template": "turquoise", "title": {"tag": "plain_text",
                                                          "content": f"🔎 {brand + ' · ' if brand else ''}"
                                                                     f"新发现资源 · {len(items)} 个"}},
            "elements": elements}
    try:
        return FeishuClient(webhook, getattr(settings, "feishu_secret", "")).send_card(card)
    except Exception:  # noqa: BLE001
        logger.exception("网盘发现推送失败")
        return False


def pan_discovery_tick(settings=None) -> int:
    """定时:按资源词搜公开平台 → 转存入库 → 推飞书。返回本轮成功数。"""
    from config.settings import get_settings
    from app.db import get_session_local
    from app.db.models import User

    settings = settings or get_settings()
    if not getattr(settings, "pan_discovery_enabled", True):
        return 0
    db = get_session_local()()
    total = 0
    try:
        for (uid,) in db.execute(select(User.id).where(User.enabled.is_(True))).all():
            try:
                out = sync(db, uid, settings=settings)
                total += out.get("ok", 0)
                if out.get("items"):
                    push_items(out["items"], settings)
            except Exception:  # noqa: BLE001 - 单用户失败不影响其余
                db.rollback()
                logger.exception("网盘发现失败 user=%s", uid)
    finally:
        db.close()
    return total
