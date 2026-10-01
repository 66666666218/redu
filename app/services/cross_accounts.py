"""跨平台同类资源号发现(2026-10-01)。

⚠️ 注意与 `cross_platform.py`(跨**板块**趋势:rising_across/run_cross_platform_alert)
   区分——那个是"微博/抖音/百度等板块之间的共振",本模块是"跨**平台站点**找同类资源号"。

**要解决的问题**:公众号监控采到的**具体网盘资源**(如"花少2人格测试"、"XX游戏安装包"),
在别的平台上也有人发——那些账号是我们的同类对标号,但现有的候选发现只搜狗搜微信生态,
看不到它们。

**做法**(用户口径):拿资源关键词 → 各平台搜 → **结果里真含网盘链**的才收录
("确认其内容,如果确认是推广网盘的就设置成对标账号添加进去")。

**各平台门槛(2026-10-01 逐平台实测)**:
- `zhihu`    ✅ 带登录 Cookie 直接搜(`api/v4/search_v3`,**无需 x-zse 签名**)
- `bilibili` ⚠️ 分区榜能拿 UP 主(无需登录),但风控按频率、连发即 `-352`,要做必须限速
- `weibo`    ❌ 完整 Cookie + XSRF 仍 `ok:-100`(比登录态更深的校验)
- `tieba`    ❌ 带 BDUSS/STOKEN 仍 403
- `baidu`    ⚠️ 网页搜索可用(www.baidu.com/s 返回 200),但结果是内容页不是账号页
- `douyin` / `xiaohongshu` ❌ 需真机请求签名,行业共识做不到

架构:`SEARCHERS` 注册表;新增平台只加一个 `(cookie, keyword, limit) -> list[dict]`。
相关:`doc/pan-promotion-channels.md`(为什么做)、`wechat/_candidates.py`(微信生态内的同类发现)。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import CrossPlatformAccount
from app.utils import get_logger

logger = get_logger(__name__)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def _pan_of(text: str) -> str:
    """文本里的第一条网盘分享链(没有返回空串)。经门面取工具,避免子模块循环导入。"""
    from app.services.wechat_monitor import _extract_pan_urls

    urls = _extract_pan_urls("", text or "")
    return urls[0] if urls else ""


def _search_zhihu(cookie: str, keyword: str, limit: int = 20) -> list[dict]:
    """知乎搜索 → `[{uid, name, url, snippet, pan_link}]`。

    端点 `api/v4/search_v3` 实测**带登录 Cookie 即可、无需 x-zse 签名**(2026-10-01)。
    账号标识取 `author.url_token`(知乎账号的稳定 id,主页 = `/people/<token>`)。
    """
    import requests

    if not cookie or not (keyword or "").strip():
        return []
    try:
        resp = requests.get(
            "https://www.zhihu.com/api/v4/search_v3",
            params={"t": "general", "q": keyword.strip(), "limit": max(1, min(limit, 20))},
            headers={"Cookie": cookie, "User-Agent": _UA, "Referer": "https://www.zhihu.com/",
                     "Accept": "application/json, text/plain, */*"},
            timeout=20)
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001 - 搜索失败不该炸整轮发现
        logger.warning("知乎搜索失败(%s):%s", keyword, exc)
        return []
    out: list[dict] = []
    for item in payload.get("data") or []:
        obj = item.get("object") or {}
        author = obj.get("author") or {}
        uid = str(author.get("url_token") or "").strip()
        name = str(author.get("name") or "").strip()
        if not uid or not name:
            continue          # hot_timing/ring_box 这类非账号条目直接跳过
        snippet = f"{obj.get('title') or ''} {obj.get('content') or ''}".strip()
        out.append({"uid": uid, "name": name,
                    "url": f"https://www.zhihu.com/people/{uid}",
                    "snippet": snippet[:255],
                    "pan_link": _pan_of(snippet)})
    return out


SEARCHERS = {"zhihu": _search_zhihu}


def discover_cross_accounts(session: Session, user_id: int, settings=None,
                            keywords: list[str] | None = None, limit: int = 0) -> dict:
    """拿资源关键词去各平台搜,把**内容含网盘链**的账号收录为跨平台对标号。

    关键词来源:`resource_library.resonance_resources`(同链被 ≥2 个对标号同发 = 需求被
    反复验证)——用**已被验证过的资源**当搜索词,比拿热榜标题去搜精准得多。

    返回 `{"status", "keywords", "platforms", "found", "new", "items"}`;
    `found` 统计的是**带盘链**的命中数(光聊资源的不计)。
    """
    from app.services.cookie_store import get_cookies

    if keywords is None:
        top = int(getattr(settings, "cross_discover_keywords", 5) or 5)
        keywords = _keywords_from_library(session, user_id, top)
    if not keywords:
        return {"status": "no_keywords", "new": 0}
    cookies = get_cookies(session, user_id)
    platforms = [p for p in SEARCHERS if cookies.get(p)]
    if not platforms:
        return {"status": "no_cookie", "new": 0}

    found, new, items = 0, 0, []
    for kw in keywords:
        for plat in platforms:
            try:
                hits = SEARCHERS[plat](cookies[plat], kw, limit=20)
            except Exception:  # noqa: BLE001 - 单平台失败不影响其余
                logger.exception("跨平台搜索失败 %s/%s", plat, kw)
                continue
            for h in hits:
                if not h.get("pan_link"):
                    continue      # 只收"真在发网盘资源"的号——光聊资源的普通用户不要
                found += 1
                if _save(session, user_id, plat, h, kw):
                    new += 1
                    items.append({"platform": plat, "name": h["name"], "keyword": kw})
    session.commit()
    logger.info("跨平台发现:关键词 %d × 平台 %d → 命中 %d,新增 %d",
                len(keywords), len(platforms), found, new)
    return {"status": "ok", "keywords": keywords, "platforms": platforms,
            "found": found, "new": new, "items": items}


def _keywords_from_library(session: Session, user_id: int, top: int = 5) -> list[str]:
    """从资源库挑**需求被验证过**的资源名当搜索词(同链被多号同发)。"""
    try:
        from app.services.resource_library import resonance_resources

        rows = resonance_resources(session, user_id, days=30, min_accounts=2, limit=top)
    except Exception:  # noqa: BLE001
        logger.exception("取资源库关键词失败")
        return []
    kws: list[str] = []
    for r in rows:
        titles = r.get("titles") or []
        # 取标题前 12 字当搜索词:整句搜不到,核心资源名才搜得到
        if titles and str(titles[0]).strip():
            kws.append(str(titles[0]).strip()[:12])
    return kws[:top]


def _save(session: Session, user_id: int, platform: str, hit: dict, keyword: str) -> bool:
    """入库(按 user+platform+uid 去重);返回是否新增。"""
    exists = session.scalar(select(CrossPlatformAccount.id).where(
        CrossPlatformAccount.user_id == user_id,
        CrossPlatformAccount.platform == platform,
        CrossPlatformAccount.uid == hit["uid"]).limit(1))
    if exists:
        return False
    session.add(CrossPlatformAccount(
        user_id=user_id, platform=platform, uid=hit["uid"][:64], name=hit["name"][:128],
        url=str(hit.get("url") or "")[:500], hit_keyword=keyword[:128],
        snippet=str(hit.get("snippet") or "")[:255],
        pan_link=str(hit.get("pan_link") or "")[:500]))
    return True


def list_cross_accounts(session: Session, user_id: int, platform: str = "") -> list[dict]:
    """跨平台对标号列表(新→旧)。"""
    stmt = select(CrossPlatformAccount).where(CrossPlatformAccount.user_id == user_id)
    if platform:
        stmt = stmt.where(CrossPlatformAccount.platform == platform)
    rows = session.scalars(stmt.order_by(CrossPlatformAccount.id.desc()).limit(200)).all()
    return [{"id": r.id, "platform": r.platform, "uid": r.uid, "name": r.name, "url": r.url,
             "hit_keyword": r.hit_keyword, "snippet": r.snippet, "pan_link": r.pan_link,
             "status": r.status,
             "discovered_at": r.discovered_at.isoformat(sep=" ", timespec="seconds")}
            for r in rows]


def cross_account_tick(settings=None) -> int:
    """每日定时:为所有配了目标平台 Cookie 的用户跑一轮跨平台发现。返回新增数。"""
    from config.settings import get_settings
    from app.db import get_session_local
    from app.db.models import User

    settings = settings or get_settings()
    if not getattr(settings, "cross_discover_enabled", True):
        return 0
    db = get_session_local()()
    total = 0
    try:
        for (uid,) in db.execute(select(User.id).where(User.enabled.is_(True))).all():
            try:
                out = discover_cross_accounts(db, uid, settings=settings)
                total += out.get("new", 0)
            except Exception:  # noqa: BLE001 - 单用户失败不影响其余
                db.rollback()
                logger.exception("跨平台发现失败 user=%s", uid)
    finally:
        db.close()
    return total
