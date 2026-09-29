"""飞书卡片构建与排版(纯函数:表格对齐/markdown 清洗/趋势摘要/各类卡片组装)。"""

from __future__ import annotations

from app.db import repository

from app.db.models import BaiduHotItem, DouhotWatchSnap, DouhotWord, FeishuAlert, WeiboHotItem, XianyuItem

from app.services import douhot

from config.settings import Settings, get_settings

from datetime import datetime, timedelta

from sqlalchemy import select

from sqlalchemy.orm import Session

import unicodedata

from app.utils import get_logger
logger = get_logger(__name__)
import app.services.feishu as _pkg  # 兼容 monkeypatch:FeishuClient 等经包命名空间运行时查找


SECTIONS = ("weibo", "xianyu", "douhot", "baidu")
SECTION_LABELS = {"weibo": "微博热搜", "xianyu": "闲鱼热榜", "douhot": "抖音热点", "baidu": "百度热搜"}
_RANK_FIELD = {"weibo": "rank", "xianyu": "best_rank", "douhot": "score", "baidu": "rank"}
_TABLES = {"weibo": WeiboHotItem, "xianyu": XianyuItem, "douhot": DouhotWord, "baidu": BaiduHotItem}
_SERIES = {
    "weibo": repository.weibo_heat_series,
    "baidu": repository.baidu_heat_series,
    "douhot": repository.douhot_score_series,
    "xianyu": repository.xianyu_want_series,
}
_TS_COL = {"weibo": "captured_at", "baidu": "captured_at", "douhot": "created_at", "xianyu": "created_at"}
def _agent_confidence_rank(level: str | None) -> int:
    """置信度等级 → 数值,便于比较。高=3/中=2/低=1;未知视为 0。"""
    return {"高": 3, "中": 2, "低": 1}.get(level or "", 0)
def mask_own(text: str, settings: Settings | None = None) -> str:
    """飞书推送脱敏:自营号名一律替换为「内部号」(2026-09-29 用户要求)。

    名单 = settings.own_account_names(逗号分隔)。原则:推到群里的是给
    员工/协作方看的热点情报,自营身份(自己在运营哪个号)绝不能出现。
    无名单/空文本原样返回。
    """
    settings = settings or get_settings()
    names = [n.strip() for n in (getattr(settings, "own_account_names", "") or "").split(",") if n.strip()]
    for n in names:
        text = text.replace(n, "内部号")
    return text
def _display_width(s: str) -> int:
    """字符串显示宽度:中文/全角/歧义(A)/emoji 记 2,ASCII 记 1(飞书字体下 2:1 等宽)。

    `A`(歧义,如 — 破折号)在中文排版中按全角渲染,故记 2——否则含破折号的行会差 1 单位。
    """
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F", "A") else 1 for c in s)
def _pad_cell(s: str, width: int) -> str:
    """把单元格左对齐补齐到指定显示宽度,不足用**全角空格 U+3000** 填充。

    飞书聊天字体下中文/全角=2 个半角宽,全角空格正好补 2 的倍数的差,从而让
    同一列的字符串左端真正对齐;余数为奇数时末尾补 1 个半角空格。
    """
    gap = width - _display_width(s)
    if gap <= 0:
        return s
    return s + "　" * (gap // 2) + (" " if gap % 2 else "")
def _aligned_row(prefix: str, cols: list[tuple[str, int]]) -> str:
    """按 (文本, 列宽) 左对齐拼接成一行,`prefix` 置于行首(不作列对齐)。"""
    return prefix + "".join(_pad_cell(t, w) for t, w in cols)
def _col_set_row(cells: list[tuple[str, int]], *, grey: bool = False) -> dict:
    """飞书卡片 column_set 行:cells = [(lark_md 文本, 权重)]。

    同一组权重连成多行 → 客户端按网格对齐,与字体宽窄无关(lark_md 是比例字体,
    空格补齐永远对不齐——闲鱼推送"乱"的根因,这是正解)。
    列内元素用 **div/lark_md**(自定义机器人老 schema 兼容);
    不要用 `{"tag":"markdown"}`——那是卡片 schema 2.0 元素,自定义 bot.webhook 不渲染,会变空白。
    """
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "background_style": "grey" if grey else "default",
        "columns": [
            {"tag": "column", "width": "weighted", "weight": weight,
             "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": text}}]}
            for text, weight in cells
        ],
    }
