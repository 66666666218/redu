"""飞书推送作业:日报/洞察摘要/关键词告警/实时推送(依赖 _cards 与 feishu_client)。"""

from __future__ import annotations

from app.db import repository

from app.db.models import BaiduHotItem, DouhotWatchSnap, DouhotWord, FeishuAlert, WeiboHotItem, XianyuItem

from app.services import douhot

from app.services.feishu_client import FeishuClient, platform_webhook, webhook_for, webhooks_for

from config.settings import Settings, get_settings

from datetime import datetime, timedelta

from sqlalchemy import select

from sqlalchemy.orm import Session

from typing import Any

import time

from app.utils import get_logger
logger = get_logger(__name__)
import app.services.feishu as _pkg  # 兼容 monkeypatch:FeishuClient 等经包命名空间运行时查找
from app.services.feishu._cards import SECTIONS, SECTION_LABELS, _SERIES, _SHORT, _TABLES, _TS_COL, _agent_confidence_rank, _aligned_row, _batches, _col_set_row, _delta, _keyword_entries_rows, _md_safe, _section_lines, _split_messages, _w, build_daily, build_keyword_card, mask_own


def run_feishu_daily(settings: Settings | None = None, db: Session | None = None) -> int:
    """每日定时:给所有启用用户生成并推送日报。返回推送次数。

    分群:各平台专属群只收该平台日报段;总群收聚合段(活跃对比/跨板块上升)+
    未配专属群的平台段 + 关键词交互卡片。未配任何专属群时退化为原"总群一条全量日报"。
    `settings`/`db` 供测试注入。
    """
    from app.db import get_session_local

    settings = settings or get_settings()
    own_session = db is None
    db = db or get_session_local()()
    has_any = settings.feishu_webhook or any(_pkg.webhook_for(settings, s) for s in SECTIONS)
    if not has_any:
        if own_session:
            db.close()
        return 0
    sent = 0
    try:
        for user in repository.list_enabled_users(db):
            # 主群(总群):完整日报(与原逻辑一致)+ 关键词卡 —— 主群"保持不变、不停止"
            if settings.feishu_webhook:
                client = _pkg.FeishuClient(settings.feishu_webhook, settings.feishu_secret)
                text = build_daily(db, user.id, settings, include_keywords=False)
                for chunk in _split_messages(text):
                    time.sleep(1.0)  # 飞书应用级频控(11232)防护:相邻消息 ≥1s
                    if client.send(chunk):
                        sent += 1
                card = build_keyword_card(db, user.id, settings)
                if card and client.send_card(card):
                    sent += 1
            # 各平台专属群:该平台日报段(主群之外的"加料",互不替代)
            for sec in SECTIONS:
                wh = platform_webhook(settings, sec)
                if not wh:
                    continue
                text = "\n".join([f"📊 {SECTION_LABELS[sec]}日报 · {datetime.now().strftime('%m-%d')}"]
                                 + _section_lines(db, user.id, sec))
                c = _pkg.FeishuClient(wh, settings.feishu_secret)
                for chunk in _split_messages(text):
                    time.sleep(1.0)  # 飞书应用级频控防护
                    if c.send(chunk):
                        sent += 1
                # 该板块的关键词监控卡(含名次变化 ↑N名/↓N名)推专属群
                kcard = build_keyword_card(db, user.id, settings, section=sec)
                if kcard and c.send_card(kcard):
                    sent += 1
        logger.info("飞书日报推送完成,消息数=%s", sent)
        return sent
    finally:
        if own_session:
            db.close()
def run_feishu_wechat_analysis(settings: Settings | None = None, db: Session | None = None) -> int:
    """公众号内容选题分析:把分析结论推送到公众号专属群。

    无文章/未配公众号群则跳过(不推"暂无文章");`settings`/`db` 供测试注入。
    """
    from app.db import get_session_local
    from app.db.models import WechatArticle
    from app.services.wechat_analyzer import analyze_articles

    settings = settings or get_settings()
    wh = _pkg.webhook_for(settings, "wechat")
    if not wh:
        return 0
    own_session = db is None
    db = db or get_session_local()()
    sent = 0
    try:
        client = _pkg.FeishuClient(wh, settings.feishu_secret)
        for user in repository.list_enabled_users(db):
            rows = db.scalars(
                select(WechatArticle).where(WechatArticle.user_id == user.id)
                .order_by(WechatArticle.publish_at.desc().nulls_last()).limit(200)
            ).all()
            if not rows:
                continue
            articles = [{"title": r.title, "content": r.content, "author": r.author, "publish_at": r.publish_at} for r in rows]
            report = analyze_articles(articles)
            if not report["count"]:
                continue
            # 自营号脱敏:分析结论(含"最活跃对标号"点名)推群前把自营号名替换掉
            lines = [f"📊 公众号 · 内容选题分析(近 {report['count']} 篇)",
                     mask_own(report["summary"], settings)]
            lines += [f"  · {mask_own(s, settings)}" for s in report["suggestions"]]
            if client.send("\n".join(lines)):
                sent += 1
        logger.info("公众号分析推送完成,条数=%s", sent)
        return sent
    finally:
        if own_session:
            db.close()
