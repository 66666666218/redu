# -*- coding: utf-8 -*-
"""抖音**对标号**监控(2026-10-29)—— 用户口径:

> 「抖音同行监控我感觉短剧比重太大了,因为**也有做小游戏的**…… 你也可以找几个**对标账号**来看,
>  其实有很多小游戏的对标账号的,**他们发了你就能看到**」

## 它解决什么(以及为什么不是"再多加几个搜索词")

`douyin_leads` 那条链是**搜索驱动**的,判据是「标题里带《口令》/夸克标记」。实测(2026-10-29):

· 搜 `小游戏 / 小游戏推广 / 抖音小游戏` 三个词 → **47 条作品,46 条判成小游戏**
  —— 关键词**能发现**这个赛道;
· 但这 47 条**没有一条带《口令》**(小游戏推广用的是 `#话题`,不是网盘口令)
  ⇒ `collect_leads` 会**全部丢掉**,卡片上一条也看不到。

而且搜索返回的是「**当前最热的**」那一批,实测大量是 1 周前 / 3 个月前 / 11 个月前的
—— 每轮都推同一批,不是"他发了新东西"。

⇒ 所以小游戏这类**没有口令形态**的赛道,正确做法是**按账号看新作品**:
  · 账号是稳定的(`cross_platform_accounts`,`platform="douyin"`,`uid` = sec_uid);
  · 每轮只比"这个 vid 见没见过" ⇒ 有"新"的语义,不发则一条都不推;
  · 卡片按**赛道**分组(与线索卡同一口径,`category_topics.niche_of`)。

## 三条纪律

1. **首次扫描只建基线、不推送** —— 一个新号的主页上通常有 20+ 条旧作品,全推一遍
   就是"上线即刷屏"。基线落库(`pushed_at` 直接填上),之后**只推新出现的 vid**。
   运行记录里会写明「建基线」,免得"第一次没推"被当成坏了。
2. **一个号失败不拖垮整轮**;但**全部失败**必须抛(验证码/未登录要显形,
   不能被记成"这些号今天没发东西")。
3. **慢活(开页面)全部做完再写库** —— 写点必须在浏览器关掉之后。本仓在
   `new-write-point-holds-lock-over-slowwork` 上栽过两次(抱着 SQLite 写锁等外部调用)。

## 号从哪来

`scripts/douyin_peer_add.py --name 抖音小游戏`:按名字做一次**用户搜索**
(`/search/<名字>?type=user`)拿到 sec_uid 再入库。也可以 `--uid` 直接给。
⚠️ 同名号很多(实测「小游戏推广」有 3 个不同 sec_uid)⇒ 脚本会打印候选,
**对不上就别猜**。
"""
from __future__ import annotations

import re
import time
import urllib.parse
from datetime import datetime

from sqlalchemy import select

from app.utils import get_logger

logger = get_logger(__name__)

#: 主页地址。`uid` = sec_uid(形如 `MS4wLjABAAAA…`),不是昵称、也不是抖音号。
_PROFILE_URL = "https://www.douyin.com/user/{uid}"
_USER_SEARCH_URL = "https://www.douyin.com/search/{q}?type=user"

_GOTO_TIMEOUT_MS = 45_000
#: 落地后等一会儿再读 DOM —— 主页的瀑布流是前端渲染的,读太早只有骨架。
_PAGE_WAIT_S = 6.0
#: 号与号之间的间隔(像人;也避免连打)。
_BETWEEN_ACCOUNTS_S = 4.0

#: 从主页抽作品。**判据写在 JS 里**(DOM 才是这条路的事实源,与 `douyin_cdp_source` 同一条纪律)。
_GRAB_PROFILE_JS = r"""() => {
    const seen = new Set();
    const rows = [];
    for (const a of document.querySelectorAll('a[href*="/video/"]')) {
        const m = (a.href || '').match(/\/video\/(\d+)/);
        if (!m || seen.has(m[1])) continue;
        seen.add(m[1]);
        const box = a.closest('li') || a.parentElement;
        rows.push({vid: m[1], text: (box ? box.innerText : '').trim()});
    }
    const body = document.body ? document.body.innerText : '';
    return {
        rows: rows,
        title: document.title || '',
        //: 与搜索页同一判据:**只看标题**。正文里出现「验证码」可能只是某条作品的标题。
        captcha: /验证码/.test(document.title || ''),
        logged_out: /登录后(?:即可|查看)/.test(body),
    };
}"""

