"""多租户服务:用【该用户自己】的 Cookie 采集,结果按 user_id 隔离存 MySQL。

复用现有采集器(collector / xianyu / douhot)与趋势分析(compute_growth/slope),
仅把**数据源**换成用户 Cookie、**存储**换成 per-user 的 ORM。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from config.settings import Settings, get_settings
from app.db.models import (
    AlertRecord,
    DouhotAlerted,
    DouhotWatch,
    DouhotWatchSnap,
    DouhotWord,
    RunRecord,
    WeiboHotItem,
    WeiboTrend,
    XianyuDaily,
    XianyuItem,
    XianyuSummary,
)
from app.services import alert_service, collector, douhot, xianyu
from app.services.cookie_store import get_cookies
from app.services.trend_analyzer import compute_growth, compute_slope
from app.utils import get_logger

logger = get_logger(__name__)


def _base(settings: Settings | None) -> Settings:
    return settings or get_settings()


def _record_run(session: Session, user_id: int, kind: str, status: str, detail: str = "") -> None:
    session.add(
        RunRecord(
            user_id=user_id,
            run_id=datetime.now().strftime("%Y%m%d%H%M%S"),
            kind=kind,
            status=status,
            started_at=datetime.now(),
            detail=detail,
        )
    )


def run_weibo(session: Session, user_id: int, settings: Settings | None = None) -> dict:
    settings = _base(settings)
    cookies = get_cookies(session, user_id)
    weibo_cookie = cookies.get("weibo", "")
    if not weibo_cookie:
        raise ValueError("未配置微博 Cookie")
    s = settings.model_copy(update={"weibo_cookie": weibo_cookie})
    try:
        items = collector.fetch_hot_search(s)
        now = datetime.now()
        prev_keys = set(session.scalars(select(WeiboHotItem.title).where(WeiboHotItem.user_id == user_id)).all())
        for it in items:
            session.add(
                WeiboHotItem(user_id=user_id, title=it.title, heat=it.heat, rank=it.rank, captured_at=now)
            )
        session.commit()
        rising = _weibo_rising(session, user_id, settings, now)
        latest = [{"key": it.title, "heat": it.heat} for it in items]
        latest += [{"key": r["keyword"], "growth": r["growth"]} for r in rising]
        alert_service.evaluate(session, user_id, "weibo", latest, prev_keys, settings)
        _record_run(session, user_id, "weibo", "success", f"items={len(items)}")
        session.commit()
        return {"platform": "weibo", "count": len(items), "rising": rising}
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        _record_run(session, user_id, "weibo", "failed", f"{type(exc).__name__}: {exc}")
        session.commit()
        raise


def _weibo_rising(session, user_id: int, settings: Settings, now) -> list[dict]:
    """基于该用户的历史热搜热度,判定微博上涨词。"""
    latest = session.scalars(
        select(WeiboHotItem).where(WeiboHotItem.user_id == user_id).order_by(WeiboHotItem.id.desc()).limit(200)
    ).all()
    by_word: dict[str, list[int]] = {}
    for it in reversed(latest):
        by_word.setdefault(it.title, []).append(it.heat)
    rising = []
    for title, heats in by_word.items():
        if len(heats) < 2:
            continue
        g = compute_growth([float(x) for x in heats])
        sl = compute_slope([float(x) for x in heats])
        if g is not None and sl is not None and g > settings.growth_threshold and sl > 0:
            session.add(WeiboTrend(user_id=user_id, keyword=title, source="weibo", growth=g, slope=sl, rising=True, decided_at=now))
            rising.append({"keyword": title, "growth": g, "slope": sl})
    rising.sort(key=lambda r: r["growth"], reverse=True)
    return rising[:20]


def run_xianyu(session: Session, user_id: int, settings: Settings | None = None) -> dict:
    settings = _base(settings)
    cookies = get_cookies(session, user_id)
    goofish_cookie = cookies.get("goofish", "")
    if not goofish_cookie:
        raise ValueError("未配置闲鱼 Cookie")
    try:
        client = xianyu.XianyuClient(goofish_cookie)
        hot = xianyu.collect_hot(settings, client)
        prev_keys = set(session.scalars(select(XianyuItem.item_id).where(XianyuItem.user_id == user_id)).all())
        for it in hot:
            session.add(XianyuItem(user_id=user_id, **it))
        session.commit()
        latest = [{"key": it["item_id"], "hit_keywords": it["hit_keywords"], "best_rank": it["best_rank"]} for it in hot]
        alert_service.evaluate(session, user_id, "xianyu", latest, prev_keys, settings)
        _record_run(session, user_id, "xianyu", "success", f"items={len(hot)}")
        session.commit()
        return {"platform": "xianyu", "count": len(hot)}
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        _record_run(session, user_id, "xianyu", "failed", f"{type(exc).__name__}: {exc}")
        session.commit()
        raise


def run_douhot(session: Session, user_id: int, settings: Settings | None = None) -> dict:
    settings = _base(settings)
    cookies = get_cookies(session, user_id)
    douyin_cookie = cookies.get("douyin", "")
    if not douyin_cookie:
        raise ValueError("未配置抖音(热点宝) Cookie")
    try:
        words = douhot.fetch_content_words(douyin_cookie, settings)
        now = datetime.now()
        prev_keys = set(session.scalars(select(DouhotWord.title).where(DouhotWord.user_id == user_id)).all())
        for w in words:
            session.add(DouhotWord(user_id=user_id, created_at=now, **w))
        session.commit()
        latest = [{"key": w["title"], "score": w["score"], "trend_delta": w.get("trend_delta", 0)} for w in words]
        alert_service.evaluate(session, user_id, "douhot", latest, prev_keys, settings)
        # 按用户关注类型补拉 搜索榜/我的订阅,并记录对应快照
        watch_types = {w.list_type for w in session.scalars(select(DouhotWatch).where(DouhotWatch.user_id == user_id)).all()}
        lists: dict[str, list[dict]] = {"word": words}
        if "search" in watch_types:
            lists["search"] = douhot.fetch_search_words(douyin_cookie, settings)
        if "subscribe" in watch_types:
            lists["subscribe"] = douhot.fetch_subscribe_words(douyin_cookie, settings)
        if "video" in watch_types:
            lists["video"] = douhot.fetch_video_words(douyin_cookie, settings)
        if "topic" in watch_types:
            lists["topic"] = douhot.fetch_topic_words(douyin_cookie, settings)
        _record_douhot_watch_snaps(session, user_id, lists)
        rising = _douhot_rising(session, user_id, settings, now)
        _record_run(session, user_id, "douhot", "success", f"words={len(words)} risen={len(rising)}")
        session.commit()
        return {"platform": "douhot", "count": len(words), "rising": rising}
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        _record_run(session, user_id, "douhot", "failed", f"{type(exc).__name__}: {exc}")
        session.commit()
        raise


def _douhot_rising(session, user_id: int, settings: Settings, now) -> list[dict]:
    rows = session.scalars(
        select(DouhotWord).where(DouhotWord.user_id == user_id).order_by(DouhotWord.id.desc()).limit(500)
    ).all()
    by_word: dict[str, list[float]] = {}
    for w in reversed(rows):
        by_word.setdefault(w.title, []).append(w.score)
    rising = []
    for title, scores in by_word.items():
        if len(scores) < 2:
            continue
        g = compute_growth(scores)
        sl = compute_slope(scores)
        if g is not None and sl is not None and g > settings.growth_threshold and sl > 0:
            # 冷却去重
            alerted = session.scalar(
                select(DouhotAlerted).where(DouhotAlerted.user_id == user_id, DouhotAlerted.title == title)
            )
            if alerted and (now - alerted.alerted_at).total_seconds() < settings.douhot_alert_cooldown_hours * 3600:
                continue
            if alerted is None:
                session.add(DouhotAlerted(user_id=user_id, title=title, alerted_at=now))
            session.add(AlertRecord(user_id=user_id, keyword=title, reason=f"抖音内容词飙升指数环比 {g:.0%}", triggered_at=now))
            rising.append({"title": title, "growth": g, "slope": sl})
    rising.sort(key=lambda r: r["growth"], reverse=True)
    return rising[: settings.douhot_alert_max]


def dashboard(session: Session, user_id: int) -> dict:
    """用户仪表盘:微博上涨 + 闲鱼热榜 + 抖音热词(按 user 隔离)。"""
    trends = [
        {
            "keyword": r.keyword,
            "growth": r.growth,
            "slope": r.slope,
            "decided_at": r.decided_at.isoformat() if r.decided_at else None,
        }
        for r in session.scalars(
            select(WeiboTrend).where(WeiboTrend.user_id == user_id).order_by(WeiboTrend.id.desc()).limit(20)
        ).all()
    ]
    xianyu_rows = session.scalars(
        select(XianyuItem).where(XianyuItem.user_id == user_id).order_by(XianyuItem.hit_keywords.desc(), XianyuItem.best_rank.asc()).limit(30)
    ).all()
    douhot_rows = session.scalars(
        select(DouhotWord).where(DouhotWord.user_id == user_id).order_by(DouhotWord.score.desc()).limit(100)
    ).all()
    return {
        "weibo_trends": trends,
        "xianyu_hot": [
            {"item_id": r.item_id, "title": r.title, "price": r.price, "hit_keywords": r.hit_keywords, "best_rank": r.best_rank}
            for r in xianyu_rows
        ],
        "douhot_words": [
            {"title": r.title, "score": r.score, "trend_delta": r.trend_delta}
            for r in douhot_rows
        ],
    }


def xianyu_daily(session: Session, user_id: int) -> dict:
    today = datetime.now().date().isoformat()
    summary = session.scalar(
        select(XianyuSummary).where(XianyuSummary.user_id == user_id).order_by(XianyuSummary.id.desc())
    )
    return {"summary_date": summary.summary_date if summary else None, "items": []}


# ---------- 闲鱼深度分析 ----------
def _xy_detail_limit(settings: Settings) -> int:
    return getattr(settings, "xianyu_detail_limit", 20)


def run_xianyu_deep(session: Session, user_id: int, settings: Settings | None = None) -> dict:
    """采集闲鱼热榜并抓取前 N 商品详情(想要数/类目/浏览量/卖家粉丝),写入当日快照。"""
    settings = _base(settings)
    cookies = get_cookies(session, user_id)
    goofish = cookies.get("goofish", "")
    if not goofish:
        raise ValueError("未配置闲鱼 Cookie")
    try:
        client = xianyu.XianyuClient(goofish)
        hot = xianyu.collect_hot(settings, client)
        today = datetime.now().date().isoformat()
        base_delay = getattr(settings, "request_delay_seconds", 2.5)
        saved = 0
        for idx, it in enumerate(hot[: _xy_detail_limit(settings)]):
            detail = xianyu.fetch_detail(client, it["item_id"])
            row = session.scalar(
                select(XianyuDaily).where(
                    XianyuDaily.user_id == user_id,
                    XianyuDaily.item_id == it["item_id"],
                    XianyuDaily.snap_date == today,
                )
            )
            if row is None:
                row = XianyuDaily(user_id=user_id, snap_date=today, item_id=it["item_id"])
                session.add(row)
            row.title = it["title"][:500]
            row.price = it["price"]
            row.category = detail.get("category", "")
            row.want_count = detail.get("want_count", 0)
            row.collect_count = detail.get("collect_count", 0)
            row.sold_count = detail.get("sold_count", 0)
            row.view_count = detail.get("view_count", 0)
            row.seller_fans = detail.get("seller_fans", 0)
            saved += 1
            if idx < _xy_detail_limit(settings) - 1:
                import random
                import time

                time.sleep(base_delay * random.uniform(0.8, 1.4))
        session.commit()
        _record_run(session, user_id, "xianyu_deep", "success", f"items={saved}")
        session.commit()
        return {"platform": "xianyu_deep", "count": saved}
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        _record_run(session, user_id, "xianyu_deep", "failed", f"{type(exc).__name__}: {exc}")
        session.commit()
        raise


def xianyu_analytics(session: Session, user_id: int) -> dict:
    """闲鱼深度面板:今日vs昨日 想要数涨跌、类目分布、上升/下降榜。"""
    today = datetime.now().date().isoformat()
    yesterday = (datetime.now().date() - timedelta(days=1)).isoformat()
    today_rows = {
        r.item_id: r
        for r in session.scalars(select(XianyuDaily).where(XianyuDaily.user_id == user_id, XianyuDaily.snap_date == today)).all()
    }
    yesterday_rows = {
        r.item_id: r
        for r in session.scalars(select(XianyuDaily).where(XianyuDaily.user_id == user_id, XianyuDaily.snap_date == yesterday)).all()
    }
    items = []
    for iid, t in today_rows.items():
        y = yesterday_rows.get(iid)
        y_want = y.want_count if y else None
        delta = (t.want_count - y_want) if y_want is not None else 0
        pct = (delta / y_want) if y_want else (100.0 if t.want_count > 0 else 0.0)
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
                "view_today": t.view_count,
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


# ---------- 热点宝关键词监控 ----------
def add_douhot_watch(session: Session, user_id: int, list_type: str, keyword: str) -> dict:
    list_type = list_type if list_type in ("word", "search", "subscribe", "video", "topic") else "word"
    keyword = keyword.strip()
    row = session.scalar(
        select(DouhotWatch).where(
            DouhotWatch.user_id == user_id, DouhotWatch.list_type == list_type, DouhotWatch.keyword == keyword
        )
    )
    if row is None:
        session.add(DouhotWatch(user_id=user_id, list_type=list_type, keyword=keyword))
        session.commit()
    return {"list_type": list_type, "keyword": keyword}


def list_douhot_watch(session: Session, user_id: int) -> list[dict]:
    rows = session.scalars(select(DouhotWatch).where(DouhotWatch.user_id == user_id)).all()
    return [{"list_type": r.list_type, "keyword": r.keyword} for r in rows]


def _record_douhot_watch_snaps(session: Session, user_id: int, lists: dict[str, list[dict]]) -> None:
    """把用户关注的词,在对应榜单(内容词/搜索/订阅)中记录得分与排名快照。"""
    watches = session.scalars(select(DouhotWatch).where(DouhotWatch.user_id == user_id)).all()
    for w in watches:
        words = lists.get(w.list_type, [])
        score, rank = 0, 0
        for i, word in enumerate(words, start=1):
            if word.get("title") == w.keyword:
                score, rank = word.get("score", 0), i
                break
        session.add(
            DouhotWatchSnap(user_id=user_id, list_type=w.list_type, keyword=w.keyword, score=score, rank_now=rank)
        )
    session.commit()


def douhot_watch_analytics(session: Session, user_id: int) -> list[dict]:
    watches = session.scalars(select(DouhotWatch).where(DouhotWatch.user_id == user_id)).all()
    out = []
    for w in watches:
        snaps = session.scalars(
            select(DouhotWatchSnap)
            .where(DouhotWatchSnap.user_id == user_id, DouhotWatchSnap.keyword == w.keyword)
            .order_by(DouhotWatchSnap.id.asc())
        ).all()
        values = [s.score for s in snaps]
        growth = compute_growth(values) if len(values) >= 2 else None
        out.append(
            {
                "keyword": w.keyword,
                "list_type": w.list_type,
                "last_score": values[-1] if values else 0,
                "rank_now": snaps[-1].rank_now if snaps else 0,
                "points": len(values),
                "growth": growth,
            }
        )
    return out


def run_section_for_all_users(section: str, settings: Settings | None = None) -> dict:
    """为【所有用户】跑某个板块采集(用各人自己的 Cookie)。

    常规定时采集已改为**按每个用户自己设置的频率**执行(见
    `app/services/scheduler.py::collect_tick`);本函数保留给"批量/全员立即采集"
    这类运维场景使用。
    """
    from app.db import get_session_local
    from app.db.models import User

    settings = settings or get_settings()
    runners = {"weibo": run_weibo, "xianyu": run_xianyu, "douhot": run_douhot}
    if section not in runners:
        return {"section": section, "users": 0, "ok": 0, "failed": 0}
    db = get_session_local()()
    users = db.scalars(select(User).order_by(User.id)).all()
    ok = failed = 0
    for u in users:
        try:
            runners[section](db, u.id, settings)
            ok += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            logger.warning("定时采集 %s 用户 %s 失败:%s", section, u.id, exc)
            db.rollback()
    db.close()
    logger.info("定时采集 %s 用户=%s 成功=%s 失败=%s", section, len(users), ok, failed)
    return {"section": section, "users": len(users), "ok": ok, "failed": failed}
