"""B站对标号的**投稿标题**采集(2026-10-05)。

**为什么只采标题、不采链**:用户口径「**b站如果没有链可以只采集标题,从资源库里搜然后完善**」。
这恰好绕开了实测的死结 —— 签名、`space` 接口都通,但**视频简介里拿不到盘链**
(实测 30 条投稿的 `desc` 里一条 `pan.quark.cn`/`pan.baidu.com` 都没有;B站的链不在这儿)。
而**标题本身就是资源名**(实测正是「野鹅敢死队 经典影片 国语配音」这类),够用了。

**落到哪**:`hot_source_items`(`source="bili-pan"`)。这是**刻意的** ——
选题 Agent 的 `_platform_hot_candidates` 本就从这张表取候选,拿到之后
`_library_evidence` 会**自动**去资源库查"这个资源我们有没有、有没有我方链"。
于是"**只采标题 → 回资源库补全**"这条链**不用改 Agent 一行代码**就通了。

⚠️ **这个作业挂在 `wechat`(本机)角色** —— 2026-10-05 实测的结论:
`space/wbi/arc/search` 的**风控按 IP 类别区别对待**(同一时刻 `search/type` 两边都 OK):

| 发起处 | 匿名 | 带登录 cookie |
|---|---|---|
| 本机家宽 | 可用 | 可用 |
| 远程机房 | **`code=-352 风控校验失败`** | **可用** |

而 cookie 是在**本机**扫码产生的,`user_cookies` **又不在 `remote_sync` 的同步清单里** ——
挂远程就得每次重登**人工把 cookie 搬到远程 `.env`**。挂本机则**零搬运**;
产物(`hot_source_items` 里 `source="bili-pan"` 的行)由 `remote_sync` **窄同步**给远程的
选题 Agent(只推这几十行,不碰远程自己那 7 万条热榜)。

⚠️ 另试过铸 `buvid3` 带上,**无效**(仍 412),别再重复这条路。
"""
from __future__ import annotations

import hashlib
import time
import urllib.parse
from datetime import datetime

from sqlalchemy import select

from app.utils import get_logger

logger = get_logger(__name__)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_SPACE_URL = "https://api.bilibili.com/x/space/wbi/arc/search"
# 一轮最多记多少条标题进热榜表。Agent 侧只取 `rank <= 10`,多记无用;
# 留 30 是为了让"最近 30 条投稿"整体可见(排障时想看全)。
_MAX_TITLES = 30
# 限流返回码:B站用 -352(频率)/-412(风控)两个都表示"被挡住",而非"没有数据"。
_RATE_LIMIT_CODES = (-352, -412, -509)


class BiliScanError(RuntimeError):
    """B站采集被挡住(限流/风控/非 JSON)。**必须抛**,不能吞成空列表。"""


def _bili_cookie(session, user_id: int, settings=None) -> str:
    """取 B站 cookie:**先 cookie_store,再 `.env` 兜底**(与 `pan_discovery` 取夸克 cookie 同口径)。

    ⚠️ **为什么要 cookie**(2026-10-05 受控实测):`space/wbi/arc/search` 的**风控比普通搜索严得多**。
    同一 IP 上 `search/type`(搜用户/搜视频)完全正常,而 space 端点:

    | 发起处 | 结果 |
    |---|---|
    | 本机家宽 | 可用(实测取到 30 条,多次) |
    | 远程机房 | `{"code":-352,"message":"风控校验失败","v_voucher":…}` |

    —— 即机房 IP 在该端点上被要求**过风控验证**,而家宽 IP 不必。裸请求带 `buvid3` **无效**。
    登录态(`SESSDATA` 等)通常能显著放宽风控,所以这里把 cookie 带上;拿不到就退回匿名
    (**不报错**,只是更可能被挡 —— 被挡会如实抛,不会假装成功)。

    `.env` 兜底(`BILI_COOKIE`)是给**远程**用的:本地 `bili_login.py` 扫一次码,
    把 cookie 同时放进远程 `.env` 即可(`user_cookies` 表**不在** `remote_sync` 的同步清单里)。
    """
    try:
        from app.services.cookie_store import get_cookie

        ck = (get_cookie(session, user_id, "bilibili") or "").strip()
        if ck:
            return ck
    except Exception:  # noqa: BLE001 - 取不到就当没配
        logger.debug("读取 B站 cookie 失败", exc_info=True)
    if settings is None:
        settings = _settings()
    return str(getattr(settings, "bili_cookie", "") or "").strip()