#: 用户搜索页:拿 `a[href*="/user/"]` 里的 sec_uid。
_GRAB_USERS_JS = r"""() => {
    const seen = new Set();
    const users = [];
    for (const a of document.querySelectorAll('a[href*="/user/"]')) {
        const m = (a.href || '').match(/\/user\/([^/?#]+)/);
        if (!m || m[1] === 'self' || seen.has(m[1])) continue;
        seen.add(m[1]);
        users.push({uid: m[1], name: (a.innerText || '').trim().split('\n')[0]});
    }
    return {users: users, title: document.title || '',
            captcha: /验证码/.test(document.title || '')};
}"""

#: `1.7万` / `9217` / `2.3亿` / `12w` —— 主页卡片上的点赞数写法。
_RE_METRIC = re.compile(r"^([0-9]+(?:\.[0-9]+)?)\s*([万wW亿]?)$")
#: 置顶标记(它占一行,但不是标题也不是点赞数)。
_PINNED = frozenset({"置顶"})

_UNIT = {"万": 10_000, "w": 10_000, "W": 10_000, "亿": 100_000_000}


def parse_metric(text: str) -> int:
    """`1.7万` → 17000;认不出返回 0(**不编**)。"""
    m = _RE_METRIC.match((text or "").strip())
    if not m:
        return 0
    #: ⚠️ `2.3 * 1e8` 在浮点里是 229999999.99…,直接 `int()` 会少 1 —— 先 round。
    return int(round(float(m.group(1)) * _UNIT.get(m.group(2), 1)))


def parse_work(text: str) -> dict:
    """主页卡片文字 → `{title, likes, pinned}`。

    实测形状(2026-10-29,一手抓下来的,别照猜):
        `13 / ##疯狂小鱼塘 …`            ⇒ 点赞 13、标题是最后一行
        `置顶 / 1.7万 / 亿万玩家用创意…`  ⇒ 点赞 1.7 万、标题是最后一行
    ⚠️ 标题取**最后一个非空行**,不是第一行 —— 第一行是点赞数/置顶标记。
    """
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return {"title": "", "likes": 0, "pinned": False}
    pinned = lines[0] in _PINNED
    likes = 0
    for ln in lines[:2]:
        likes = parse_metric(ln)
        if likes:
            break
    return {"title": lines[-1][:255], "likes": likes, "pinned": pinned}


def _rows_to_works(rows: list[dict], account: dict, limit: int) -> list[dict]:
    """页面行 → 作品记录(带赛道)。**纯函数**,便于单测。"""
    from app.services.category_topics import niche_of

    out: list[dict] = []
    seen: set[str] = set()
    for r in rows or []:
        vid = str(r.get("vid") or "").strip()
        if not vid or vid in seen:
            continue
        seen.add(vid)
        parsed = parse_work(str(r.get("text") or ""))
        if not parsed["title"]:
            continue
        out.append({
            "aweme_id": vid,
            "url": f"https://www.douyin.com/video/{vid}",
            "title": parsed["title"],
            "likes": parsed["likes"],
            "niche": niche_of(parsed["title"]),
            "account_uid": account["uid"],
            "account_name": account.get("name") or "",
            #: 台账(`push_ledger`)认的键叫 `author` —— 在这里就映射好,
            #: 别指望下游认识 `account_name`(日结的「作者」列会整列空)。
            "author": account.get("name") or "",
            #: 日结/周结按 `liked_count` 显示热度(👍);不映射的话那一列恒「—」。
            "liked_count": parsed["likes"],
        })
        if len(out) >= max(1, int(limit or 0)):
            break
    return out


# ---------------------------------------------------------------- 账号目标

def target_accounts(session, user_id: int, settings) -> list[dict]:
    """本轮要看的对标号:**库里的 active douyin 账号**,没扫过的优先。

    ⚠️ 每次只取 `douyin_peer_max_accounts` 个(开页面是慢活);按
    `last_scan_at` 升序轮转,所以号多了也会轮到,不会永远只扫前几个。
    """
    from app.db.models import CrossPlatformAccount

    limit = max(0, int(getattr(settings, "douyin_peer_max_accounts", 6) or 0))
    if limit <= 0:
        return []
    stmt = (select(CrossPlatformAccount)
            .where(CrossPlatformAccount.user_id == user_id,
                   CrossPlatformAccount.platform == "douyin",
                   CrossPlatformAccount.status == "active")
            .order_by(CrossPlatformAccount.last_scan_at.is_(None).desc(),
                      CrossPlatformAccount.last_scan_at.asc(),
                      CrossPlatformAccount.id.asc())
            .limit(limit))
    return [{"id": a.id, "uid": a.uid, "name": a.name} for a in session.scalars(stmt)]


