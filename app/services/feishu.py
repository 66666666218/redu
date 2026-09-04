"""飞书群机器人「热点日报 + 实时提醒」(见 doc/dev.md §5.11)。

客户端(签名/发送)在 `app/services/feishu_client.py`;本模块负责**作业编排**:
- `run_feishu_daily()`:按 `FEISHU_DAILY_CRON` 每天一次,推三板块榜单对比 + 趋势分析。
- `run_feishu_realtime(section, user_id)`:采集成功后推"新增/飙升"话题(按冷却去重)。
- `run_feishu_keyword_alerts(...)`:智能体"预测爆发"实时推送(按置信度门槛)。
- `run_feishu_insight_digest()`:每周"近7天爆点回顾"。

未配置 `FEISHU_WEBHOOK` 时全部自动关闭(不影响其他功能)。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from config.settings import Settings, get_settings
from app.db import repository
from app.db.models import BaiduHotItem, DouhotWatchSnap, DouhotWord, FeishuAlert, WeiboHotItem, XianyuItem
from app.services.feishu_client import FeishuClient
from app.utils import get_logger

logger = get_logger(__name__)

SECTIONS = ("weibo", "xianyu", "douhot", "baidu")
SECTION_LABELS = {"weibo": "微博热搜", "xianyu": "闲鱼热榜", "douhot": "抖音热点", "baidu": "百度热搜"}
# 每个板块如何取"标题"与"排名/分值"
_RANK_FIELD = {"weibo": "rank", "xianyu": "best_rank", "douhot": "score", "baidu": "rank"}
# 每板块的历史表(用于上一轮快照对比)
_TABLES = {"weibo": WeiboHotItem, "xianyu": XianyuItem, "douhot": DouhotWord, "baidu": BaiduHotItem}
# 每板块的历史热度序列函数(用于实时预测/周对比)
_SERIES = {
    "weibo": repository.weibo_heat_series,
    "baidu": repository.baidu_heat_series,
    "douhot": repository.douhot_score_series,
    "xianyu": repository.xianyu_want_series,
}
# 每板块的时间列(表字段不一)
_TS_COL = {"weibo": "captured_at", "baidu": "captured_at", "douhot": "created_at", "xianyu": "created_at"}


def _agent_confidence_rank(level: str | None) -> int:
    """置信度等级 → 数值,便于比较。高=3/中=2/低=1;未知视为 0。"""
    return {"高": 3, "中": 2, "低": 1}.get(level or "", 0)


# ---------------------------------------------------------------------------
# 数据读取:取该用户该板块最近两次采集批次
# ---------------------------------------------------------------------------

def _batches(db: Session, user_id: int, section: str) -> tuple[dict[str, object], dict[str, object]]:
    """返回 (当前批 {title: row}, 上一批 {title: row})。

    批次 = 该用户该板块**全局最近一次采集时间点**(当前批)与其**前一个时间点**(上一批)。
    同一标题在不同批各自出现 → 才能做"排名涨跌";只在当前批出现 → 判为新增。
    每批内若同标题出现多次(极少数),取该批最后一次。
    """
    import time as _t

    table = _TABLES[section]
    rows = db.scalars(
        select(table).where(table.user_id == user_id).order_by(table.id.asc())
    ).all()
    if not rows:
        return {}, {}

    def key_of(r) -> str:
        return str(getattr(r, "title", "") or getattr(r, "item_id", "") or "").strip()

    def ts_of(r) -> datetime:
        ts = getattr(r, "captured_at", None) or getattr(r, "created_at")
        return ts if isinstance(ts, datetime) else datetime.min

    # 收集全局所有出现过的采集时间点,取最近两个
    all_ts = {ts_of(r) for r in rows}
    sorted_ts = sorted(all_ts)
    cur_ts = sorted_ts[-1]
    prev_ts = sorted_ts[-2] if len(sorted_ts) >= 2 else None

    def build(ts: datetime) -> dict[str, object]:
        out: dict[str, object] = {}
        for r in rows:
            if ts_of(r) == ts:
                out[key_of(r)] = r  # id 升序遍历,同批内后者覆盖 = 取该批最后一条
        return {k: v for k, v in out.items() if k}

    return build(cur_ts), build(prev_ts) if prev_ts is not None else {}


# ---------------------------------------------------------------------------
# 日报
# ---------------------------------------------------------------------------

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
        trend = a.get("trend_label") or "平稳"
        arrow = "↑" if trend == "上升期" else ("↓" if trend == "回落期" else "→")
        p = prev_map.get(title)
        if p is None:
            marker = "🆕"
        else:
            delta = p.rank_now - s.rank_now  # 正 = 名次更靠前(上升)
            marker = (f"↑{delta}名" if delta > 0 else (f"↓{abs(delta)}名" if delta < 0 else "  "))
        if a.get("burst") and p is not None:
            marker = "🔥" + marker
        rows.append({
            "title": title, "score": s.score, "rank": s.rank_now, "marker": marker,
            "growth": a.get("growth"), "trend": trend, "arrow": arrow,
            "forecast": a.get("forecast_next"),
        })
    rows.sort(key=lambda x: x["rank"])  # 按搜索序(排名)排
    return rows, prev_map, latest_map


def _entry_line(r: dict) -> str:
    g = f"{r['growth'] * 100:+.0f}%" if r["growth"] is not None else "—"
    fc = f" 预测{_w(r['forecast'])}" if r["forecast"] is not None else ""
    return f"  {r['marker']} {r['title'][:22]}  {_w(r['score'])}  {r['arrow']}{r['trend']}  {g}{fc}"


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
        flag = "🔥" if agent.get("burst") else ""
        fc = f" 预测{agent['forecast_next']:.0f}" if agent.get("forecast_next") is not None else ""
        conf = f" {agent['confidence']}" if agent.get("confidence") and agent["confidence"] != "数据不足" else ""
        g = f"环比{'+' if (agent.get('growth') or 0) > 0 else ''}{((agent.get('growth') or 0) * 100 if agent.get('growth') is not None else 0):.0f}%" if values else ""
        lines.append(f"  · {w['keyword']}  {agent['trend_label']}{flag}  {g}{fc}{conf}")
    return lines if len(lines) > 1 else []


def build_keyword_card(db: Session, user_id: int, settings: Settings) -> dict | None:
    """生成"关键词监控"飞书交互卡片(有内容才返回 dict,否则 None)。

    每个关注词一块:标题(关键词 · 板块 · 趋势概览)+ 明细 note(每条 趋势/名次/新增)。
    """
    from app.services import keyword_agent
    from app.services.keyword_watch import list_watch

    entry_top = getattr(settings, "douhot_watch_daily_top", None) or 100
    elements = []
    for w in list_watch(db, user_id):
        snaps = repository.watch_snap_series(db, user_id, w["keyword"], section=w.get("section"))
        label = SECTION_LABELS.get(w.get("section", ""), w.get("section", ""))
        entries = [s for s in snaps if getattr(s, "entry_title", "")]
        if entries:
            rows, prev_map, latest_map = _keyword_entries_rows(db, user_id, w, snaps)
            if not rows:
                continue
            elements.append({"tag": "div", "text": {"tag": "lark_md",
                            "content": f"**{w['keyword']}** · {label} · {_overview(rows)}"}})
            news = sum(1 for r in rows if r["marker"] == "🆕")
            rose = sum(1 for r in rows if r["marker"].startswith("↑") or r["marker"].startswith("🔥↑"))
            fell = sum(1 for r in rows if r["marker"].startswith("↓") or r["marker"].startswith("🔥↓"))
            dropped = len(set(prev_map) - set(latest_map))
            body = "\n".join(_entry_line(r) for r in rows[:entry_top])
            body += f"\n↳ 今日vs昨日:🆕{news} ↑{rose} ↓{fell} 跌出{dropped}"
            elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": body}]})
        else:
            values = [s.score for s in snaps]
            a = keyword_agent.analyze(w["keyword"], values)
            g = f"{a['growth'] * 100:+.0f}%" if a.get("growth") is not None else "—"
            fc = f"  预测{a['forecast_next']:.0f}" if a.get("forecast_next") is not None else ""
            elements.append({"tag": "div", "text": {"tag": "lark_md",
                            "content": f"**{w['keyword']}** · {label} · {a['trend_label']}{'🔥' if a.get('burst') else ''}"}})
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


def build_daily(db: Session, user_id: int, settings: Settings, include_keywords: bool = True) -> str:
    """生成四板块日报文本(供飞书推送与测试)。

    结构:今日活跃对比(各板块上升数)→ 跨板块共同上升(含各板块预测)→ 各板块榜 → 关键词智能体预测。
    `include_keywords=False` 时不含关键词段(改用飞书交互卡片展示)。
    """
    today = datetime.now().strftime("%m-%d")
    lines = [f"📊 热点日报 · {today}", "每个话题标注相对上一轮的涨跌或新增。"]
    tally = _riser_tally(db, user_id)
    if tally:
        lines.append(f"📈 今日活跃对比:{tally}")
    lines += _cross_section_lines(db, user_id, top_n=4)
    for section in SECTIONS:
        lines += _section_lines(db, user_id, section)
    if include_keywords:
        lines += _keyword_watch_lines(db, user_id)
    return "\n".join(lines)


def run_feishu_daily() -> int:
    """每日定时:给所有启用用户生成并推送日报。返回推送次数。"""
    from app.db import get_session_local

    settings = get_settings()
    if not settings.feishu_webhook:
        return 0
    client = FeishuClient(settings.feishu_webhook, settings.feishu_secret)
    db = get_session_local()()
    sent = 0
    try:
        for user in repository.list_enabled_users(db):
            text = build_daily(db, user.id, settings, include_keywords=False)  # 关键词单独用卡片
            for chunk in _split_messages(text):  # 过长自动拆成多条飞书
                if client.send(chunk):
                    sent += 1
            card = build_keyword_card(db, user.id, settings)
            if card and client.send_card(card):
                sent += 1
        logger.info("飞书日报推送完成,消息数=%s", sent)
        return sent
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 实时提醒(采集成功后调用)
# ---------------------------------------------------------------------------

def _in_cooldown(db: Session, user_id: int, section: str, title: str, settings: Settings) -> bool:
    now = datetime.now()
    row = db.scalar(
        select(FeishuAlert).where(
            FeishuAlert.user_id == user_id, FeishuAlert.section == section, FeishuAlert.title == title
        )
    )
    return row is not None and (now - row.alerted_at).total_seconds() < settings.feishu_alert_cooldown_hours * 3600


def _section_weekly_tally(db: Session, now: datetime | None = None) -> list[str]:
    """各板块"本周(近7天) vs 上周(前7天)"活跃话题数对比,用于周报对比总结。跨用户聚合。"""
    now = now or datetime.now()
    since14 = now - timedelta(days=14)
    week_start = since14 + timedelta(days=7)  # = now - 7d
    out = []
    for section in SECTIONS:
        table = _TABLES[section]
        col = getattr(table, _TS_COL[section])
        rows = db.scalars(select(table).where(col >= since14)).all()
        this_week, last_week = set(), set()
        for r in rows:
            ts = getattr(r, _TS_COL[section])
            key = str(getattr(r, "title", "") or getattr(r, "item_id", "") or "").strip()
            if not key:
                continue
            (this_week if ts >= week_start else last_week).add(key)
        if not this_week and not last_week:
            continue
        d = len(this_week) - len(last_week)
        arrow = f"+{d}" if d > 0 else (str(d) if d < 0 else "=")
        out.append(f"{_SHORT[section]} {len(this_week)}↔{len(last_week)}({arrow})")
    return out


def run_feishu_insight_digest(settings: Settings | None = None) -> int:
    """每周一次"近7天爆点回顾"——跨用户聚合,推送到飞书群。

    统计最近 7 天(按快照 captured_at)里关注词的爆发/上升情况,附历史回溯
    (首次上涨/峰值/持续时长)。未配置 webhook 直接跳过。
    """
    settings = settings or get_settings()
    if not settings.feishu_webhook:
        return 0
    from app.services import keyword_agent, tenant
    from app.db import get_session_local
    since = datetime.now() - timedelta(days=7)
    client = FeishuClient(settings.feishu_webhook, settings.feishu_secret)
    db = get_session_local()()
    try:
        lines = ["📅 近 7 天爆点回顾"]
        tally = _section_weekly_tally(db)
        if tally:
            lines.append(f"📊 板块活跃对比(本周↔上周):{' | '.join(tally)}")
        lines.append("")
        burst_rows, rising_rows = [], []
        for user in repository.list_enabled_users(db):
            from app.services.keyword_watch import list_watch
            for w in list_watch(db, user.id):
                snaps = repository.watch_snap_series(db, user.id, w["keyword"], since, section=w.get("section"))
                values = [s.score for s in snaps]
                if len(values) < 2:
                    continue
                agent = keyword_agent.analyze(w["keyword"], values)
                hist = keyword_agent.history(values, [s.captured_at for s in snaps])
                row = {**agent, "duration_hours": hist.get("duration_hours"),
                       "first_rise": hist.get("first_rise"), "peak_value": hist.get("peak_value")}
                (burst_rows if agent["burst"] else rising_rows).append(row)
        if not burst_rows and not rising_rows:
            lines.append("本周暂无爆点/上升词(需先有关注词并积累多轮采集)")
        if burst_rows:
            burst_rows.sort(key=lambda r: r["forecast_next"] or 0, reverse=True)
            lines.append("🔥 爆发词:")
            for r in burst_rows[:12]:
                hrs = f"{r['duration_hours']}h" if r.get("duration_hours") is not None else "—"
                lines.append(f"  · {r['keyword']} 环比+{(r['growth'] or 0)*100:.0f}% "
                             f"预测{int(r['forecast_next'] or 0)} 已涨约{hrs}")
        if rising_rows:
            rising_rows.sort(key=lambda r: r["growth"] or 0, reverse=True)
            lines.append(f"📈 上升词({len(rising_rows)}):")
            lines.append("  " + ", ".join(r["keyword"] for r in rising_rows[:10]))
        return 1 if client.send("\n".join(lines)) else 0
    finally:
        db.close()


def run_feishu_keyword_alerts(user_id: int, settings: Settings | None = None, db: Session | None = None) -> int:
    """智能体预警:把"预测可能爆发"的关注词推送到群里。

    对用户每个关注词,用 `keyword_agent` 做趋势分析;命中爆发信号(上升期 + 强增长 +
    一定置信度 + 正加速度)且不在冷却期内的,推一条"预测爆发"消息。去重表复用 FeishuAlert。
    返回推送条数;`db` 供测试注入。
    """
    settings = settings or get_settings()
    if not settings.feishu_webhook:
        return 0
    from app.services import keyword_agent
    from app.services import tenant
    from app.db import get_session_local

    own_session = db is None
    db = db or get_session_local()()
    pushed = 0
    try:
        from app.services.keyword_watch import list_watch
        watches = list_watch(db, user_id)
        if not watches:
            return 0
        client = FeishuClient(settings.feishu_webhook, settings.feishu_secret)
        hits: list[dict] = []
        for w in watches:
            snaps = repository.watch_snap_series(db, user_id, w["keyword"], section=w.get("section"))
            values = [s.score for s in snaps]
            if not values:
                continue
            agent = keyword_agent.analyze(w["keyword"], values)
            # 置信度分级:只有达到最低置信度的爆发才实时推送,中/低置信只进日报与洞察
            if not agent["burst"]:
                continue
            if agent.get("confidence") not in ("高", "中"):
                continue
            if _agent_confidence_rank(agent.get("confidence")) < _agent_confidence_rank(settings.feishu_burst_min_confidence):
                continue
            if _in_cooldown(db, user_id, "keyword_burst", w["keyword"], settings):
                continue
            _mark_alerted(db, user_id, "keyword_burst", w["keyword"], "预测爆发")
            hits.append(agent)
        if hits:
            lines = ["🔮 智能体预测 · 可能爆发"]
            for a in hits:
                lines.append(f"  · {a['keyword']} 预测 {a['forecast_next']:.0f} 环比+{a['growth']*100:.0f}%")
            client.send("\n".join(lines))
            pushed = len(hits)
            logger.info("飞书智能体预警推送 user=%s 条数=%s", user_id, pushed)
        return pushed
    finally:
        if own_session:
            db.close()


def _mark_alerted(db: Session, user_id: int, section: str, title: str, reason: str) -> None:
    row = db.scalar(
        select(FeishuAlert).where(
            FeishuAlert.user_id == user_id, FeishuAlert.section == section, FeishuAlert.title == title
        )
    )
    if row is None:
        db.add(FeishuAlert(user_id=user_id, section=section, title=title, reason=reason))
    else:
        row.reason, row.alerted_at = reason, datetime.now()
    db.commit()


def run_feishu_realtime(
    section: str, user_id: int, settings: Settings | None = None, db: Session | None = None
) -> int:
    """采集成功后调用:把新增/飙升的话题立即推送到群里,并去重。

    返回此次推送条数(0 = 未配置/无触发)。`db` 供测试注入;缺省用全局会话。
    """
    settings = settings or get_settings()
    if not settings.feishu_webhook:
        return 0
    from app.db import get_session_local

    own_session = db is None
    db = db or get_session_local()()
    pushed = 0
    try:
        cur, prev = _batches(db, user_id, section)
        if not cur:
            return 0
        client = FeishuClient(settings.feishu_webhook, settings.feishu_secret)
        pushed_items: list[tuple[str, str]] = []
        for title, c in list(cur.items())[:40]:
            tag, extra = _delta(section, c, prev)
            reason = None
            if tag == "new":
                reason = "新增"
            elif tag == "up":
                if section == "douhot":
                    pct = float(extra.rstrip("%")) if extra.endswith("%") else 0
                    if pct / 100 >= settings.feishu_hot_ratio:
                        reason = f"飙升+{pct:.0f}%"
                else:
                    up = int(extra.rstrip("名").lstrip("+"))
                    if up >= settings.feishu_hot_rank_jump:
                        reason = str(extra)
            if reason and not _in_cooldown(db, user_id, section, title, settings):
                pushed_items.append((title, reason))
                _mark_alerted(db, user_id, section, title, reason)
        if pushed_items:
            head = f"⚡ {SECTION_LABELS[section]} 实时热点"
            lines = [head]
            # 给每个推送词补一句"预测/置信度"(历史样本 ≥2 才预测)
            from app.services import keyword_agent

            series = _SERIES.get(section, lambda db, uid: {})(db, user_id)
            for title, reason in pushed_items:
                line = f"  · {title[:24]}  {reason}"
                vals = [v for _, v in series.get(title, [])]
                if len(vals) >= 2:
                    a = keyword_agent.analyze(title, vals)
                    if a.get("forecast_next") is not None:
                        line += f"  预测{a['forecast_next']:.0f}"
                    if a.get("confidence") and a["confidence"] != "数据不足":
                        line += f" {a['confidence']}"
                    if a.get("trend_label"):
                        line += f" {a['trend_label']}"
                lines.append(line)
            client.send("\n".join(lines))
            pushed = len(pushed_items)
            logger.info("飞书实时推送 section=%s user=%s 条数=%s", section, user_id, pushed)
        return pushed
    finally:
        if own_session:
            db.close()