def _signed_get(params: dict, referer: str, cookie: str = "") -> dict:
    """带 wbi 签名的 GET(mixin 复用 `cross_accounts._bili_mixin`,匿名也能取到)。"""
    import requests

    from app.services.cross_accounts import _bili_mixin

    mixin = _bili_mixin()
    if not mixin:
        raise BiliScanError("B站 wbi mixin 取不到,签名算不出来")
    params = {**params, "wts": int(time.time())}
    clean = {k: "".join(c for c in str(v) if c not in "!'()*") for k, v in sorted(params.items())}
    query = urllib.parse.urlencode(clean)
    url = f"{_SPACE_URL}?{query}&w_rid={hashlib.md5((query + mixin).encode()).hexdigest()}"
    headers = {"User-Agent": _UA, "Referer": referer}
    # ⚠️ **必须 strip**(2026-10-05 实测踩到):cookie 里夹带行尾 `\r`/空格时,requests 直接抛
    # `Invalid leading whitespace, reserved character(s), or return character(s) in header value`
    # —— 而 `.env` 的值恰好在**行尾**,带 `\r` 是常态(Windows 编辑/CRLF)。这个坑**不是理论**:
    # 第一次把 cookie 送上远程就是这么炸的,而且报错措辞完全看不出"是 cookie 带了回车"。
    cookie = str(cookie or "").strip()
    if cookie:
        headers["Cookie"] = cookie
    resp = requests.get(url, headers=headers, timeout=20)
    try:
        return resp.json()
    except Exception as exc:  # noqa: BLE001 - 412 时返回的是 HTML 拦截页
        raise BiliScanError(
            f"B站返回非 JSON(HTTP {resp.status_code},多半是风控拦截页)") from exc


def fetch_user_titles(mid: str, *, ps: int = _MAX_TITLES, cookie: str = "") -> list[dict]:
    """取一个号的最近投稿标题 → `[{title, bvid, url, created}]`。

    ⚠️ **被限流就抛 `BiliScanError`**,绝不返回空列表 —— 空列表与"这号真没投稿"
    在调用方眼里长得一模一样(`_save` 侧就有前车之鉴)。
    """
    if not str(mid or "").strip():
        return []
    payload = _signed_get({"mid": str(mid).strip(), "ps": int(ps), "pn": 1,
                           "order": "pubdate", "platform": "web", "web_location": "1550101"},
                          referer=f"https://space.bilibili.com/{mid}", cookie=cookie)
    code = payload.get("code")
    if code in _RATE_LIMIT_CODES:
        raise BiliScanError(f"B站限流 code={code} {str(payload.get('message'))[:60]}")
    if code != 0:
        raise BiliScanError(f"B站返回 code={code} {str(payload.get('message'))[:60]}")
    vlist = (((payload.get("data") or {}).get("list") or {}).get("vlist")) or []
    out: list[dict] = []
    for v in vlist[: int(ps)]:
        title = str(v.get("title") or "").strip()
        if not title:
            continue
        bvid = str(v.get("bvid") or "").strip()
        out.append({"title": title,
                    "bvid": bvid,
                    "url": f"https://www.bilibili.com/video/{bvid}" if bvid else "",
                    "created": int(v.get("created") or 0)})
    return out


_NAV_URL = "https://api.bilibili.com/x/web-interface/nav"


def verify_login(cookie: str = "") -> dict:
    """拿 nav 接口验 cookie 还有没有效 → `{is_login, uname, mid}`。

    ⚠️ **B站失败也回 HTTP 200**,判据只能是 `data.isLogin` —— 接口一旦按"HTTP 通了"判,
    就会把"cookie 已失效"读成"一切正常"(本仓最熟的那个坑)。

    ⚠️ **为什么要主动验**:cookie 会过期(实测 `SESSDATA` 通常撑数月,但不是永久)。
    等它过期后,`space` 端点会退回**匿名风控**(远程机房 IP 直接 `-352 风控校验失败`),
    症状是"采集突然全失败" —— 而**用户需要知道的只有一句:去重新扫码**。
    所以这一步在每轮扫描前跑,失效就当场告警,别让人对着 412 猜。
    """
    import requests

    ck = str(cookie or "").strip()
    if not ck:
        return {"is_login": False, "uname": "", "mid": 0, "reason": "未配 cookie"}
    try:
        r = requests.get(_NAV_URL, headers={"User-Agent": _UA, "Cookie": ck,
                                            "Referer": "https://www.bilibili.com/"}, timeout=20)
        d = (r.json() or {}).get("data") or {}
        return {"is_login": bool(d.get("isLogin")), "uname": str(d.get("uname") or ""),
                "mid": int(d.get("mid") or 0), "reason": ""}
    except Exception as exc:  # noqa: BLE001 - 网络问题≠失效,分开报
        return {"is_login": False, "uname": "", "mid": 0,
                "reason": f"{type(exc).__name__}: {str(exc)[:80]}"}