# ---------------------------------------------------------------- 去重 / 落库

def record_new(session, user_id: int, works: list[dict], *, baseline: bool) -> list[dict]:
    """把作品落库;返回**需要推送**的那些(首次扫描返回空)。

    `baseline=True`(这个号一条记录都没有)⇒ 全部记下、`pushed_at` 直接填上、**不推**。
    `baseline=False` ⇒ 只插没见过的,`pushed_at=None` 留给推成功后打戳。
    """
    from app.db.models import DouyinPeerWork

    if not works:
        return []
    ids = [w["aweme_id"] for w in works]
    known = set(session.scalars(
        select(DouyinPeerWork.aweme_id).where(
            DouyinPeerWork.user_id == user_id,
            DouyinPeerWork.aweme_id.in_(ids))).all())
    fresh = [w for w in works if w["aweme_id"] not in known]
    now = datetime.now()
    for w in fresh:
        session.add(DouyinPeerWork(
            user_id=user_id, aweme_id=w["aweme_id"], account_uid=w["account_uid"],
            account_name=w["account_name"], title=w["title"], url=w["url"],
            niche=w["niche"], likes=int(w.get("likes") or 0),
            first_seen_at=now, pushed_at=now if baseline else None))
    session.flush()
    return [] if baseline else fresh


def pending(session, user_id: int) -> list[dict]:
    """上一轮**没推成**的作品(`pushed_at IS NULL`)—— 推送失败要能补,不许静默丢。"""
    from app.db.models import DouyinPeerWork

    rows = session.scalars(select(DouyinPeerWork).where(
        DouyinPeerWork.user_id == user_id,
        DouyinPeerWork.pushed_at.is_(None))).all()
    return [{"aweme_id": r.aweme_id, "url": r.url, "title": r.title, "likes": r.likes,
             "niche": r.niche, "account_uid": r.account_uid,
             "account_name": r.account_name, "author": r.account_name,
             "liked_count": r.likes} for r in rows]


def mark_pushed(session, user_id: int, works: list[dict]) -> None:
    from app.db.models import DouyinPeerWork

    ids = [w["aweme_id"] for w in works]
    if not ids:
        return
    now = datetime.now()
    for row in session.scalars(select(DouyinPeerWork).where(
            DouyinPeerWork.user_id == user_id,
            DouyinPeerWork.aweme_id.in_(ids))).all():
        row.pushed_at = now


# ---------------------------------------------------------------- 浏览器

def _with_page(settings, fn, *, user_id: int = 1):
    """开一次浏览器、跑 `fn(page)`,收尾只关**我们自己起的**那个。"""
    from app.services import douyin_cdp_source as cdp
    from playwright.sync_api import sync_playwright

    pre_up, _ = cdp.available(settings)          #: 之前就在跑?(决定收尾要不要关)
    ok, why = cdp.ensure_running(settings)
    if not ok:
        raise cdp.DouyinCdpError(why)
    from app.db import get_session_local

    session = get_session_local()()
    try:
        cookies = cdp._cookies_of(session, user_id)   #: 登录态**只靠注入**(与搜索路同源)
    finally:
        session.close()
    with sync_playwright() as p:
        br = p.chromium.connect_over_cdp(cdp.endpoint(settings))
        ctx = br.contexts[0] if br.contexts else br.new_context()
        ctx.add_cookies(cookies)
        page = ctx.new_page()
        try:
            return fn(page)
        finally:
            try:
                page.close()
            except Exception:  # noqa: BLE001 - 关标签失败不该影响已取到的结果
                logger.debug("关闭 CDP 标签失败", exc_info=True)
            if not pre_up:
                cdp._close_browser(cdp._port(settings))


def _guard(raw: dict, where: str) -> None:
    """验证码 / 未登录要**显形** —— 它们与"这个号没有作品"完全同形。"""
    from app.services import douyin_cdp_source as cdp

    if raw.get("captcha"):
        raise cdp.DouyinCdpError(
            f"抖音{where}返回的是**「验证码中间页」**(标题:{str(raw.get('title'))[:40]})"
            "—— 这是风控要求过验证,不是「这个号没发东西」", profile_suspect=True)
    if raw.get("logged_out"):
        raise cdp.DouyinCdpError(f"抖音{where}显示**未登录** —— 库里的 cookie 不被接受")


