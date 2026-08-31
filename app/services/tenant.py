"""多租户服务:用【该用户自己】的 Cookie 采集,结果按 user_id 隔离存 MySQL。

复用现有采集器(collector / xianyu / douhot)与趋势分析(compute_growth/slope),
仅把**数据源**换成用户 Cookie、**存储**换成 per-user 的 ORM。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from config.settings import Settings, get_settings
from app.db.models import (
    AlertRecord,
    DouhotAlerted,
    DouhotWord,
    RunRecord,
    WeiboHotItem,
    WeiboTrend,
    XianyuItem,
    XianyuSummary,
)
from app.services import collector, douhot, xianyu
from app.services.cookie_store import get_cookies
from app.services.trend_analyzer import compute_growth, compute_slope
from app.utils import get_logger

logger = get_logger(__name__)


def _base(settings: Settings | None) -> Settings:
    return settings or get_settings()


def _record_run(session: Session, user_id: int, kind: str, status: str, detail: str = "") -> None:
    session.add(
        RunRecord(user_id=user_id, kind=kind, status=status, started_at=datetime.now(), detail=detail)
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
        for it in items:
            session.add(
                WeiboHotItem(user_id=user_id, title=it.title, heat=it.heat, rank=it.rank, captured_at=now)
            )
        session.commit()
        rising = _weibo_rising(session, user_id, settings, now)
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
        for it in hot:
            session.add(XianyuItem(user_id=user_id, **it))
        session.commit()
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
        words = douhot.fetch_content_words(douyin_cookie)
        now = datetime.now()
        for w in words:
            session.add(DouhotWord(user_id=user_id, created_at=now, **w))
        session.commit()
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
        select(DouhotWord).where(DouhotWord.user_id == user_id).order_by(DouhotWord.score.desc()).limit(30)
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