def _alert_cookie_dead(session, user_id: int, reason: str) -> None:
    """cookie 失效 → 推管理员群(要人动手,所以 push_feishu 默认开)。

    复用 `notify_incident` 的冷却去重,不会每 2 小时刷一次屏。
    """
    try:
        from app.services.alert_service import notify_incident

        notify_incident(session, user_id, "bili_scan",
                        "🔴 B站 cookie 已失效,对标号采集停用",
                        f"`nav` 接口判定 `isLogin=False`({reason or '未登录'})。"
                        "space 端点会退回**匿名风控**(机房 IP 直接 `-352 风控校验失败`),"
                        "所以标题采集会全失败。**跑一次 `python scripts/bili_login.py` 扫码即可**;"
                        "扫完本机会自动带上(不必再手动往别处搬 cookie)。")
    except Exception:  # noqa: BLE001 - 告警失败不影响本轮结果
        logger.debug("B站 cookie 失效告警推送失败", exc_info=True)


def _save_titles(session, user_id: int, account, titles: list[dict], settings=None) -> int:
    """标题 → `hot_source_items`(`source="bili-pan"`)。返回写入条数。

    `rank` 用**投稿在最近列表里的位置**(第 1 条最新)当名次 —— Agent 只取 `rank <= 10`,
    于是"这个号最近发的 10 条"进候选池,正好是"当下在推什么"。
    `extra` 记**账号名**,排障时能认出"这条标题是哪个号发的"。
    """
    from app.db.models import HotSourceItem

    name = str(getattr(account, "name", "") or "")[:60]
    n = 0
    for i, t in enumerate(titles[:_MAX_TITLES], start=1):
        session.add(HotSourceItem(user_id=user_id, source="bili-pan", rank=i,
                                  title=str(t["title"])[:500], url=str(t.get("url") or "")[:700],
                                  extra=name[:200]))
        n += 1
    return n


def scan_accounts(session, user_id: int, settings=None, count: int | None = None) -> dict:
    """从游标处取 `count` 个 B站对标号,采它们的投稿标题入热榜表。

    返回 `{"status", "scanned", "titles", "accounts", "cursor"}`。
    ⚠️ **任意一个号被限流就整轮抛**(`BiliScanError`)—— 见模块头注。
    """
    from sqlalchemy import func

    from app.db.models import CrossPlatformAccount

    settings = settings or _settings()
    n = int(count if count is not None else
            (getattr(settings, "bili_scan_accounts_per_run", 1) or 1))
    total = int(session.scalar(select(func.count()).select_from(CrossPlatformAccount).where(
        CrossPlatformAccount.user_id == user_id,
        CrossPlatformAccount.platform == "bilibili",
        CrossPlatformAccount.status == "active")) or 0)
    if not total:
        return {"status": "no_accounts", "scanned": 0, "titles": 0, "accounts": [], "cursor": 0}

    rows = session.scalars(select(CrossPlatformAccount).where(
        CrossPlatformAccount.user_id == user_id,
        CrossPlatformAccount.platform == "bilibili",
        CrossPlatformAccount.status == "active").order_by(_scan_priority(),
                                                          CrossPlatformAccount.last_scan_at.asc(),
                                                          CrossPlatformAccount.id)).all()
    # ⚠️ **不再用"游标 % 总数"**(2026-10-05 改):那种轮转有两个毛病 ——
    # ① 中途增删号会让窗口错位、有的号被跳过;② **已知的"空壳号"(0 投稿)每轮都还会轮到一次**,
    # 白烧一次本就紧张的 space 额度(实测第 1 个号 uid 650752289 就是 0 投稿)。
    # 改成**按扫描状态排序取队首**:没扫过的优先 → 再扫最久没扫的 → 已知空壳排最后。
    picked = rows[: max(1, min(n, len(rows)))]

    written, seen_names = 0, []
    ck = _bili_cookie(session, user_id, settings)      # 登录态能显著放宽 space 端点的风控
    if not ck:
        logger.info("未配 B站 cookie,本轮按**匿名**取(更可能被风控挡);"
                    "扫码一次即可:python scripts/bili_login.py")
    for acc in picked:
        titles = fetch_user_titles(acc.uid, cookie=ck)   # 限流/风控会在这里抛,整轮中止(有意)
        written += _save_titles(session, user_id, acc, titles, settings)
        # ⚠️ **记下扫描状态** —— 这是"59 个号里有多少空壳"唯一能**量出来**的办法
        # (space 端点限流极紧,不可能为了统计专门扫一圈)。
        acc.last_scan_at = datetime.now()
        acc.video_count = len(titles)
        seen_names.append(f"{acc.name}({len(titles)})")
        time.sleep(2.0)                              # 号与号之间留间隔,别连发
    session.commit()
    logger.info("B站对标号扫描:共 %d 个号 → 本轮扫 %s,写入标题 %d 条",
                len(rows), "、".join(seen_names), written)
    return {"status": "ok", "scanned": len(picked), "titles": written,
            "accounts": seen_names, "total": len(rows)}


