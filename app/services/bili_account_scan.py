"""B站对标号的**投稿标题**采集(2026-10-05)。

**为什么只采标题、不采链**:用户口径「**b站如果没有链可以只采集标题,从资源库里搜然后完善**」。
这恰好绕开了实测的死结 —— 签名、`space` 接口都通,但**视频简介里拿不到盘链**
(实测 30 条投稿的 `desc` 里一条 `pan.quark.cn`/`pan.baidu.com` 都没有;B站的链不在这儿)。
而**标题本身就是资源名**(实测正是「野鹅敢死队 经典影片 国语配音」这类),够用了。

**落到哪**:`hot_source_items`(`source="bili-pan"`)。这是**刻意的** ——
选题 Agent 的 `_platform_hot_candidates` 本就从这张表取候选,拿到之后
`_library_evidence` 会**自动**去资源库查"这个资源我们有没有、有没有我方链"。
于是"**只采标题 → 回资源库补全**"这条链**不用改 Agent 一行代码**就通了。

⚠️ **这个作业必须挂在 `hotspot`(远程)角色**,不能挂本机:
① 产物落 `hot_source_items`,而**读它的选题 Agent(`agent_tick_all_users`)也在远程**,
   且 `remote_sync` **不推这张表** —— 挂本机的话标题永远到不了 Agent 眼前;
② 它需要的对标号来自 `cross_platform_accounts`,那张表**已经同步到远程**。

⚠️⚠️ **关于限流,别被"远程跑不动"这种第一印象带偏**(2026-10-05 亲历):
两个 IP 现在**都** 412,看起来像"机房 IP 被封",于是很容易得出"该挪回本机"的结论 ——
**那是错的**。做**受控对比**(同一时刻、同一套签名、两种 IP × 两个端点)才看清真相:

| 端点 | 本机 | 远程 |
|---|---|---|
| `search/type`(搜用户/搜视频) | OK | OK |
| `space/wbi/arc/search` | **412** | **412** |

⇒ **限流是按端点分的,与 IP 无关**,`space` 的额度**远紧于** `search`;两边表现一模一样,
远程 IP **没有被封**。烧额度的是我当天的密集探测(先是本机成功取到 30 条**两次**,
连打几次之后才开始 412 —— 注意这本身就证明"能通")。
另外试过铸 `buvid3` 带上,**无效**(仍 412),别再重复这条路。

⚠️⚠️ **限流是这条链的头号风险**:B站 `space` 接口**匿名额度很低** —— 实测连发两次就
`HTTP 412`(返回 HTML 拦截页,不是 JSON),再试是 `code=-352`(频率限制)。
所以:① **每轮只扫一个号**;② 游标轮转,59 个号轮着来;③ **限流必须抛**,
绝不 `return []` —— 否则"被挡住"会记成"这个号没投稿",与本仓反复踩的
「静默失败 = 假成功」一模一样(`falsification-needs-control-variables` 也提醒:
拿被限流的样本下结论是错的)。
"""
from __future__ import annotations

import hashlib
import time
import urllib.parse

from sqlalchemy import select

from app.utils import get_logger

logger = get_logger(__name__)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_CURSOR_KEY = "bili_account_scan_cursor"
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


def _cursor(session) -> int:
    from app.db.models import SystemConfig

    row = session.scalar(select(SystemConfig).where(SystemConfig.key == _CURSOR_KEY))
    try:
        return int(row.value) if row and row.value else 0
    except (TypeError, ValueError):
        return 0


def advance_cursor(session, n: int = 1) -> None:
    """把窗口往前推(在**整轮跑完之后**调,与 `cross_accounts.advance_bili_cursor` 同口径)。"""
    from app.db.models import SystemConfig

    row = session.scalar(select(SystemConfig).where(SystemConfig.key == _CURSOR_KEY))
    nxt = str(_cursor(session) + max(1, int(n or 1)))
    if row is None:
        session.add(SystemConfig(key=_CURSOR_KEY, value=nxt))
    else:
        row.value = nxt
    session.commit()


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
        CrossPlatformAccount.status == "active").order_by(CrossPlatformAccount.id)).all()
    start = _cursor(session) % len(rows)
    picked = [rows[(start + i) % len(rows)] for i in range(min(max(1, n), len(rows)))]

    written, seen_names = 0, []
    ck = _bili_cookie(session, user_id, settings)      # 登录态能显著放宽 space 端点的风控
    if not ck:
        logger.info("未配 B站 cookie,本轮按**匿名**取(更可能被风控挡);"
                    "扫码一次即可:python scripts/bili_login.py")
    for acc in picked:
        titles = fetch_user_titles(acc.uid, cookie=ck)   # 限流/风控会在这里抛,整轮中止(有意)
        written += _save_titles(session, user_id, acc, titles, settings)
        seen_names.append(f"{acc.name}({len(titles)})")
        time.sleep(2.0)                              # 号与号之间留间隔,别连发
    session.commit()
    logger.info("B站对标号扫描:游标 %d/%d → 扫 %s,写入标题 %d 条",
                start, len(rows), "、".join(seen_names), written)
    return {"status": "ok", "scanned": len(picked), "titles": written,
            "accounts": seen_names, "cursor": start}


def _settings():
    from config.settings import get_settings

    return get_settings()


def bili_account_scan_tick(settings=None) -> int:
    """定时入口:扫一轮,返回写入的标题条数。"""
    from app.db import get_session_local
    from app.db.models import User, SystemConfig
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
                out = scan_accounts(db, uid, settings=settings)
                total += out.get("titles", 0)
                # ⚠️ **跑完才推游标**(与 cross_accounts 同口径):中途抛了就不推,
                # 下一轮从同一个号重来 —— 否则被限流的那个号会被**永久跳过**。
                if out.get("status") == "ok" and out.get("scanned"):
                    cnt = int(getattr(settings, "bili_scan_accounts_per_run", 1) or 1)
                    row = db.scalar(_select(SystemConfig).where(
                        SystemConfig.key == _CURSOR_KEY))
                    nxt = str(_cursor(db) + max(1, cnt))
                    if row is None:
                        db.add(SystemConfig(key=_CURSOR_KEY, value=nxt))
                    else:
                        row.value = nxt
                _record_run(db, uid, "bili_account_scan", "success",
                            f"扫{out.get('scanned', 0)}个号 标题{out.get('titles', 0)}条"
                            f" {','.join(out.get('accounts') or [])}")
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