def _in_cooldown(db: Session, user_id: int, section: str, title: str, settings: Settings) -> bool:
    now = datetime.now()
    title = title[:200]  # 与 gate/mark 的截断口径一致
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
        # 只取 (title, item_id, ts) 三列,不加载 ORM 实体:旧写法 `db.scalars(select(table)).all()`
        # 会把 4 张表 14 天跨用户全部行拉成 Python 对象,weibo/douhot 高频档下单次
        # 周报可达数十万实体 → 内存峰值失控(同 _latest_batch 类修复的漏网路径)。
        title_col = getattr(table, "title", None)
        item_id_col = getattr(table, "item_id", None)
        cols = [col] + [c for c in (title_col, item_id_col) if c is not None]
        rows = db.execute(select(*cols).where(col >= since14)).all()
        this_week, last_week = set(), set()
        for row in rows:
            ts = row[0]
            key = (str(row[1] or "") if title_col is not None else "") \
                or (str(row[2 if title_col is not None else 1] or "") if item_id_col is not None else "")
            key = key.strip()
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
    client = _pkg.FeishuClient(settings.feishu_webhook, settings.feishu_secret)
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
                snaps = repository.watch_snap_series(db, user.id, w["keyword"], since,
                                                     section=w.get("section"), list_type=w.get("list_type"))
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
            lines.append("🔴重点 爆发词:")
            for r in burst_rows[:12]:
                hrs = f"{r['duration_hours']}h" if r.get("duration_hours") is not None else "—"
                lines.append(f"  · 🔴{r['keyword']} 环比+{(r['growth'] or 0)*100:.0f}% "
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
    if not _pkg.webhook_for(settings, "douhot"):
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
        client = _pkg.FeishuClient(_pkg.webhook_for(settings, "douhot"), settings.feishu_secret)
        hits: list[dict] = []
        for w in watches:
            snaps = repository.watch_snap_series(db, user_id, w["keyword"], section=w.get("section"),
                                                 list_type=w.get("list_type"))
            entries = [s for s in snaps if getattr(s, "entry_title", "")]
            if entries:
                # 逐条类(话题/搜索/视频):每个相关主题独立判爆发,推具体哪个主题,而非整个词聚合
                by_entry: dict[str, list] = {}
                for s in entries:
                    by_entry.setdefault(s.entry_title, []).append(s)
                for title, es in by_entry.items():
                    vals = [e.score for e in es]
                    if not vals:
                        continue
                    a = keyword_agent.analyze(title, vals)
                    tg = getattr(es[-1], "trend_growth", None)
                    boom = a["burst"] or (tg is not None and tg >= 1.0)  # 预测爆发 或 趋势暴涨
                    if not boom:
                        continue
                    if not (tg is not None and tg >= 1.0):  # 趋势暴涨客观爆发,免置信度门槛
                        if a.get("confidence") not in ("高", "中"):
                            continue
                        if _agent_confidence_rank(a.get("confidence")) < _agent_confidence_rank(settings.feishu_burst_min_confidence):
                            continue
                    if _in_cooldown(db, user_id, "keyword_burst", title, settings):
                        continue
                    hits.append({"keyword": title, "forecast_next": a.get("forecast_next"),
                                 "growth": tg if tg is not None else a.get("growth")})
            else:
                values = [s.score for s in snaps]
                if not values:
                    continue
                agent = keyword_agent.analyze(w["keyword"], values)
                if not agent["burst"]:
                    continue
                if agent.get("confidence") not in ("高", "中"):
                    continue
                if _agent_confidence_rank(agent.get("confidence")) < _agent_confidence_rank(settings.feishu_burst_min_confidence):
                    continue
                if _in_cooldown(db, user_id, "keyword_burst", w["keyword"], settings):
                    continue
                hits.append(agent)
        if hits:
            lines = ["🔮 智能体预测 · 可能爆发"]
            for a in hits:
                fc = f"预测 {a['forecast_next']:.0f} " if a.get("forecast_next") is not None else ""
                lines.append(f"  · 🔴重点 {a['keyword']} {fc}环比+{(a['growth'] or 0) * 100:.0f}%")
            if client.send("\n".join(lines)):
                for a in hits:  # 发送成功才落冷却,失败下次还能再推
                    _mark_alerted(db, user_id, "keyword_burst", a["keyword"], "预测爆发")
                pushed = len(hits)
            logger.info("飞书智能体预警推送 user=%s 条数=%s", user_id, pushed)
        return pushed
    finally:
        if own_session:
            db.close()
def _mark_alerted(db: Session, user_id: int, section: str, title: str, reason: str) -> None:
    title = title[:200]  # 与 feishu_alert_gate 的 title[:200] 截断一致,否则冷却查不到
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
    if not _pkg.webhook_for(settings, section):   # 该板块有专属群或总群,否则不发
        return 0
    from app.db import get_session_local

    own_session = db is None
    db = db or get_session_local()()
    pushed = 0
    try:
        cur, prev = _batches(db, user_id, section)
        if not cur:
            return 0
        client = None
        whs = webhooks_for(settings, section)   # 主群 + 该板块专属群
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
                pushed_items.append((title, reason))  # 冷却标记移到发送成功后(发送失败不烧冷却)
        if pushed_items:
            head = f"⚡ {SECTION_LABELS[section]} 实时热点"
            if section == "xianyu":
                # 闲鱼专属:column_set 网格列(标题可点开商品页;🔥=想要数较昨日 +50% 以上)
                today = datetime.now().date().isoformat()
                yday = (datetime.now() - timedelta(days=1)).date().isoformat()
                t_daily = {str(d.item_id): d for d in repository.xianyu_daily_by_date(db, user_id, today)}
                y_daily = {str(d.item_id): d for d in repository.xianyu_daily_by_date(db, user_id, yday)}
                elements: list[dict[str, Any]] = [
                    {"tag": "note", "elements": [{"tag": "plain_text",
                     "content": "触发:新增 / 排名跳升 · 🔥=想要数较昨日 +50% 以上 · 点标题看商品"}]},
                    _col_set_row([("**标题**", 6), ("**价格**", 2), ("**想要数**", 2), ("**变化**", 2)], grey=True),
                ]
                for title, reason in pushed_items:
                    item = cur.get(title)
                    iid = str(getattr(item, "item_id", "")) if item else ""
                    d, y = t_daily.get(iid), y_daily.get(iid)
                    want = d.want_count if d else None
                    price = (getattr(item, "price", "") or (d.price if d else "") or "")[:12] or "—"
                    safe = _md_safe(title)
                    name = safe[:22] + ("…" if len(safe) > 22 else "")
                    if want is not None and y is not None and getattr(y, "want_count", 0):
                        pct = (want - y.want_count) / y.want_count * 100
                        want_txt = f"{want} ({pct:+.0f}%)"
                        hot = pct >= 50
                    else:
                        want_txt = str(want) if want is not None else "—"
                        hot = False
                    name_md = f"**{name}**" if hot else name
                    if iid:
                        name_md = f"[{name_md}](https://www.goofish.com/item?id={iid})"
                    elements.append(_col_set_row([
                        (f"{'🔥' if hot else ''}{name_md}", 6), (price, 2),
                        (want_txt, 2), (reason, 2),
                    ]))
                card = {"config": {"wide_screen_mode": True},
                        "header": {"template": "blue", "title": {"tag": "plain_text", "content": head}},
                        "elements": elements}
                # 主群 + 专属群都要收到(互不替代):逐个 webhook 都发,不因先成功而短路
                sent_ok = False
                for wh in whs:
                    if _pkg.FeishuClient(wh, settings.feishu_secret).send_card(card):
                        sent_ok = True
                if sent_ok:
                    for t, rsn in pushed_items:
                        _mark_alerted(db, user_id, section, t, rsn)  # 发送成功才落冷却
                    pushed = len(pushed_items)
            else:
                # 通用板块:column_set 网格列(与字体无关,永远对齐);带智能体预测/置信度/趋势
                from app.services import keyword_agent

                series = _SERIES.get(section, lambda db, uid: {})(db, user_id)
                elements: list[dict[str, Any]] = [
                    {"tag": "note", "elements": [{"tag": "plain_text",
                     "content": "触发:新增 / 排名跳升 ≥3 / 涨幅 ≥30%"}]},
                    _col_set_row([("**名称**", 6), ("**变化**", 2), ("**预测**", 2),
                                  ("**置信**", 1), ("**趋势**", 1)], grey=True),
                ]
                for title, reason in pushed_items:
                    vals = [v for _, v in series.get(title, [])]
                    a = keyword_agent.analyze(title, vals) if len(vals) >= 2 else {}
                    fc = f"预测{a['forecast_next']:.0f}" if a.get("forecast_next") is not None else "—"
                    conf = a.get("confidence") if a.get("confidence") and a["confidence"] != "数据不足" else "—"
                    trend = a.get("trend_label") or "—"
                    elements.append(_col_set_row([
                        (_md_safe(title)[:20], 6), (reason, 2), (fc, 2), (conf, 1), (trend, 1)]))
                card = {"config": {"wide_screen_mode": True},
                        "header": {"template": "blue", "title": {"tag": "plain_text", "content": head}},
                        "elements": elements}
                sent_ok = False
                for wh in whs:
                    if _pkg.FeishuClient(wh, settings.feishu_secret).send_card(card):
                        sent_ok = True
                if sent_ok:
                    for t, rsn in pushed_items:
                        _mark_alerted(db, user_id, section, t, rsn)
                    pushed = len(pushed_items)
            logger.info("飞书实时推送 section=%s user=%s 条数=%s", section, user_id, pushed)
        return pushed
    finally:
        if own_session:
            db.close()
def run_feishu_keyword_realtime(user_id: int, settings: Settings | None = None, db: Session | None = None) -> int:
    """话题词(榜单搜索类)实时提醒:检测到 新进/上升/预测爆发 主题即推一条(冷却去重,不刷屏)。

    与每日日报(08:00 全量 Top100)互补——这里只推"有变化"的个别主题,让你及时看到话题词波动:
    - 🆕 新进(最新批有、上批无);↑ 上升期(趋势转为上升);🔥 预测爆发。
    """
    settings = settings or get_settings()
    if not _pkg.webhook_for(settings, "douhot"):
        return 0
    from app.services.keyword_watch import list_watch
    from app.db import get_session_local

    own_session = db is None
    db = db or get_session_local()()
    pushed = 0
    try:
        client = _pkg.FeishuClient(_pkg.webhook_for(settings, "douhot"), settings.feishu_secret)
        for w in list_watch(db, user_id):
            if w.get("section") != "douhot" or w.get("list_type") not in ("search", "video", "topic"):
                continue
            snaps = repository.watch_snap_series(db, user_id, w["keyword"], section="douhot")
            if not any(getattr(s, "entry_title", "") for s in snaps):
                continue
            rows, prev_map, _ = _keyword_entries_rows(db, user_id, w, snaps)
            if not rows:
                continue
            # 汇总有变化的主题(去重:一个主题只显示一次,标最优先信号 🔴重点>↑上升>🆕新进)
            prio = {"重点": 0, "上升": 1, "新进": 2}

            def sig_of(r: dict) -> tuple[int, str] | None:
                if r.get("burst"):
                    g = f"+{r['growth'] * 100:.0f}%" if r.get("growth") is not None and r["growth"] > 0 else ""
                    return (prio["重点"], f"🔴重点 {g}".rstrip())
                if r["trend"] == "上升期" and r.get("growth") is not None and r["growth"] > 0:
                    return (prio["上升"], f"↑上升 +{r['growth'] * 100:.0f}%")
                if r["marker"] == "🆕":
                    return (prio["新进"], "🆕新进")
                return None

            changed: dict[str, tuple[int, str, dict]] = {}
            for r in rows:
                s = sig_of(r)
                if s:
                    changed[r["title"]] = (s[0], s[1], r)
            if not changed:
                continue
            if _in_cooldown(db, user_id, "kw_realtime", w["keyword"], settings):
                continue
            items = sorted(changed.values(), key=lambda x: (x[0], -x[2]["score"]))[:12]
            fk = w.get("filter_keyword") or ""
            fks = (f"·只含「{fk}」" if fk else "")
            dw = w.get("date_window") or douhot._default_window(w["list_type"])
            # 左对齐列(全角空格补齐),缺省填 —
            cols = [("名称", 28), ("热度值", 12), ("变化", 12)]
            table = [_aligned_row("  ", cols)]
            for p, sig, r in items:
                table.append(_aligned_row("  ", [(r['title'][:12], 28), (_w(r['score']), 12), (sig, 12)]))
            card = {
                "config": {"wide_screen_mode": True},
                "header": {"template": "blue",
                           "title": {"tag": "plain_text",
                                     "content": f"📌 话题词监控 · {w['keyword']}({douhot._window_label(dw)}){fks}"}},
                "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(table)}}],
            }
            if client.send_card(card):
                _mark_alerted(db, user_id, "kw_realtime", w["keyword"], "话题词变化")
                pushed += 1
        return pushed
    finally:
        if own_session:
            db.close()