def _md_safe(text: str) -> str:
    """标题/关键词进 lark_md 前的清洗。

    lark_md 支持 `[文字](链接)`、`<at user_id="all">`、行内 `` `code` ``;
    任何用户可控字段(公众号昵称、监控关键词、筛选词、商品标题…)原样拼进去
    就是一个跨用户注入面——总群推送里能 @所有人、塞钓鱼链接。
    - `[ ]` → 全角,破坏链接结构;
    - `< >` → 全角,禁 `<at>` 与内联标签;
    - 反引号 → 移除,防 code span 逃逸;
    - 换行 → 空格,保证一条一行。
    """
    return ((text or "")
            .replace("[", "【").replace("]", "】")
            .replace("<", "＜").replace(">", "＞")
            .replace("`", "")
            .replace("\n", " ").replace("\r", " "))
def _batches(db: Session, user_id: int, section: str) -> tuple[dict[str, object], dict[str, object]]:
    """返回 (当前批 {title: row}, 上一批 {title: row})。

    批次 = 该用户该板块**全局最近一次采集时间点**(当前批)与其**前一个时间点**(上一批)。
    同一标题在不同批各自出现 → 才能做"排名涨跌";只在当前批出现 → 判为新增。
    每批内若同标题出现多次(极少数),取该批最后一次。
    """
    table = _TABLES[section]
    # 只取最近两个采集时间点 + 这两批的行,不再整表加载
    # (旧库 20 万行/用户时,每次采集成功后的实时推送都会全量扫一遍,线性劣化)
    ts_col = getattr(table, "captured_at", None) or getattr(table, "created_at")
    recent_ts = db.execute(
        select(ts_col).where(table.user_id == user_id)
        .distinct().order_by(ts_col.desc()).limit(2)
    ).scalars().all()
    if not recent_ts:
        return {}, {}
    cur_ts = recent_ts[0]
    prev_ts = recent_ts[1] if len(recent_ts) >= 2 else None

    def key_of(r) -> str:
        return str(getattr(r, "title", "") or getattr(r, "item_id", "") or "").strip()

    def ts_of(r) -> datetime:
        ts = getattr(r, "captured_at", None) or getattr(r, "created_at")
        return ts if isinstance(ts, datetime) else datetime.min

    def build(ts: datetime) -> dict[str, object]:
        rows = db.scalars(
            select(table).where(table.user_id == user_id, ts_col == ts).order_by(table.id.asc())
        ).all()
        out: dict[str, object] = {}
        for r in rows:
            out[key_of(r)] = r  # id 升序遍历,同批内后者覆盖 = 取该批最后一条
        return {k: v for k, v in out.items() if k}

    return build(cur_ts), build(prev_ts) if prev_ts is not None else {}
def _delta(section: str, cur: object, prev_by_title: dict[str, object]) -> tuple[str, str]:
    """返回 (shift_tag, extra):排名/分值相对上一轮的变化标签与补充说明。"""
    title = str(getattr(cur, "title", "") or getattr(cur, "item_id", "")).strip()
    prev = prev_by_title.get(title)
    if prev is None:
        return "new", ""

    if section == "douhot":
        cv = float(getattr(cur, "score", 0) or 0)
        pv = float(getattr(prev, "score", 0) or 0)
        if pv > 0 and cv >= pv * 1.05:
            return "up", f"+{(cv - pv) / pv * 100:.0f}%"
        if pv > 0 and cv <= pv * 0.95:
            return "down", f"-{(pv - cv) / pv * 100:.0f}%"
        return "stay", ""

    cr = int(getattr(cur, _RANK_FIELD[section], 0) or 0)
    pr = int(getattr(prev, _RANK_FIELD[section], 0) or 0)
    delta = pr - cr  # 名次从 pr 变到 cr;变正 = 上升(名次更小)
    if delta > 0:
        return "up", f"+{delta}名"
    if delta < 0:
        return "down", f"-{abs(delta)}名"
    return "stay", ""
