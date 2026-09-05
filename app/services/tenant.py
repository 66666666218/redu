"""多租户服务:用【该用户自己】的 Cookie 采集,结果按 user_id 隔离存 MySQL。

本模块 = **采集编排 + 仪表盘 + 判涨**(每个平台的运行器与大屏数据)。
已拆分的域:
- 闲鱼分析(每日快照/深度采集/涨跌)→ `xianyu_analytics.py`
- 关键词监控 + 智能体预测 → `keyword_watch.py`
- 共享 `_base`/`_record_run` → `tenant_base.py`

各域模块统一 re-export 到 `tenant.xxx`,保证外部(路由/测试)引用不变。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from config.settings import Settings, get_settings
from app.db.models import (
    AlertRecord,
    BaiduHotItem,
    DouhotAlerted,
    DouhotWatch,
    DouhotWord,
    RunRecord,
    WeiboHotItem,
    WeiboTrend,
    XianyuItem,
)
from app.services import alert_service, baidu, collector, douhot, xianyu
from app.services.cookie_store import get_cookies
from app.db import repository
from app.services.trend_analyzer import compute_growth, compute_slope
from app.services.tenant_base import _base, _record_run, verify_cooldown_active  # noqa: F401  (供外部/测试引用)
from app.services.xianyu_analytics import run_xianyu_deep, xianyu_analytics, xianyu_daily, xianyu_deep_due  # noqa: F401
from app.services.keyword_watch import (  # noqa: F401
    _record_douhot_watch_snaps,
    add_douhot_watch,
    douhot_watch_analytics,
    list_douhot_watch,
    platform_agent,
)
from app.utils import get_logger

logger = get_logger(__name__)


def _record_watch(session: Session, user_id: int, section: str, items: list[dict]) -> None:
    """采集后记录该板块用户监控关键词的快照(items 为 [{"title","value"}])。"""

    def _list() -> dict[str, list[dict]]:
        return {"word": [{"title": it.get("title", ""), "value": it.get("value", it.get("score", 0))} for it in items]}

    from app.services.keyword_watch import record_watch_snaps

    try:
        record_watch_snaps(section, session, user_id, _list())
    except Exception:  # noqa: BLE001 - 记录失败不中断采集
        session.rollback()


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
        _record_watch(session, user_id, "weibo", [{"title": it.title, "value": it.heat} for it in items])
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
    latest = repository.weibo_items(session, user_id, limit=200)
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


def run_baidu(session: Session, user_id: int, settings: Settings | None = None) -> dict:
    """采集百度热搜(公开接口,无需登录态),按 user 存库并判涨。"""
    settings = _base(settings)
    try:
        items = baidu.fetch_hot(settings)
        now = datetime.now()
        prev_keys = set(session.scalars(select(BaiduHotItem.title).where(BaiduHotItem.user_id == user_id)).all())
        for it in items:
            session.add(BaiduHotItem(user_id=user_id, title=it.title, heat=it.heat, rank=it.rank, url=it.url, captured_at=now))
        session.commit()
        rising = _baidu_rising(session, user_id, settings, now)
        latest = [{"key": it.title, "heat": it.heat} for it in items]
        latest += [{"key": r["keyword"], "growth": r["growth"]} for r in rising]
        alert_service.evaluate(session, user_id, "baidu", latest, prev_keys, settings)
        _record_watch(session, user_id, "baidu", [{"title": it.title, "value": it.heat} for it in items])
        _record_run(session, user_id, "baidu", "success", f"items={len(items)}")
        session.commit()
        return {"platform": "baidu", "count": len(items), "rising": rising}
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        _record_run(session, user_id, "baidu", "failed", f"{type(exc).__name__}: {exc}")
        session.commit()
        raise


def _baidu_rising(session, user_id: int, settings: Settings, now) -> list[dict]:
    """基于该用户历史百度热搜热度,判定上涨词(与微博一致的双重校验)。"""
    series = repository.baidu_heat_series(session, user_id)
    rising = []
    for title, points in series.items():
        heats = [h for _, h in points]
        if len(heats) < 2:
            continue
        g = compute_growth([float(x) for x in heats])
        sl = compute_slope([float(x) for x in heats])
        if g is not None and sl is not None and g > settings.growth_threshold and sl > 0:
            rising.append({"keyword": title, "growth": g, "slope": sl})
    rising.sort(key=lambda r: r["growth"], reverse=True)
    return rising[:20]


def run_xianyu(session: Session, user_id: int, settings: Settings | None = None) -> dict:
    settings = _base(settings)
    if verify_cooldown_active(session, user_id, settings):  # 验证后冷却:避免反复撞滑块
        _record_run(session, user_id, "xianyu", "skipped", "verify_cooldown")
        session.commit()
        return {"platform": "xianyu", "count": 0, "status": "skipped", "reason": "verify_cooldown"}
    cookies = get_cookies(session, user_id)
    goofish_cookie = cookies.get("goofish", "")
    if not goofish_cookie:
        raise ValueError("未配置闲鱼 Cookie")
    try:
        client = xianyu.XianyuClient(goofish_cookie)
        # 风控降频:每轮只抓 batch 个关键词,按已运行的 xianyu 次数轮转起始窗口,多轮覆盖全部
        prior = session.scalar(select(func.count()).select_from(RunRecord).where(
            RunRecord.user_id == user_id, RunRecord.kind == "xianyu")) or 0
        batch = max(1, int(getattr(settings, "xianyu_batch_keywords", 0) or 5))
        n_kw = len([k.strip() for k in settings.xianyu_keywords.split(",") if k.strip()])
        start_offset = (prior * batch) % max(n_kw, 1)
        hot = xianyu.collect_hot(settings, client, start_offset=start_offset)
        prev_keys = set(session.scalars(select(XianyuItem.item_id).where(XianyuItem.user_id == user_id)).all())
        for it in hot:
            session.add(XianyuItem(user_id=user_id, **it))
        session.commit()
        latest = [{"key": it["item_id"], "hit_keywords": it["hit_keywords"], "best_rank": it["best_rank"]} for it in hot]
        alert_service.evaluate(session, user_id, "xianyu", latest, prev_keys, settings)
        _record_watch(session, user_id, "xianyu", [{"title": it["title"], "value": it.get("hit_keywords", 0)} for it in hot])
        _record_run(session, user_id, "xianyu", "success", f"items={len(hot)}")
        session.commit()
        # 搜索接力深采(想要数/类目)——自动跑,受 验证冷却 + 详情限流 保护;按间隔控制频率防累积风控
        if xianyu_deep_due(session, user_id, settings):
            try:
                run_xianyu_deep(session, user_id, settings, hot=hot)
            except Exception:  # noqa: BLE001 - 深采失败不影响搜索结果(已提交)
                session.rollback()
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
        # (search/video/topic 的监控词走定向查询,拉全榜仅为兜底展示)
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
        _record_douhot_watch_snaps(session, user_id, lists, douyin_cookie, settings)
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
    rows = repository.douhot_words(session, user_id, limit=500)
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
        for r in repository.weibo_rising(session, user_id, limit=20)
    ]
    xianyu_rows = repository.xianyu_items(session, user_id, limit=30)
    douhot_rows = repository.douhot_top_words(session, user_id, limit=100)
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


def run_section_for_all_users(section: str, settings: Settings | None = None) -> dict:
    """为【所有用户】跑某个板块采集(用各人自己的 Cookie)。

    常规定时采集已改为**按每个用户自己设置的频率**执行(见
    `app/services/scheduler.py::collect_tick`);本函数保留给"批量/全员立即采集"
    这类运维场景使用。
    """
    from app.db import get_session_local
    from app.db.models import User

    settings = settings or get_settings()
    runners = {"weibo": run_weibo, "xianyu": run_xianyu, "douhot": run_douhot, "baidu": run_baidu}
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
