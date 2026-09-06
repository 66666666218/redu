"""热点宝关键词监控 + 智能体预测域(见 doc/dev.md §5.10)。

- 关键词关注(watch)的增/查、采集时记录快照(`_record_douhot_watch_snaps`,
  抖音 内容词/搜索/视频/话题 均走定向查询)
- 智能体分析 `douhot_watch_analytics`(趋势/预测/置信度/爆发)
- 多平台预测 `platform_agent`(微博 heat / 闲鱼 want_count 序列喂给 keyword_agent)
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from config.settings import Settings
from app.db import repository
from app.db.models import DouhotWatch, DouhotWatchSnap
from app.services import douhot
from app.services.trend_analyzer import compute_growth, compute_slope


_WORD_TYPES = ("word", "search", "subscribe", "video", "topic")
_ENTRY_CAP = 100  # 榜单定向搜索类关注,每次采集最多记录的相关主题条数(默认 100,可用 DOUHOT_WATCH_ENTRY_CAP 覆盖)


def add_watch(session: Session, user_id: int, section: str, list_type: str, keyword: str,
              filter_keyword: str = "", date_window: int | str | None = None) -> dict:
    list_type = list_type if list_type in _WORD_TYPES else "word"
    keyword = keyword.strip()
    filter_keyword = (filter_keyword or "").strip()
    dw = (douhot._normalize_date_window(date_window) if date_window is not None
          else douhot._default_window(list_type))
    if repository.get_watch(session, user_id, section, list_type, keyword, filter_keyword) is None:
        session.add(DouhotWatch(user_id=user_id, section=section, list_type=list_type, keyword=keyword,
                                filter_keyword=filter_keyword, date_window=dw))
        session.commit()
    return {"section": section, "list_type": list_type, "keyword": keyword,
            "filter_keyword": filter_keyword, "date_window": dw}


def add_douhot_watch(session: Session, user_id: int, list_type: str, keyword: str,
                     filter_keyword: str = "", date_window: int | str | None = None) -> dict:
    return add_watch(session, user_id, "douhot", list_type, keyword, filter_keyword, date_window)  # 兼容旧调用


def update_watch(session: Session, user_id: int, section: str, list_type: str, keyword: str,
                 filter_keyword: str = "", date_window: int | str | None = None) -> dict:
    """修改一个已关注关键词的**观测时段**(date_window)。找不到该关注时抛 KeyError。

    只更新时段,不动其它字段(关键词/过滤词/板块);`date_window` 为 None 时回落到榜单默认。
    """
    list_type = list_type if list_type in _WORD_TYPES else "word"
    keyword = keyword.strip()
    filter_keyword = (filter_keyword or "").strip()
    w = repository.get_watch(session, user_id, section, list_type, keyword, filter_keyword)
    if w is None:
        raise KeyError("未找到该关注")
    dw = (douhot._normalize_date_window(date_window) if date_window is not None
          else douhot._default_window(list_type))
    w.date_window = dw
    session.commit()
    return {"section": section, "list_type": list_type, "keyword": keyword,
            "filter_keyword": filter_keyword, "date_window": dw}


def list_watch(session: Session, user_id: int, section: str | None = None) -> list[dict]:
    return [
        {"section": r.section, "list_type": r.list_type, "keyword": r.keyword,
         "filter_keyword": getattr(r, "filter_keyword", "") or "",
         "date_window": getattr(r, "date_window", None) or None}
        for r in repository.list_watches(session, user_id, section)
    ]


def remove_watch(session: Session, user_id: int, section: str, list_type: str, keyword: str,
                 filter_keyword: str = "") -> bool:
    """取消一个关键词关注(并删除其历史快照)。返回是否删除。"""
    return repository.delete_watch(session, user_id, section, list_type, keyword, filter_keyword)


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

    douhot 内容词走**定向查询**(单值,榜外也能查);**搜索/视频/话题**榜定向查询后把
    搜出的**每个相关主题各记一条**(`entry_title`=该主题标题),从而逐条追踪趋势;
    订阅榜无 keyword 参数、其余板块从本次采集的 `lists` 里按"标题包含子串"找命中项
    (`lists` 由各 runner 传 `{"word": [{"title","value"}]}`)。
    """
    watches = repository.list_watches(session, user_id, section)
    # 同一次采集的所有快照用同一 captured_at,保证"一次采集=一批",批次分组才可靠
    collect_ts = datetime.now()

    def add_snap(list_type: str, keyword: str, title: str, score: float, rank: int,
                 trend_growth: float = 0) -> None:
        session.add(DouhotWatchSnap(user_id=user_id, section=section, list_type=list_type,
                                    keyword=keyword, entry_title=title, score=score, rank_now=rank,
                                    trend_growth=trend_growth, captured_at=collect_ts))

    for w in watches:
        # douhot 榜单搜索类(搜索/视频/话题):把搜出的每个相关主题各记一条 → 逐条追踪趋势
        if section == "douhot" and w.list_type in ("search", "video", "topic") and cookie:
            cap = int(getattr(settings, "douhot_watch_entry_cap", None) or _ENTRY_CAP)
            try:
                entries = douhot.fetch_keyword_items(cookie, w.list_type, w.keyword, settings, limit=cap,
                                                     filter_keyword=getattr(w, "filter_keyword", "") or "",
                                                     date_window=getattr(w, "date_window", None))
            except Exception:  # noqa: BLE001 - 定向查询失败不中断采集
                entries = []
            for idx, it in enumerate(entries[:cap], start=1):
                add_snap(w.list_type, w.keyword, it["title"], it["score"] or 0, idx,
                         trend_growth=it.get("trend_growth") or 0)
            continue
        # douhot 内容词:按词定向查该词的专属热度(单值)。
        if section == "douhot" and w.list_type == "word" and cookie:
            try:
                heat = douhot.fetch_keyword_heat(cookie, w.keyword, settings)
                add_snap(w.list_type, w.keyword, "", heat.get("score", 0), heat.get("rank_now", 0),
                         trend_growth=heat.get("trend_growth") or 0)
            except Exception:  # noqa: BLE001 - 定向查询失败降级为 0,不中断采集
                add_snap(w.list_type, w.keyword, "", 0, 0)
            continue
        # 其余板块/订阅榜:从本次采集的 lists 里按"标题包含子串"找命中项。
        words = lists.get(w.list_type) or lists.get("word") or []
        score, rank = 0, 0
        kw = w.keyword.strip().lower()
        for i, word in enumerate(words, start=1):
            title = str(word.get("title") or "").strip().lower()
            if kw and kw in title:
                score = word.get("score") or word.get("value") or word.get("heat") or 0
                rank = i
                break
        add_snap(w.list_type, w.keyword, "", score, rank)
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
        # 榜单定向搜索类关注:按 entry_title 逐条分组,每条算独立趋势;关键词卡顶层取最佳条目。
        entries = [s for s in snaps if getattr(s, "entry_title", "")]
        if entries:
            by_entry: dict[str, list] = {}
            for s in entries:
                by_entry.setdefault(s.entry_title, []).append(s)
            entry_agents = []
            for title, es in by_entry.items():
                vals = [e.score for e in es]
                a = keyword_agent.analyze(title, vals)
                tg = getattr(es[-1], "trend_growth", None)  # 用 trends 序列算出的真实趋势,而非相邻分数差
                growth = tg if tg is not None else compute_growth(vals)
                if growth is not None:
                    label = "上升期" if growth > 0.05 else ("回落期" if growth < -0.05 else "平稳")
                else:
                    label = a["trend_label"]
                burst = bool(a["burst"] or (growth is not None and growth >= 1.0))  # 趋势暴涨也算重点
                entry_agents.append({
                    "title": title, "last_score": vals[-1], "rank_now": es[-1].rank_now,
                    "growth": growth, "trend_label": label,
                    "forecast_next": a["forecast_next"], "confidence": a["confidence"],
                    "burst": burst, "points": len(vals), "summary": a["summary"],
                })
            entry_agents.sort(key=lambda x: (x["burst"], x["last_score"]), reverse=True)
            top = entry_agents[0]
            # 概括不要笼统说"该词上升",应点名具体哪个主题在涨/新进(词内含多条主题,趋势各异)
            risers = [e for e in entry_agents if e["trend_label"] == "上升期"]
            new_ones = [e for e in entry_agents if e["points"] == 1]
            bits = [f"追踪{len(entry_agents)}主题"]
            if risers:
                bit_str = "、".join(
                    f"{e['title'][:12]}" + (f"+{e['growth'] * 100:.0f}%" if e.get("growth") is not None else "")
                    for e in risers[:3]
                )
                bits.append("上升:" + bit_str)
            elif new_ones:
                bits.append("新增:" + "、".join(e["title"][:12] for e in new_ones[:3]))
            else:
                bits.append("走势平稳")
            # 卡片标题的"趋势概览":升/降/新增 各多少(比单一"上升期"更明确)
            ups = sum(1 for e in entry_agents if e["trend_label"] == "上升期")
            downs = sum(1 for e in entry_agents if e["trend_label"] == "回落期")
            news = sum(1 for e in entry_agents if e["points"] == 1)
            ov_parts = [f"{len(entry_agents)}主题"]
            if ups:
                ov_parts.append(f"升{ups}")
            if downs:
                ov_parts.append(f"降{downs}")
            if news:
                ov_parts.append(f"新{news}")
            out.append({
                "section": section, "keyword": w.keyword, "list_type": w.list_type,
                "filter_keyword": getattr(w, "filter_keyword", "") or "",
                "date_window": getattr(w, "date_window", None) or None,
                "last_score": top["last_score"], "rank_now": top["rank_now"],
                "points": len(entry_agents), "growth": top["growth"],
                "trend_label": "上升期" if risers else ("平稳" if not news else "平稳"),
                "trend_overview": " · ".join(ov_parts),   # 如 "3主题 · 升1 · 新1"
                "forecast_next": top["forecast_next"],
                "summary": " · ".join(bits), "confidence": top["confidence"],
                "burst": top["burst"], "entries": entry_agents,  # 全部记录的相关主题(前100)
            })
            continue
        values = [s.score for s in snaps]
        agent = keyword_agent.analyze(w.keyword, values)
        tg = getattr(snaps[-1], "trend_growth", None) if snaps else None  # 用 trends 序列算出的真实趋势
        growth = tg if tg else (compute_growth(values) if len(values) >= 2 else None)
        label = agent["trend_label"]
        if growth is not None:
            label = "上升期" if growth > 0.05 else ("回落期" if growth < -0.05 else "平稳")
        out.append(
            {
                "section": section,
                "keyword": w.keyword,
                "list_type": w.list_type,
                "filter_keyword": getattr(w, "filter_keyword", "") or "",
                "date_window": getattr(w, "date_window", None) or None,
                "last_score": values[-1] if values else 0,
                "rank_now": snaps[-1].rank_now if snaps else 0,
                "points": len(values),
                "growth": growth,
                "trend_label": label,
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
    for idx, r in enumerate(latest, start=1):
        n = name(r)
        pts = series.get(n, [])
        a = keyword_agent.analyze(n, [v for _, v in pts])
        out.append({
            "name": n, "score": val(r), "rank": idx,
            "trend_label": a["trend_label"], "growth": a["growth"],
            "forecast_next": a["forecast_next"], "burst": a["burst"], "points": a["points"],
            "hot": idx <= 3,   # 实时大热点:当前榜单前3(不管是否预测爆发)——吃瓜大瓜也能标出
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