def _trend_summary(section: str, cur: dict[str, object], prev: dict[str, object], n: int = 5) -> str:
    """分析一段话:上升最多 / 新增 / 回落最多,用于日报的"整体趋势"。"""
    up, down, new = [], [], []
    for title, c in list(cur.items())[:60]:
        tag, extra = _delta(section, c, prev)
        if tag == "up":
            up.append((title, extra))
        elif tag == "down":
            down.append((title, extra))
        elif tag == "new":
            new.append(title)
    parts = []
    if up:
        parts.append("上升:" + "; ".join(f"{t}({e})" for t, e in up[:n]))
    if new:
        parts.append("新增:" + ", ".join(new[:n]))
    if down:
        parts.append("回落:" + "; ".join(f"{t}({e})" for t, e in down[:n]))
    return " | ".join(parts) if parts else "整体平稳"
def _section_lines(db: Session, user_id: int, section: str, top_n: int = 10) -> list[str]:
    """某板块日报:每话题带排名变化或新增标签,末尾附一段趋势分析。"""
    cur, prev = _batches(db, user_id, section)
    if not cur:
        return [f"  · {SECTION_LABELS[section]}:暂无数据"]

    # douhot 无 rank 列,按 score 逆序得到名次顺序,用于显示与排序
    order_titles = sorted(cur.keys(), key=lambda t: -float(getattr(cur[t], "score", 0) or 0))
    ranked = order_titles[:top_n]
    lines = [f"【{SECTION_LABELS[section]}】"]
    for title in ranked:
        c = cur[title]
        tag, extra = _delta(section, c, prev)
        if tag == "new":
            badge = "✅新增"
        elif tag == "up":
            badge = f"🔥{extra}"
        elif tag == "down":
            badge = f"📉{extra}"
        else:
            badge = "➖持平"
        lines.append(f"  · {title[:24]}  {badge}")
    lines.append(f"  分析:{_trend_summary(section, cur, prev)}")
    return lines
def _w(v: float | None) -> str:
    """热度数值格式化:≥1万 转"万",否则原样(取整)。"""
    if v is None:
        return "—"
    v = float(v)
    return f"{v / 1e4:.0f}万" if abs(v) >= 10000 else f"{v:.0f}"
def _dw(s: str) -> int:
    """字符串的显示宽度:中文/全角算 2,其余算 1(用于列对齐)。"""
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)
def _pad(s: str, width: int) -> str:
    """把字符串左对齐,补齐空格到指定显示宽度;超宽则按显示宽度截断加"…"(总宽=width)。"""
    s = str(s)
    if _dw(s) > width:
        out = ""
        for c in s:
            if _dw(out) + _dw(c) > width - 1:
                break
            out += c
        return out + " " * max(0, (width - 1) - _dw(out)) + "…"
    return s + " " * max(0, width - _dw(s))
def _rjust(s: str, width: int) -> str:
    """把字符串右对齐,补齐空格到指定显示宽度。"""
    s = str(s)
    return " " * max(0, width - _dw(s)) + s
_SHORT = {"weibo": "微博", "xianyu": "闲鱼", "douhot": "抖音", "baidu": "百度"}
def _riser_tally(db: Session, user_id: int) -> str:
    """今日各板块"上升/新增"话题数,用于跨板块活跃度对比。"""
    parts = []
    for section in SECTIONS:
        cur, prev = _batches(db, user_id, section)
        if not cur:
            continue
        up = sum(1 for t in cur if _delta(section, cur[t], prev)[0] in ("up", "new"))
        parts.append(f"{_SHORT.get(section, SECTION_LABELS[section])}↑{up}")
    return " | ".join(parts)
