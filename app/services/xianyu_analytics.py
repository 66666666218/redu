"""闲鱼分析域(见 doc/dev.md §5.8):每日快照、深度采集、涨跌分析。

从 `tenant.py` 拆分而来,避免单文件过大。依赖 `tenant_base` 的 `_base`/`_record_run`,
不反向 import tenant,规避循环依赖。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from config.settings import Settings
from app.db import repository
from app.db.models import XianyuDaily, XianyuItem, RunRecord
from app.services import xianyu
from app.services.cookie_store import get_cookies
from app.services import alert_service
from app.services.tenant_base import _base, _record_run, persist_refreshed_cookie, verify_cooldown_active
from app.utils import get_logger

logger = get_logger(__name__)


def xianyu_daily(session: Session, user_id: int) -> dict:
    """当日闲鱼快照明细(深采落进 `xianyu_daily` 表的那些商品)。

    与 `xianyu_analytics` 的分工:那个算**今日 vs 昨日的涨跌**(面板用),这个只给
    **当日原始快照**(想要数/收藏/出单/浏览量/类目),供列表或导出用。

    历史注:本函数此前返回 `{"summary_date": …, "items": []}` —— 它读的 `XianyuSummary`
    表**全项目没有任何写入方**(设计了一半的链路),所以 `items` 恒为空、`today` 变量算了
    没用(2026-10-01 审查发现)。现改为直接读当日快照表,即 `run_xianyu_deep` 真正写入的那张。
    """
    today = datetime.now().date().isoformat()
    rows = repository.xianyu_daily_by_date(session, user_id, today)
    return {
        "date": today,
        "count": len(rows),
        "items": [
            {
                "item_id": r.item_id,
                "title": r.title,
                "price": r.price,
                "category": r.category or "未分类",
                "want_count": r.want_count,
                "collect_count": r.collect_count,
                "sold_count": r.sold_count,
                "view_count": r.view_count,
                "seller_fans": r.seller_fans,
            }
            for r in rows
        ],
    }


def _deep_done_today(session: Session, user_id: int, today: str) -> set[str]:
    """当天**已抓过详情**的商品 id。

    ⚠️ **只认 `source='detail'`**:搜索快照(record_search_snapshot)也会写当日行,
    若把它们也算成"已抓过详情",深采会永远认为没活干、收藏/出单/浏览量永久为空
    (2026-10-03 引入 source 列的原因之一)。
    """
    return {r.item_id for r in repository.xianyu_daily_by_date(session, user_id, today)
            if (r.source or "detail") == "detail"}


def _xy_detail_limit(settings: Settings) -> int:
    return getattr(settings, "xianyu_detail_limit", 20)


def xianyu_deep_due(session: Session, user_id: int, settings: Settings) -> bool:
    """闲鱼深采是否到期:距上次成功深采 >= `xianyu_deep_interval_hours`,且当前不在验证冷却。

    搜索接力深采时用——避免每次搜索(2h)都跑一次深采(10详情)累积风控;默认 6 小时一次。

    ⚠️ **`xianyu_detail_limit <= 0` = 深采整条关**(不是"跑但抓 0 个")。
    这条开关的**来龙去脉**(2026-10-03):用户先说"只需要抓取虚拟资料标题",我据此关了深采
    → 用户随即纠正"**价格和行情需要**" —— 确实我执行过头了:
      · **价格**在搜索列表里**免费带回来**,不需要详情;
      · **行情**(想要数/收藏/出单/浏览量)只有详情接口有,正是 `XianyuDaily` 的来源。
    所以默认**开着**(10 条);关掉它会让「关注词分析」与管理后台的闲鱼统计**变空**。
    (风控考量:详情确是最大爆发点,但**现在走浏览器路径**,暴露比纯协议时代低得多。)
    """
    if int(getattr(settings, "xianyu_detail_limit", 10) or 0) <= 0:
        return False
    if verify_cooldown_active(session, user_id, settings):
        return False
    hours = getattr(settings, "xianyu_deep_interval_hours", None) or 6
    cutoff = datetime.now() - timedelta(hours=hours)
    last = session.scalar(
        select(RunRecord).where(
            RunRecord.user_id == user_id, RunRecord.kind == "xianyu_deep",
            RunRecord.status.in_(["success", "partial"]), RunRecord.started_at >= cutoff,
        ).order_by(RunRecord.id.desc())
    )
    return last is None


def run_xianyu_deep(session: Session, user_id: int, settings: Settings | None = None,
                    hot: list[dict] | None = None) -> dict:
    """采集闲鱼热榜并抓取前 N 商品详情(想要数/类目/浏览量/卖家粉丝),写入当日快照。

    `hot` 可传已采集的热榜(搜索接力深采时复用,避免重复搜索);缺省则自行 collect_hot。
    """
    settings = _base(settings)
    if verify_cooldown_active(session, user_id, settings):  # 验证后冷却:避免反复撞滑块
        _record_run(session, user_id, "xianyu_deep", "skipped", "verify_cooldown")
        session.commit()
        return {"platform": "xianyu_deep", "count": 0, "status": "skipped", "reason": "verify_cooldown"}
    cookies = get_cookies(session, user_id)
    goofish = cookies.get("goofish", "")
    # ⚠️ **客户端要跟搜索那条路一致**(2026-10-03 修):深采此前**还在用纯协议客户端**,
    # 而搜索早换了浏览器路 → 行情(想要数/收藏/出单)一直卡在"被挤爆"那条路上。
    browser_mode = bool(getattr(settings, "xianyu_use_browser", True))
    if not browser_mode and not goofish:
        raise ValueError("未配置闲鱼 Cookie")
    # 构造客户端不产生网络请求,放在 try 外:失败路径也能回写运行中刷新的令牌
    if browser_mode:
        from app.services.xianyu_browser import get_client

        client = get_client(settings)
    else:
        client = xianyu.XianyuClient(goofish, proxy=settings.xianyu_proxy_url or None)
    try:
        if hot is None:
            hot = xianyu.collect_hot(settings, client)
        today = datetime.now().date().isoformat()
        limit = _xy_detail_limit(settings)
        # 当天已抓过**详情**的商品不再重复请求:详情是 mtop 风控最大爆发点,额度优先留给
        # 当天尚未抓取的商品——无代理换 IP 时,靠"减少请求总量"降低风控概率。
        # ⚠️ **只数 source='detail' 的行**:搜索快照(record_search_snapshot)也会写当日行,
        # 若把它们也算成"已抓过详情",深采会永远认为没活干、收藏/出单/浏览量永久为空。
        done_today = _deep_done_today(session, user_id, today)
        todo = [it for it in hot if str(it.get("item_id") or "") not in done_today][:limit]
        if not todo:
            _record_run(session, user_id, "xianyu_deep", "skipped",
                        f"cached_today({len(done_today)} 条已抓,跳过重复详情)")
            session.commit()
            return {"platform": "xianyu_deep", "count": 0, "status": "skipped", "reason": "cached_today"}
        base_delay = getattr(settings, "xianyu_request_delay", None) or getattr(settings, "request_delay_seconds", 2.5)
        saved = 0
        stop_reason = None
        for idx, it in enumerate(todo):
            try:
                detail = xianyu.fetch_detail(client, it["item_id"])
            except xianyu.XianyuVerify:
                stop_reason = "verify"
                logger.warning("闲鱼详情触发人机验证,停止抓取;请人工过滑块或更换出口IP")
                break
            except xianyu.XianyuWafBlock:
                stop_reason = "waf_block"
                logger.warning("闲鱼详情触发 WAF 拦截(网关空响应),停止抓取;建议更换出口IP")
                break
            except xianyu.XianyuRateLimit:
                stop_reason = "rate_limit"
                logger.warning("闲鱼限流,停止抓取详情")
                break
            if detail is None:
                # fetch_detail 对单品普通失败返回 None(契约:不写假 0 快照)。此处必须跳过:
                # 直接 detail.get(...) 会抛 AttributeError → 外层 except → rollback 丢掉本轮全部
                # 已采 saved 行并 raise,一条坏详情毁掉整轮深采。
                logger.warning("闲鱼详情抓取失败,跳过该商品 item_id=%s", it["item_id"])
                continue
            row = repository.get_xianyu_daily(session, user_id, it["item_id"], today)
            if row is None:
                row = XianyuDaily(user_id=user_id, snap_date=today, item_id=it["item_id"])
                session.add(row)
            # 深采是**升级**:盖掉同名搜索行(source 转 detail),从此该行不被搜索覆盖
            row.source = "detail"
            row.title = it["title"][:500]
            row.price = str(it["price"] or "")[:64]
            row.category = detail.get("category", "")[:64]
            row.want_count = detail.get("want_count", 0)
            row.collect_count = detail.get("collect_count", 0)
            row.sold_count = detail.get("sold_count", 0)
            row.seller_fans = detail.get("seller_fans", 0)
            saved += 1
            if idx < len(todo) - 1:
                import random
                import time

                time.sleep(base_delay * random.uniform(0.8, 1.4))
        session.commit()
        status = "partial" if stop_reason else "success"
        detail_note = f"items={saved}" + (f",{stop_reason}" if stop_reason else "")
        _record_run(session, user_id, "xianyu_deep", status, detail_note)
        session.commit()
        persist_refreshed_cookie(session, user_id, client)
        if stop_reason == "verify":
            alert_service.notify_incident(
                session, user_id, "xianyu",
                "⚠️ 闲鱼详情抓取触发人机验证",
                f"深采在第 {saved + 1} 个商品处触发滑块(已采 {saved} 条,状态 partial);"
                f"搜索采集不受影响",
                settings=settings,
                # 已采到的部分保住了、搜索照常,只是深采提前停 → 属可自愈的降级,只进站内
                push_feishu=False)
        return {"platform": "xianyu_deep", "count": saved, "status": status, "reason": stop_reason}
    except (xianyu.XianyuVerify, xianyu.XianyuRateLimit, xianyu.XianyuWafBlock) as exc:
        # collect_hot 整轮被滑块/限流(全部关键词失败)→ 优雅降级,不 500
        session.rollback()
        _record_run(session, user_id, "xianyu_deep", "failed", f"{type(exc).__name__}: {exc}")
        session.commit()
        persist_refreshed_cookie(session, user_id, client)
        return {"platform": "xianyu_deep", "count": 0, "status": "failed", "reason": type(exc).__name__}
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        _record_run(session, user_id, "xianyu_deep", "failed", f"{type(exc).__name__}: {exc}")
        session.commit()
        persist_refreshed_cookie(session, user_id, client)
        raise


def record_search_snapshot(session: Session, user_id: int, hot: list[dict],
                           today: str | None = None) -> int:
    """把**搜索**带回来的热度写进当日快照 —— 零额外请求、不碰详情接口、不求人过滑块。

    **为什么这是关键一步**(2026-10-03):行情(想要数)此前唯一来源是详情接口
    `mtop.taobao.idle.pc.detail`,而它被阿里无痕验证挡着(要人工拖滑块)。后来发现
    **搜索响应里就带着**「6770人想要」标签(90 条命中 86 条 = 95%),于是需求热度
    **完全不必打详情接口** —— 每轮采集顺手就更新了。

    深采(详情接口)由此降级为**锦上添花**:它多给收藏/出单/浏览量,受滑块限制、允许失败,
    失败也不再让行情面板变空。

    ⚠️ **不许覆盖深采行**:同日同商品只有一行(唯一约束),深采行含搜索给不出的
    收藏/出单/浏览量,拿搜索值盖上去会把它们抹成 0。反向(深采盖搜索行)由
    `run_xianyu_deep` 负责 —— 那个方向是升级,允许。
    """
    today = today or datetime.now().date().isoformat()
    written = 0
    for it in hot:
        iid = str(it.get("item_id") or "")
        if not iid:
            continue
        row = repository.get_xianyu_daily(session, user_id, iid, today)
        if row is not None and (row.source or "detail") == "detail":
            continue                                  # 深采行更全,让位
        if row is None:
            row = XianyuDaily(user_id=user_id, snap_date=today, item_id=iid, source="search")
            session.add(row)
        row.title = (it.get("title") or "")[:500]
        row.price = str(it.get("sold_price") or it.get("price") or "")[:64]
        row.want_count = int(it.get("want_count") or 0)
        row.tags = str(it.get("tags") or "")[:255]
        row.source = "search"
        written += 1
    if written:
        session.commit()
    return written


def _median(nums: list[float]) -> float:
    if not nums:
        return 0.0
    s = sorted(nums)
    mid = len(s) // 2
    return round(s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2, 2)


_PRICE_BUCKETS = ((0, 1), (1, 3), (3, 5), (5, 10), (10, 30), (30, 100), (100, float("inf")))


def xianyu_market(session: Session, user_id: int, days: int = 30) -> dict:
    """价位行情 —— **价格看供给、想要数看需求,相除才是热度**。

    单看价格只能说明供给一侧:同款在售条数越多、价被压得越低,越说明已是**红海**
    (有人验证过能卖,但你在跟几百个同行抢)。真正的需求强度要看**想要数**
    —— 2026-10-03 起它由搜索响应免费提供(见 `xianyu.item_want`),不必打详情接口。

    于是本函数的核心指标是 **供需比 = 想要总数 ÷ 在售条数**:
      · 高 → 想买的人多、供给少 → **蓝海**(值得铺资源);
      · 低 → 一堆人在卖却没人想要 → 别碰。
    """
    cutoff = datetime.now() - timedelta(days=days)
    rows = list(session.scalars(select(XianyuItem).where(
        XianyuItem.user_id == user_id, XianyuItem.created_at >= cutoff,
    ).order_by(XianyuItem.created_at.desc())).all())
    rows = xianyu.dedupe_resources(rows, len(rows))   # 折叠"同一资源换 ID 重上架"

    prices: list[float] = []
    wants: list[int] = []
    sellers: set[str] = set()
    per_kw: dict[str, dict] = {}
    for r in rows:
        p = xianyu._price_num(r.sold_price or r.price)
        want = int(r.want_count or 0)
        if p != float("inf"):
            prices.append(p)
        wants.append(want)
        if r.seller:
            sellers.add(r.seller)
        kw = (r.keywords or "").split(",")[0].strip() or "未归类"
        agg = per_kw.setdefault(kw, {"items": 0, "prices": [], "want": 0, "cut": 0})
        agg["items"] += 1
        agg["want"] += want
        if p != float("inf"):
            agg["prices"].append(p)
        if "降价" in (r.tags or ""):
            agg["cut"] += 1

    keywords = []
    for kw, a in per_kw.items():
        avg = round(sum(a["prices"]) / len(a["prices"]), 2) if a["prices"] else 0.0
        keywords.append({
            "keyword": kw,
            "items": a["items"],
            "min_price": round(min(a["prices"]), 2) if a["prices"] else 0.0,
            "median_price": _median(a["prices"]),
            "avg_price": avg,
            "want_total": a["want"],
            "ratio": round(a["want"] / a["items"], 1) if a["items"] else 0.0,
            "price_cut": a["cut"],
        })
    # 样本太小的词噪音大(1 条就能刷出离谱的供需比)→ 蓝海榜要求至少 3 条在售
    solid = [k for k in keywords if k["items"] >= 3]
    buckets = []
    for lo, hi in _PRICE_BUCKETS:
        n = sum(1 for p in prices if lo <= p < hi)
        label = f"{lo}-{hi}" if hi != float("inf") else f"{lo}+"
        buckets.append({"range": label, "count": n})
    total_want = sum(wants)
    return {
        "days": days,
        "item_count": len(rows),
        "seller_count": len(sellers),
        "supply": {
            "min_price": round(min(prices), 2) if prices else 0.0,
            "median_price": _median(prices),
            "avg_price": round(sum(prices) / len(prices), 2) if prices else 0.0,
            "p25_price": round(sorted(prices)[len(prices) // 4], 2) if prices else 0.0,
        },
        "demand": {"want_total": total_want, "want_median": _median([float(w) for w in wants])},
        "ratio": round(total_want / len(rows), 1) if rows else 0.0,
        "keywords": sorted(keywords, key=lambda k: -k["items"]),
        "blue_ocean": sorted(solid, key=lambda k: -k["ratio"])[:10],   # 想要多、在售少
        "red_ocean": sorted(solid, key=lambda k: -k["items"])[:10],    # 一堆人在卖
        "price_buckets": buckets,
    }


def xianyu_analytics(session: Session, user_id: int) -> dict:
    """闲鱼深度面板:今日vs昨日 想要数涨跌、类目分布、上升/下降榜。"""
    today = datetime.now().date().isoformat()
    yesterday = (datetime.now().date() - timedelta(days=1)).isoformat()
    today_rows = {r.item_id: r for r in repository.xianyu_daily_by_date(session, user_id, today)}
    yesterday_rows = {r.item_id: r for r in repository.xianyu_daily_by_date(session, user_id, yesterday)}
    items = []
    for iid, t in today_rows.items():
        y = yesterday_rows.get(iid)
        y_want = y.want_count if y else None
        delta = (t.want_count - y_want) if y_want is not None else 0
        # 昨日无基线 → pct=None(显示"—"),不给"无基线"强算 +100% 误报暴涨
        pct = (delta / y_want) if y_want else (None if y_want is None else 0.0)
        items.append(
            {
                "item_id": iid,
                "title": t.title[:44],
                "category": t.category or "未分类",
                "price": t.price,
                "want_today": t.want_count,
                "want_yesterday": y_want,
                "delta": delta,
                "pct": pct,
                "collect_today": t.collect_count,
                "sold_today": t.sold_count,
                "seller_fans": t.seller_fans,
            }
        )
    items.sort(key=lambda x: x["delta"], reverse=True)
    cats: dict[str, int] = {}
    total_want = 0
    for it in items:
        cats[it["category"]] = cats.get(it["category"], 0) + 1
        total_want += it["want_today"]
    return {
        "date": today,
        "count": len(items),
        "total_want": total_want,
        "top_risers": items[:10],
        "top_fallers": sorted(items, key=lambda x: x["delta"])[:10],
        "categories": [{"name": k, "count": v} for k, v in sorted(cats.items(), key=lambda x: -x[1])],
        "items": items,
    }
