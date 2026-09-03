"""热点宝关键词监控 + 智能体预测域(见 doc/dev.md §5.10)。

- 关键词关注(watch)的增/查、采集时记录快照(`_record_douhot_watch_snaps`,内容词走定向查询)
- 智能体分析 `douhot_watch_analytics`(趋势/预测/置信度/爆发)
- 多平台预测 `platform_agent`(微博 heat / 闲鱼 want_count 序列喂给 keyword_agent)
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from config.settings import Settings
from app.db import repository
from app.db.models import DouhotWatch, DouhotWatchSnap
from app.services import douhot
from app.services.trend_analyzer import compute_growth, compute_slope


def add_douhot_watch(session: Session, user_id: int, list_type: str, keyword: str) -> dict:
    list_type = list_type if list_type in ("word", "search", "subscribe", "video", "topic") else "word"
    keyword = keyword.strip()
    if repository.get_watch(session, user_id, list_type, keyword) is None:
        session.add(DouhotWatch(user_id=user_id, list_type=list_type, keyword=keyword))
        session.commit()
    return {"list_type": list_type, "keyword": keyword}


def list_douhot_watch(session: Session, user_id: int) -> list[dict]:
    return [{"list_type": r.list_type, "keyword": r.keyword} for r in repository.list_watches(session, user_id)]


def _record_douhot_watch_snaps(
    session: Session,
    user_id: int,
    lists: dict[str, list[dict]],
    douyin_cookie: str = "",
    settings: Settings | None = None,
) -> None:
    """把用户关注的词,在对应榜单中记录得分与排名快照。

    内容词(word):用**定向查询**取该词自身热度——不再依赖它碰巧在 top100 里,
    这是"任意关键词监控"的核心。其他榜仍从已采集列表里找(词须在榜内才会命中)。
    """
    watches = repository.list_watches(session, user_id)
    for w in watches:
        if w.list_type == "word" and douyin_cookie:
            try:
                heat = douhot.fetch_keyword_heat(douyin_cookie, w.keyword, settings)
                score, rank = heat.get("score", 0), heat.get("rank_now", 0)
            except Exception:  # noqa: BLE001 - 定向查询失败降级为 0,不中断采集
                score, rank = 0, 0
        else:
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
    from app.services import keyword_agent

    watches = repository.list_watches(session, user_id)
    out = []
    for w in watches:
        snaps = repository.watch_snap_series(session, user_id, w.keyword)
        values = [s.score for s in snaps]
        growth = compute_growth(values) if len(values) >= 2 else None
        agent = keyword_agent.analyze(w.keyword, values)
        out.append(
            {
                "keyword": w.keyword,
                "list_type": w.list_type,
                "last_score": values[-1] if values else 0,
                "rank_now": snaps[-1].rank_now if snaps else 0,
                "points": len(values),
                "growth": growth,
                "trend_label": agent["trend_label"],
                "forecast_next": agent["forecast_next"],
                "summary": agent["summary"],
                "series": agent["series"],
                "slope": agent["slope"],
                "confidence": agent["confidence"],
                "r2": agent["r2"],
                "accel": agent["accel"],
                "burst": agent["burst"],
            }
        )
    return out


def platform_view(session: Session, user_id: int, platform: str, top_n: int = 50) -> dict:
    """某板块的独立页数据:最新榜单条目 + 每词智能体趋势/预测。

    platform ∈ weibo/baidu(xianyu/douhot)。返回
    {items:[{name, score, trend_label, growth, forecast_next, burst}], count}.
    """
    from app.services import keyword_agent

    if platform == "weibo":
        series = repository.weibo_heat_series(session, user_id)
        latest = repository.weibo_items(session, user_id, limit=top_n)
        name = lambda r: r.title  # noqa: E731
        val = lambda r: r.heat  # noqa: E731
    elif platform == "baidu":
        series = repository.baidu_heat_series(session, user_id)
        latest = repository.baidu_items(session, user_id, limit=top_n)
        name = lambda r: r.title  # noqa: E731
        val = lambda r: r.heat  # noqa: E731
    elif platform == "douhot":
        series = repository.douhot_score_series(session, user_id)
        latest = repository.douhot_words(session, user_id, limit=top_n)
        name = lambda r: r.title  # noqa: E731
        val = lambda r: r.score  # noqa: E731
    elif platform == "xianyu":
        series = repository.xianyu_want_series(session, user_id)
        latest = repository.xianyu_items(session, user_id, limit=top_n)
        name = lambda r: r.title or r.item_id  # noqa: E731
        val = lambda r: 0  # noqa: E731
    else:
        return {"platform": platform, "count": 0, "items": []}

    out = []
    for r in latest:
        n = name(r)
        pts = series.get(n, [])
        a = keyword_agent.analyze(n, [v for _, v in pts])
        out.append({
            "name": n, "score": val(r),
            "trend_label": a["trend_label"], "growth": a["growth"],
            "forecast_next": a["forecast_next"], "burst": a["burst"], "points": a["points"],
        })
    return {"platform": platform, "count": len(out), "items": out[:top_n]}


def platform_agent(session: Session, user_id: int, top_n: int = 8) -> dict:
    """多平台智能体:把微博热度 / 闲鱼想要数 的历史序列喂给 `keyword_agent`,
    产出各平台的热点趋势 + 预测(与抖音同一套逻辑)。返回 {weibo, xianyu}。

    - weibo:按 微博热搜词 的 heat 序列(多轮采集)判定趋势并预测下一轮热度
    - xianyu:按 商品 的 want_count 序列(每日快照)判定趋势并预测
    """
    from app.services import keyword_agent

    def run(maker: object) -> list[dict]:
        out = []
        for title, series in maker:  # type: ignore[attr-defined]
            if len(series) < 2:
                continue
            a = keyword_agent.analyze(title, [v for _, v in series])
            a["title"] = title
            a["series"] = [v for _, v in series]
            out.append(a)
        out.sort(key=lambda x: (x["burst"], x["forecast_next"] or 0), reverse=True)
        return out[:top_n]

    weibo = run(repository.weibo_heat_series(session, user_id).items())  # type: ignore[arg-type]
    xianyu = run(repository.xianyu_want_series(session, user_id).items())  # type: ignore[arg-type]

    return {"weibo": weibo, "xianyu": xianyu}