def _cross_section_lines(db: Session, user_id: int, top_n: int = 4) -> list[str]:
    """今日跨板块(≥2板块)同处上升的关键词 + 各板块预测 → 对比总结。"""
    from app.services.cross_platform import rising_across

    items = rising_across(db, user_id, min_platforms=2)
    if not items:
        return ["  · 暂无跨板块同时上升词"]
    lines = ["🔥 跨板块共同上升(≥2板块)"]
    for it in items[:top_n]:
        tag = " 💥" if it.get("burst") else ""
        plats = "+".join(_SHORT.get(p, p) for p in it["platforms"])
        fc = ",".join(
            f"{_SHORT.get(p, p)}:{_w(f)}" for p, f in it["forecasts"].items() if f is not None
        )
        lines.append(f"  · {it['keyword']}{tag}  [{plats}]  预测→{fc}")
    return lines
def _keyword_entries_rows(db: Session, user_id: int, w: dict, snaps: list) -> tuple[list[dict], dict, dict]:
    """榜单搜索类关注:返回 (最新批条目 rows, prev_map, latest_map)。

    按采集批次分组(captured_at 取整到秒,一次采集的多条快照归为一批),取最新批 vs 上一批:
    - 🆕 最新批有、上批无 = 新进;↑N名/↓N名 = 名次相对上批变化;
    - 每条带 趋势(↑上升期/↓回落期/→平稳) + 环比 + 预测。
    """
    from app.services import keyword_agent
    from collections import defaultdict

    batches: dict = defaultdict(list)
    for s in snaps:
        if getattr(s, "entry_title", ""):
            batches[s.captured_at.replace(microsecond=0)].append(s)
    if not batches:
        return [], {}, {}
    ts = sorted(batches)
    latest_ts, prev_ts = ts[-1], (ts[-2] if len(ts) >= 2 else None)
    latest, prev = batches[latest_ts], (batches[prev_ts] if prev_ts is not None else [])

    def last_of(blist: list) -> dict:
        m: dict = {}
        for s in blist:
            if s.entry_title:
                m[s.entry_title] = s
        return m

    latest_map, prev_map = last_of(latest), last_of(prev)
    rows = []
    for title, s in latest_map.items():
        es = sorted([x for x in snaps if x.entry_title == title], key=lambda x: x.id)
        vals = [e.score for e in es]
        a = keyword_agent.analyze(title, vals)
        tg = getattr(s, "trend_growth", None)  # 用 trends 序列算出的真实趋势
        growth = tg if tg is not None else a.get("growth")
        if growth is not None:
            trend = "上升期" if growth > 0.05 else ("回落期" if growth < -0.05 else "平稳")
        else:
            trend = a.get("trend_label") or "平稳"
        arrow = "↑" if trend == "上升期" else ("↓" if trend == "回落期" else "→")
        p = prev_map.get(title)
        if p is None:
            marker = "🆕"
        else:
            delta = p.rank_now - s.rank_now  # 正 = 名次更靠前(上升)
            marker = (f"↑{delta}名" if delta > 0 else (f"↓{abs(delta)}名" if delta < 0 else "  "))
        # 重点/吃瓜信号:预测爆发 或 趋势暴涨(trend_growth≥100%)——一次采集即可凸显,不用等3轮
        burst = bool(a.get("burst") or (growth is not None and growth >= 1.0))
        if burst and p is not None:
            marker = "🔥" + marker
        rows.append({
            "title": title, "score": s.score, "rank": s.rank_now, "marker": marker,
            "growth": growth, "trend": trend, "arrow": arrow,
            "forecast": a.get("forecast_next"), "burst": burst,
        })
    rows.sort(key=lambda x: x["rank"])  # 按搜索序(排名)排
    return rows, prev_map, latest_map