def _scan_priority():
    """轮转优先级:**没扫过的(0) → 扫过的(1) → 已知空壳(2)**。

    用 `case` 而不是 `last_scan_at IS NULL` 排序,是为了把"**空壳号**"单独降一档 ——
    它们不是"没扫过",而是"扫过且确认没东西",不该和外层完全一样的待遇。
    """
    from sqlalchemy import case

    from app.db.models import CrossPlatformAccount

    return case(
        (CrossPlatformAccount.last_scan_at.is_(None), 0),
        (CrossPlatformAccount.video_count == 0, 2),
        else_=1,
    )


def empty_account_summary(session, user_id: int) -> dict:
    """**给巡检/人看**:59 个号里扫过多少、空壳多少、还没扫多少。"""
    from sqlalchemy import func

    from app.db.models import CrossPlatformAccount

    base = select(func.count()).select_from(CrossPlatformAccount).where(
        CrossPlatformAccount.user_id == user_id,
        CrossPlatformAccount.platform == "bilibili",
        CrossPlatformAccount.status == "active")
    total = int(session.scalar(base) or 0)
    scanned = int(session.scalar(base.where(CrossPlatformAccount.last_scan_at.isnot(None))) or 0)
    empty = int(session.scalar(base.where(CrossPlatformAccount.last_scan_at.isnot(None),
                                          CrossPlatformAccount.video_count == 0)) or 0)
    return {"total": total, "scanned": scanned, "empty": empty, "unscanned": total - scanned}


def _settings():
    from config.settings import get_settings

    return get_settings()


def bili_account_scan_tick(settings=None) -> int:
    """定时入口:扫一轮,返回写入的标题条数。"""
    from app.db import get_session_local
    from app.db.models import User
    from app.services.tenant_base import _record_run

    from sqlalchemy import select as _select

    settings = settings or _settings()
    if not getattr(settings, "bili_scan_enabled", True):
        return 0
    db = get_session_local()()
    total = 0
    try:
        for (uid,) in db.execute(_select(User.id).where(User.enabled.is_(True))).all():
            try:
                # ⚠️ **先验 cookie 是否还有效**(nav 接口,判据 `data.isLogin`)。
                # 配了 cookie 却已失效 ⇒ **当场告警"去重新扫码"**;否则它会退回匿名风控,
                # 表现为"采集突然全失败",而人对着 412 只能猜。
                ck = _bili_cookie(db, uid, settings)
                if ck:
                    st = verify_login(ck)
                    if not st["is_login"]:
                        _alert_cookie_dead(db, uid, str(st.get("reason") or ""))
                        db.commit()
                out = scan_accounts(db, uid, settings=settings)
                total += out.get("titles", 0)
                # ⚠️ **不再有游标**:轮转改成按 `last_scan_at` / `video_count` 排序取队首
                # (见 `scan_accounts`),降权与"没扫过的优先"都由排序本身表达 ——
                # 中途增删号也不会像"游标 % 总数"那样错位跳过。
                summary = empty_account_summary(db, uid)
                _record_run(db, uid, "bili_account_scan", "success",
                            f"扫{out.get('scanned', 0)}个号 标题{out.get('titles', 0)}条"
                            f" **空壳{summary['empty']}/{summary['scanned']}扫过**"
                            f"(共{summary['total']}) {','.join(out.get('accounts') or [])}")
                db.commit()
            except Exception as exc:  # noqa: BLE001 - 单用户失败不影响其余
                # **限流必须记成 failed 而不是 success(0)** —— 否则"被挡住"看不见
                db.rollback()
                logger.warning("B站对标号扫描受阻:%s", str(exc)[:160])
                _record_run(db, uid, "bili_account_scan", "failed", str(exc)[:200])
                db.commit()
    finally:
        db.close()
    return total