def fetch_accounts(accounts: list[dict], settings, *, user_id: int = 1) -> dict[str, list[dict]]:
    """依次打开每个号的主页,返回 `{uid: 作品列表}`。**慢活,不做任何写库**。"""
    limit = int(getattr(settings, "douyin_peer_max_works", 6) or 6)

    def _run(page) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        last_error = ""
        for i, acct in enumerate(accounts):
            if i:
                time.sleep(_BETWEEN_ACCOUNTS_S)
            try:
                page.goto(_PROFILE_URL.format(uid=acct["uid"]),
                          wait_until="domcontentloaded", timeout=_GOTO_TIMEOUT_MS)
                time.sleep(_PAGE_WAIT_S)
                raw = page.evaluate(_GRAB_PROFILE_JS) or {}
                _guard(raw, f"主页({acct.get('name') or acct['uid']})")
                out[acct["uid"]] = _rows_to_works(raw.get("rows") or [], acct, limit)
            except Exception as exc:  # noqa: BLE001 - 一个号失败不拖垮整轮
                last_error = str(exc)[:200]
                logger.warning("对标号 %s 抓取失败:%s", acct.get("name") or acct["uid"],
                               last_error)
        if accounts and not out:
            #: **全部失败**必须抛 —— 否则运行记录会写成"这些号今天没发东西"(假成功)。
            from app.services import douyin_cdp_source as cdp

            raise cdp.DouyinCdpError(f"{len(accounts)} 个对标号**全部抓取失败**:{last_error}")
        return out

    return _with_page(settings, _run, user_id=user_id)


def resolve_user(name: str, settings=None, *, user_id: int = 1) -> list[dict]:
    """按名字做一次**用户搜索**,返回 `[{uid, name}]`(可能多个同名号)。"""
    from app.services import douyin_cdp_source as cdp

    def _run(page) -> list[dict]:
        page.goto("https://www.douyin.com/", wait_until="domcontentloaded",
                  timeout=_GOTO_TIMEOUT_MS)
        time.sleep(4)
        page.goto(_USER_SEARCH_URL.format(q=urllib.parse.quote(name)),
                  wait_until="domcontentloaded", timeout=_GOTO_TIMEOUT_MS)
        time.sleep(_PAGE_WAIT_S)
        raw = page.evaluate(_GRAB_USERS_JS) or {}
        _guard(raw, "用户搜索")
        if not raw.get("users"):
            raise cdp.DouyinCdpError(f"用户搜索「{name}」一个号都没读到(不是「没有这个号」)")
        return [u for u in raw["users"] if u.get("uid") and u.get("name")]

    return _with_page(settings, _run, user_id=user_id)


# ---------------------------------------------------------------- 推送

def push_works(works: list[dict], settings, session=None, user_id: int = 0) -> bool:
    """把**新作品**推飞书(走抖音同行群,未配回落主群)。返回是否发出。"""
    if not works:
        return False
    from app.services import category_topics
    from app.services.feishu._cards import _col_set_row, _md_safe
    from app.services.feishu_client import FeishuClient, webhook_for

    webhook = (getattr(settings, "feishu_webhook_douyin_peer", "") or "").strip() \
        or webhook_for(settings, "douhot") \
        or getattr(settings, "feishu_webhook_admin", "")
    if not webhook:
        logger.info("对标号作品:未配飞书 webhook,跳过推送")
        return False

    groups: dict[str, list[dict]] = {}
    for w in works:
        groups.setdefault(w.get("niche") or category_topics.NICHE_OTHER, []).append(w)
    order = sorted(groups, key=lambda n: (n == category_topics.NICHE_OTHER,
                                          len(groups[n]), n))
    intro = f"对标号本轮发了 **{len(works)}** 条新作品。"
    if len(groups) > 1:
        intro += "\n**本轮赛道**:" + " · ".join(f"{n} {len(groups[n])}" for n in order)

    elements: list[dict] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": intro}},
        _col_set_row([("**账号**", 3), ("**新作品**", 6), ("**赛道**", 2), ("**点赞**", 1)],
                     grey=True)]
    for niche in order:
        if len(groups) > 1:
            elements.append({"tag": "div", "text": {"tag": "lark_md",
                "content": f"**▍{niche}** · {len(groups[niche])} 条"}})
        for w in groups[niche]:
            title = _md_safe((w.get("title") or "")[:30]) or "—"
            url = _md_safe(w.get("url") or "")
            work = f"[{title}]({url})" if url else title
            likes = int(w.get("likes") or 0)
            elements.append(_col_set_row([
                (_md_safe(w.get("account_name") or "—"), 3),
                (work, 6),
                (_md_safe(niche), 2),
                (f"↗{likes}" if likes else "—", 1)]))
    card = {"config": {"wide_screen_mode": True},
            "header": {"template": "purple",
                       "title": {"tag": "plain_text",
                                 "content": f"🎯 抖音对标号 · 新作品 {len(works)} 条"}},
            "elements": elements}
    try:
        ok = FeishuClient(webhook, getattr(settings, "feishu_secret", "")).send_card(card)
    except Exception:  # noqa: BLE001 - 推送失败不该让整轮失败(下一轮会补推)
        logger.exception("对标号作品推送失败")
        return False
    if ok and session is not None and user_id:
        from app.services import push_ledger

        push_ledger.record(session, user_id, "douyin_peer", works)
    return ok