def _entry_line(r: dict) -> str:
    g = f"{r['growth'] * 100:+.0f}%" if r["growth"] is not None else "—"
    fc = f"  预测{_w(r['forecast'])}" if r["forecast"] is not None else ""
    flag = "🔴重点 " if r.get("burst") else ""
    # 表格形式:名称丨热度值丨趋势(显示宽度对齐:中文算2)
    row = f"{_pad(r['title'][:12], 18)}丨{_rjust(_w(r['score']), 8)}丨{r['arrow']}{r['trend']} {g}{fc}"
    return f"  {r['marker']} {flag}{row}"
def _overview(rows: list[dict]) -> str:
    ups = sum(1 for r in rows if r["trend"] == "上升期")
    downs = sum(1 for r in rows if r["trend"] == "回落期")
    news = sum(1 for r in rows if r["marker"] == "🆕")
    parts = [f"{len(rows)}主题"]
    if ups:
        parts.append(f"升{ups}")
    if downs:
        parts.append(f"降{downs}")
    if news:
        parts.append(f"新{news}")
    return " · ".join(parts)
def _keyword_entries_lines(db: Session, user_id: int, w: dict, snaps: list, top_n: int = 100) -> list[str]:
    """榜单搜索类关注 → 日报文本段(每行一条,末尾附今日vs昨日汇总)。"""
    rows, prev_map, latest_map = _keyword_entries_rows(db, user_id, w, snaps)
    if not rows:
        return []
    news = sum(1 for r in rows[:top_n] if r["marker"] == "🆕")
    rose = sum(1 for r in rows[:top_n] if r["marker"].startswith("↑") or r["marker"].startswith("🔥↑"))
    fell = sum(1 for r in rows[:top_n] if r["marker"].startswith("↓") or r["marker"].startswith("🔥↓"))
    dropped = len(set(prev_map) - set(latest_map))
    label = SECTION_LABELS.get(w.get("section", ""), w.get("section", ""))
    lines = [f"  · {w['keyword']}({label} · {_overview(rows)})"]
    lines += [_entry_line(r) for r in rows[:top_n]]
    lines.append(f"      ↳ 今日vs昨日:🆕{news} ↑{rose} ↓{fell} 跌出{dropped}")
    return lines
def _keyword_watch_lines(db: Session, user_id: int, top_n: int = 8) -> list[str]:
    """日报里的"关键词关注"段落(纯文本)。

    单值词(内容词/订阅/其它板块):给趋势/环比/预测/置信度;
    榜单搜索类(话题/搜索/视频,逐条记录):列出当前 Top 相关主题,每条带趋势 + 🆕新增。
    """
    from app.services import keyword_agent
    from app.services.keyword_watch import list_watch
    from config.settings import get_settings

    watches = list_watch(db, user_id)
    if not watches:
        return []
    entry_top = getattr(get_settings(), "douhot_watch_daily_top", None) or 100
    lines = ["【关键词关注 · 智能体】"]
    for w in watches:
        snaps = repository.watch_snap_series(db, user_id, w["keyword"], section=w.get("section"))
        entries = [s for s in snaps if getattr(s, "entry_title", "")]
        if entries:
            lines += _keyword_entries_lines(db, user_id, w, snaps, entry_top)
            continue
        values = [s.score for s in snaps]
        agent = keyword_agent.analyze(w["keyword"], values)
        tg = getattr(snaps[-1], "trend_growth", None) if snaps else None  # 内容词趋势(trends 序列)
        growth = tg if tg else agent.get("growth")
        label = agent.get("trend_label") or "平稳"
        if growth is not None:
            label = "上升期" if growth > 0.05 else ("回落期" if growth < -0.05 else "平稳")
        flag = "🔴重点" if agent.get("burst") else ""
        fc = f" 预测{agent['forecast_next']:.0f}" if agent.get("forecast_next") is not None else ""
        conf = f" {agent['confidence']}" if agent.get("confidence") and agent["confidence"] != "数据不足" else ""
        g = f"环比{'+' if (growth or 0) > 0 else ''}{((growth or 0) * 100 if growth is not None else 0):.0f}%" if values else ""
        lines.append(f"  · {w['keyword']}  {label}{flag}  {g}{fc}{conf}")
    return lines if len(lines) > 1 else []
