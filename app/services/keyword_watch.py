"""热点宝关键词监控 + 智能体预测域(见 doc/dev.md §5.10)。

- 关键词关注(watch)的增/查、采集时记录快照(`_record_douhot_watch_snaps`,
  抖音 内容词/搜索/视频/话题 均走定向查询)
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


_WORD_TYPES = ("word", "search", "subscribe", "video", "topic")


def add_watch(session: Session, user_id: int, section: str, list_type: str, keyword: str) -> dict:
    list_type = list_type if list_type in _WORD_TYPES else "word"
    keyword = keyword.strip()
    if repository.get_watch(session, user_id, section, list_type, keyword) is None:
        session.add(DouhotWatch(user_id=user_id, section=section, list_type=list_type, keyword=keyword))
        session.commit()
    return {"section": section, "list_type": list_type, "keyword": keyword}


def add_douhot_watch(session: Session, user_id: int, list_type: str, keyword: str) -> dict:
    return add_watch(session, user_id, "douhot", list_type, keyword)  # 兼容旧调用


def list_watch(session: Session, user_id: int, section: str | None = None) -> list[dict]:
    return [
        {"section": r.section, "list_type": r.list_type, "keyword": r.keyword}
        for r in repository.list_watches(session, user_id, section)
    ]


def remove_watch(session: Session, user_id: int, section: str, list_type: str, keyword: str) -> bool:
    """取消一个关键词关注(并删除其历史快照)。返回是否删除。"""
    return repository.delete_watch(session, user_id, section, list_type, keyword)


def list_douhot_watch(session: Session, user_id: int) -> list[dict]:
    return list_watch(session, user_id, "douhot")  # 兼容旧调用


def record_watch_snaps(
    section: str,
    session: Session,
    user_id: int,
    lists: dict[str, list[dict]],
    cookie: str = "",
    settings: Settings | None = None,
) -> None:
    """把用户关注的词在**本板块**记录得分与排名快照。

    douhot 各榜单(内容词/搜索/视频/话题)走**定向查询**(榜外也能查,与热点宝
    官网输入关键词看到的一致);订阅榜无 keyword 参数、其余板块从本次采集的
    `lists` 里找(词须在榜内才会命中,`lists` 由各 runner 传 `{"word": [{"title","value"}]}`)。
    """
    watches = repository.list_watches(session, user_id, section)
    for w in watches:
        # douhot 且该榜支持 keyword 定向查询 → 不依赖全榜默认数据,榜外词也记录专属热度
        if section == "douhot" and w.list_type in ("word", "search", "video", "topic") and cookie:
            try:
                if w.list_type == "word":
                    heat = douhot.fetch_keyword_heat(cookie, w.keyword, settings)
                else:
                    heat = douhot.fetch_list_keyword_heat(cookie, w.list_type, w.keyword, settings)
                score, rank = heat.get("score", 0), heat.get("rank_now", 0)
            except Exception:  # noqa: BLE001 - 定向查询失败降级为 0,不中断采集
                score, rank = 0, 0
        else:
            words = lists.get(w.list_type) or lists.get("word") or []
            score, rank = 0, 0
            # 子串匹配(大小写不敏感):关键词出现在榜单条目标题里即命中。
            # 专为闲鱼这类**长标题**板块——精确相等几乎命中不了("PS教程" vs
            # "PS零基础教程全套学习"),子串匹配才能记到数据。
            kw = w.keyword.strip().lower()
            for i, word in enumerate(words, start=1):
                title = str(word.get("title") or "").strip().lower()
                if kw and kw in title:
                    score = word.get("score") or word.get("value") or word.get("heat") or 0
                    rank = i
                    break
        session.add(
            DouhotWatchSnap(user_id=user_id, section=section, list_type=w.list_type,
                            keyword=w.keyword, score=score, rank_now=rank)
        )
    session.commit()


def _record_douhot_watch_snaps(session: Session, user_id: int, lists: dict[str, list[dict]],
                               douyin_cookie: str = "", settings: Settings | None = None) -> None:
    record_watch_snaps("douhot", session, user_id, lists, douyin_cookie, settings)  # 兼容旧调用


def watch_analytics(section: str, session: Session, user_id: int) -> list[dict]:
    from app.services import keyword_agent

    watches = repository.list_watches(session, user_id, section)
    out = []
    for w in watches:
        snaps = repository.watch_snap_series(session, user_id, w.keyword, section=section)
        values = [s.score for s in snaps]
        growth = compute_growth(values) if len(values) >= 2 else None
        agent = keyword_agent.analyze(w.keyword, values)
        out.append(
            {
                "section": section,
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


def douhot_watch_analytics(session: Session, user_id: int) -> list[dict]:
    return watch_analytics("douhot", session, user_id)  # 兼容旧调用


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