# ---------------------------------------------------------------- 作业入口

def run_for_user(session, user_id: int, settings) -> dict:
    """单个用户一轮:**只在这里推进"已完成"的状态**(游标与打戳同源)。"""
    from app.db.models import CrossPlatformAccount

    accts = target_accounts(session, user_id, settings)
    if not accts:
        return {"accounts": 0, "works": 0, "new": 0, "pushed": False,
                "note": "库里没有对标号(用 scripts/douyin_peer_add.py 加一个)"}

    # ⚠️ 慢活先做完(开页面),写库放在后面 —— 见模块纪律 3。
    fetched = fetch_accounts(accts, settings, user_id=user_id)

    from app.db.models import DouyinPeerWork

    works_total = 0
    new_works: list[dict] = []
    baselines: list[str] = []
    for acct in accts:
        got = fetched.get(acct["uid"])
        if got is None:                      # 这个号失败了,不推进 last_scan_at
            continue
        works_total += len(got)
        #: 基线判据 = **这个号在作品表里一条都没有**(不是"本轮没扫到"),见纪律 1。
        seen_before = session.scalar(
            select(DouyinPeerWork.id).where(
                DouyinPeerWork.user_id == user_id,
                DouyinPeerWork.account_uid == acct["uid"]).limit(1)) is not None
        new_works.extend(record_new(session, user_id, got, baseline=not seen_before))
        if not seen_before and got:
            baselines.append(acct.get("name") or acct["uid"])
        row = session.get(CrossPlatformAccount, acct["id"])
        if row is not None:
            row.last_scan_at = datetime.now()
            row.video_count = len(got)       #: 主页可见作品数(与 B站那条的语义不同,见注释)

    #: 上一轮没推成的也一起补(不许静默丢)。去重键仍是 aweme_id。
    seen_ids = {x["aweme_id"] for x in new_works}
    todo = new_works + [w for w in pending(session, user_id) if w["aweme_id"] not in seen_ids]
    pushed = push_works(todo, settings, session=session, user_id=user_id) if todo else False
    if pushed:
        mark_pushed(session, user_id, todo)
    return {"accounts": len(accts), "works": works_total, "new": len(new_works),
            "pushed": pushed, "pushed_n": len(todo) if pushed else 0,
            "baselines": baselines, "pending": len(todo)}


def douyin_peer_tick(settings=None) -> int:
    """定时作业入口:每个启用用户各扫一轮对标号。返回**推送出去的新作品条数**。

    ⚠️ 注册为 role=**wechat**(它要开本机那个 CDP 浏览器,与 `douyin_leads` 同侧)。
    """
    from app.db import get_session_local
    from app.db.models import User
    from app.services.tenant_base import _record_run

    from config.settings import get_settings

    settings = settings or get_settings()
    if not bool(getattr(settings, "douyin_peer_enabled", True)):
        return 0
    db = get_session_local()()
    sent_total = 0
    try:
        for (uid,) in db.execute(select(User.id).where(User.enabled.is_(True))).all():
            try:
                res = run_for_user(db, uid, settings)
                db.commit()
                note = (f"号{res['accounts']} 作品{res['works']} 新{res['new']} "
                        f"推送{'成' if res['pushed'] else '无'}")
                if res.get("baselines"):
                    #: ⚠️ **必须写出来**:首次扫描只建基线、不推,不说就会被读成"没扫到"。
                    note += f" **建基线:{','.join(res['baselines'])}**"
                if res.get("note"):
                    note += f" · {res['note']}"
                sent_total += int(res.get("pushed_n") or 0)
                _record_run(db, uid, "douyin_peer", "success", note[:900])
                db.commit()
            except Exception as exc:  # noqa: BLE001 - 单用户失败不影响其余
                db.rollback()
                logger.exception("对标号扫描失败 user=%s", uid)
                _record_run(db, uid, "douyin_peer", "failed", str(exc)[:200])
                db.commit()
    finally:
        db.close()
    return sent_total