def build_keyword_card(db: Session, user_id: int, settings: Settings,
                       section: str | None = None) -> dict | None:
    """生成"关键词监控"飞书交互卡片(有内容才返回 dict,否则 None)。

    每个关注词一块:标题(关键词 · 板块 · 趋势概览)+ 明细表(重点/名称/热度值/趋势,
    榜单搜索类带 `↑N名/↓N名` 名次变化)+ 今日vs昨日汇总。
    `section` 非空时只出该板块的词(供推送到板块专属群;None=全部,推总群)。
    """
    from app.services import keyword_agent
    from app.services.keyword_watch import list_watch

    entry_top = getattr(settings, "douhot_watch_daily_top", None) or 100
    elements = []
    for w in list_watch(db, user_id):
        if section and w.get("section") != section:
            continue
        snaps = repository.watch_snap_series(db, user_id, w["keyword"], section=w.get("section"))
        label = SECTION_LABELS.get(w.get("section", ""), w.get("section", ""))
        entries = [s for s in snaps if getattr(s, "entry_title", "")]
        if entries:
            rows, prev_map, latest_map = _keyword_entries_rows(db, user_id, w, snaps)
            if not rows:
                continue
            fk = (w.get('filter_keyword') or '')
            fk_label = f" · 只含「{_md_safe(fk)}」" if fk else ''
            elements.append({"tag": "div", "text": {"tag": "lark_md",
                            "content": f"**{_md_safe(w['keyword'])}**{fk_label} · {label} · {_overview(rows)}"}})
            # 摘要:追踪N主题 · 上升:xx/新增:xx(与仪表盘一致,不笼统说该词上升)
            risers = [r for r in rows if r["trend"] == "上升期" and r.get("growth") is not None]
            new_ones = [r for r in rows if r["marker"] == "🆕"]
            bits = [f"追踪{len(rows)}主题"]
            if risers:
                bits.append("上升:" + "、".join(
                    f"{_md_safe(r['title'])[:10]}+{r['growth'] * 100:.0f}%" for r in risers[:3]))
            elif new_ones:
                bits.append("新增:" + "、".join(_md_safe(r["title"])[:10] for r in new_ones[:3]))
            else:
                bits.append("走势平稳")
            elements.append({"tag": "div", "text": {"tag": "lark_md",
                            "content": " · ".join(bits)}})
            news = sum(1 for r in rows if r["marker"] == "🆕")
            rose = sum(1 for r in rows if r["marker"].startswith("↑") or r["marker"].startswith("🔥↑"))
            fell = sum(1 for r in rows if r["marker"].startswith("↓") or r["marker"].startswith("🔥↓"))
            dropped = len(set(prev_map) - set(latest_map))
            # 左对齐列(全角空格补齐),缺省填 —;名次列显示 ↑N名/↓N名/🆕
            cols = [("重点", 4), ("名次", 8), ("名称", 22), ("热度值", 10), ("趋势", 12)]
            table = [_aligned_row("  ", cols)]
            for r in rows[:entry_top]:
                g = f" {r['growth'] * 100:+.0f}%" if r["growth"] is not None else ""
                mark = "🔴" if r.get("burst") else "—"
                trend = f"{r['arrow']}{r['trend']}{g}".strip()
                table.append(_aligned_row("  ", [(mark, 4), (r["marker"].strip() or "—", 8),
                                                 (_md_safe(r['title'])[:12], 22), (_w(r['score']), 10), (trend, 12)]))
            table.append(_aligned_row("  ", [("", 4), ("", 8), ("今日vs昨日", 22),
                                             (f"🆕{news} ↑{rose} ↓{fell} 跌出{dropped}", 10), ("—", 12)]))
            elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(table)}})
        else:
            values = [s.score for s in snaps]
            a = keyword_agent.analyze(w["keyword"], values)
            g = f"{a['growth'] * 100:+.0f}%" if a.get("growth") is not None else "—"
            fc = f"  预测{a['forecast_next']:.0f}" if a.get("forecast_next") is not None else ""
            elements.append({"tag": "div", "text": {"tag": "lark_md",
                            "content": f"**{_md_safe(w['keyword'])}** · {label} · {a['trend_label']}{'🔴重点' if a.get('burst') else ''}"}})
            elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": f"环比{g}{fc}"}]})
        elements.append({"tag": "hr"})
    if not elements:
        return None
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "blue", "title": {"tag": "plain_text", "content": "🤖 关键词监控 · 智能体"}},
        "elements": elements,
    }
def _split_messages(text: str, max_len: int = 15000) -> list[str]:
    """飞书文本消息有长度上限,按行拆成多条(每条 ≤max_len 字符)。"""
    if len(text) <= max_len:
        return [text]
    lines = text.split("\n")
    msgs: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for line in lines:
        if cur_len + len(line) + 1 > max_len and cur:
            msgs.append("\n".join(cur))
            cur, cur_len = [line], len(line)
        else:
            cur.append(line)
            cur_len += len(line) + 1
    if cur:
        msgs.append("\n".join(cur))
    return msgs
def _daily_cross_lines(db: Session, user_id: int) -> list[str]:
    """日报的**聚合段**(标题+今日活跃对比+跨板块上升),不含具体板块榜。"""
    today = datetime.now().strftime("%m-%d")
    lines = [f"📊 热点日报 · {today}", "每个话题标注相对上一轮的涨跌或新增。"]
    tally = _riser_tally(db, user_id)
    if tally:
        lines.append(f"📈 今日活跃对比:{tally}")
    lines += _cross_section_lines(db, user_id, top_n=4)
    return lines
def _wechat_ops_lines(db, user_id: int) -> list:
    """公众号运营段:近24h 监听/盘链/采样 统计(从文章表聚合,失败不阻塞日报)。"""
    from datetime import datetime, timedelta
    from sqlalchemy import func
    from app.db.models import WechatArticle, WechatBenchmark
    lines = ["📡 公众号运营(近24h)"]
    try:
        since = datetime.now() - timedelta(hours=24)
        new_art = db.scalar(select(func.count()).select_from(WechatArticle).where(
            WechatArticle.user_id == user_id, WechatArticle.created_at >= since)) or 0
        pan_arts = db.scalar(select(func.count()).select_from(WechatArticle).where(
            WechatArticle.user_id == user_id, WechatArticle.pan_types != "",
            WechatArticle.created_at >= since)) or 0
        sampled = db.scalar(select(func.count()).select_from(WechatArticle).where(
            WechatArticle.user_id == user_id, WechatArticle.traffic_at.is_not(None),
            WechatArticle.created_at >= since)) or 0
        bm = db.scalar(select(func.count()).select_from(WechatBenchmark).where(
            WechatBenchmark.user_id == user_id, WechatBenchmark.active.is_(True))) or 0
        lines.append(f"  · 在监对标号 {bm} 个 · 新文章 {new_art} 篇(带盘链 {pan_arts})")
        lines.append(f"  · 已采样阅读 {sampled} 篇")
    except Exception:  # noqa: BLE001 - 统计失败不阻塞日报
        lines.append("  · 统计暂不可用")
    return lines
def build_daily(db: Session, user_id: int, settings: Settings, include_keywords: bool = True) -> str:
    """生成四板块日报文本(供飞书推送与测试)。

    结构:今日活跃对比(各板块上升数)→ 跨板块共同上升(含各板块预测)→ 各板块榜 → 关键词智能体预测。
    `include_keywords=False` 时不含关键词段(改用飞书交互卡片展示)。
    """
    lines = _daily_cross_lines(db, user_id)
    for section in SECTIONS:
        lines += _section_lines(db, user_id, section)
    if include_keywords:
        lines += _keyword_watch_lines(db, user_id)
    lines += _wechat_ops_lines(db, user_id)
    return mask_own("\n".join(lines), settings)
