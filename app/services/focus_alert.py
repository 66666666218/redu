"""重点关键词告警:跨板块共振 + 板块内反复出现 → 🔴重点 即时推飞书。

两种"出现型"信号(与 cross_platform 的趋势型互补):
- 跨板块共振:同一关键词出现在 ≥2 个板块(含公众号)的最新一批——全网级信号;
- 板块内反复:同一关键词近 24h 在同一板块出现 ≥ focus_repeat_rounds 轮——持续需求信号。

防噪设计:归一化匹配(去空白/小写)+ 包含匹配(短串 ≥ focus_min_len 才算);
纯数字/符号标题跳过;每关键词 focus_cooldown_hours(默认 24h)冷却;
跨板块推总群,板块内反复推该板块专属群;每次封顶 focus_max_items 条。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from config.settings import Settings, get_settings
from app.db.models import BaiduHotItem, DouhotWord, WechatArticle, WeiboHotItem, XianyuItem
from app.services.feishu import _col_set_row
from app.utils import get_logger

logger = get_logger(__name__)

SECTION_LABELS = {"weibo": "微博", "xianyu": "闲鱼", "douhot": "抖音", "baidu": "百度", "wechat": "公众号"}
_JUNK_RE = re.compile(r"^[\d\s#.@" + chr(0x1F300) + "-" + chr(0x1FAFF) + r"\-—・·]+$")


def _norm(title: str) -> str:
    return re.sub(r"\s+", "", title or "").lower()


def _board_data(db: Session, user_id: int) -> dict[str, dict]:
    """每板块近 24h 的出现数据:{norm: {orig, detail, rounds:set(ts), ts}}。

    rounds = 出现过的采集批次时间戳集合(同批共享同一时间戳)。
    """
    since = datetime.now() - timedelta(hours=24)
    out: dict[str, dict] = {}

    def add(section: str, title: str, detail: str, ts: datetime | None) -> None:
        n = _norm(title)
        if not n or len(n) < 3 or _JUNK_RE.match(n):
            return
        per = out.setdefault(section, {})
        if n not in per:
            per[n] = {"orig": title, "detail": detail, "rounds": set(), "ts": ts or datetime.min}
        per[n]["rounds"].add(ts or datetime.min)
        per[n]["ts"] = max(per[n]["ts"], ts or datetime.min)

    for r in db.scalars(select(WeiboHotItem).where(
            WeiboHotItem.user_id == user_id, WeiboHotItem.captured_at >= since)).all():
        add("weibo", r.title, f"排名#{r.rank} · 热度 {r.heat:,}", r.captured_at)
    for r in db.scalars(select(XianyuItem).where(
            XianyuItem.user_id == user_id, XianyuItem.created_at >= since)).all():
        add("xianyu", r.title, f"{r.price or '—'} · 命中{r.hit_keywords}词 · 最佳排名{r.best_rank}", r.created_at)
    for r in db.scalars(select(DouhotWord).where(
            DouhotWord.user_id == user_id, DouhotWord.created_at >= since)).all():
        add("douhot", r.title, f"指数 {getattr(r, 'score', 0) or 0}", r.created_at)
    for r in db.scalars(select(BaiduHotItem).where(
            BaiduHotItem.user_id == user_id, BaiduHotItem.captured_at >= since)).all():
        add("baidu", r.title, f"排名#{r.rank} · 热度 {r.heat:,}", r.captured_at)
    for r in db.scalars(select(WechatArticle).where(
            WechatArticle.user_id == user_id, WechatArticle.created_at >= since)).all():
        pan = f" · 🔴{r.pan_types}" if r.pan_types else ""
        add("wechat", r.title, f"{r.author or '公众号'}{pan}", r.created_at)
    return out


def _clusters(boards: dict[str, dict], min_len: int) -> list[list[tuple[str, str, dict]]]:
    """跨板块簇:归一化相等或包含(短串 ≥ min_len)且分属不同板块。并查集聚类。"""
    entries = [(sec, norm, v) for sec, per in boards.items() for norm, v in per.items()]
    parent = list(range(len(entries)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(entries)):
        s1, n1, _ = entries[i]
        for j in range(i + 1, len(entries)):
            s2, n2, _ = entries[j]
            if s1 == s2:
                continue
            short, long_ = (n1, n2) if len(n1) <= len(n2) else (n2, n1)
            if len(short) >= min_len and short in long_:
                ra, rb = find(i), find(j)
                if ra != rb:
                    parent[rb] = ra

    grouped: dict[int, list[tuple[str, str, dict]]] = {}
    for i, e in enumerate(entries):
        grouped.setdefault(find(i), []).append(e)
    return [g for g in grouped.values() if len({s for s, _, _ in g}) >= 2]


def run_focus_alert(db: Session, user_id: int, settings: Settings | None = None) -> int:
    """检测跨板块共振 + 板块内反复 → 🔴重点 卡片即时推送(每关键词 24h 冷却)。

    返回本次推送的重点关键词数;未启用/未配置对应 webhook 时返回 0。
    """
    settings = settings or get_settings()
    if not settings.focus_alert_enabled:
        return 0
    from app.services.feishu_client import FeishuClient, webhook_for

    boards = _board_data(db, user_id)
    if not boards:
        return 0
    # 各板块"最新一批"时间戳(出现=当前在榜)
    latest_ts = {sec: max(v["ts"] for v in per.values()) for sec, per in boards.items()}

    now = datetime.now()
    hits_cross: list[dict] = []
    hits_repeat: list[tuple[str, dict]] = []

    from app.services.alert_service import feishu_alert_gate

    def cooled(kind: str, norm: str) -> bool:
        """冷却门:False = 允许推送(记录已写入)。"""
        return not feishu_alert_gate(db, user_id, "focus", f"{kind}:{norm}",
                                     settings.focus_cooldown_hours, "重点关键词")

    # ① 跨板块共振(各板块最新一批中匹配)
    current = {sec: {n: v for n, v in per.items() if v["ts"] == latest_ts[sec]}
               for sec, per in boards.items()}
    for cluster in _clusters(current, settings.focus_min_len):
        norm = min((n for _, n, _ in cluster), key=len)
        if cooled("cross", norm):
            continue
        hits_cross.append({"norm": norm, "members": cluster})

    # ② 板块内反复(近 24h 出现轮数 ≥ 阈值 且 仍在榜;公众号为累积发文,无轮次概念,跳过)
    for sec in ("weibo", "xianyu", "douhot", "baidu"):
        per = boards.get(sec, {})
        for n, v in per.items():
            if n not in current.get(sec, {}) or len(v["rounds"]) < settings.focus_repeat_rounds:
                continue
            if cooled("repeat", f"{sec}:{n}"):
                continue
            hits_repeat.append((sec, v))

    pushed = 0
    client_cache: dict[str, FeishuClient] = {}

    def _client(webhook: str) -> FeishuClient:
        if webhook not in client_cache:
            client_cache[webhook] = FeishuClient(webhook, settings.feishu_secret)
        return client_cache[webhook]

    if hits_cross:
        hits_cross.sort(key=lambda h: -len(h["members"]))
        elements = [_col_set_row([("**关键词**", 4), ("**出现板块**", 3), ("**详情**", 5)], grey=True)]
        for h in hits_cross[: settings.focus_max_items]:
            orig = h["members"][0][2]["orig"][:24]
            secs = " · ".join(SECTION_LABELS.get(s, s) for s, _, _ in h["members"])
            details = " | ".join(f"{SECTION_LABELS.get(s, s)}:{v['detail']}" for s, _, v in h["members"])
            elements.append(_col_set_row([(f"🔴 **{orig}**", 4), (secs, 3), (details, 5)]))
        card = {"config": {"wide_screen_mode": True},
                "header": {"template": "red", "title": {"tag": "plain_text",
                           "content": f"🔴 重点 · 跨板块共振(新增 {len(hits_cross)} 个)"}},
                "elements": elements}
        if settings.feishu_webhook and _client(settings.feishu_webhook).send_card(card):
            pushed += len(hits_cross)

    repeat_by_sec: dict[str, list[dict]] = {}
    for sec, v in hits_repeat:
        repeat_by_sec.setdefault(sec, []).append(v)
    for sec, items in repeat_by_sec.items():
        webhook = webhook_for(settings, sec)
        if not webhook:
            continue
        items.sort(key=lambda v: -len(v["rounds"]))
        elements = [_col_set_row([("**标题**", 6), ("**近24h轮数**", 2), ("**最新**", 4)], grey=True)]
        for v in items[: settings.focus_max_items]:
            elements.append(_col_set_row([(f"🔴 **{v['orig'][:24]}**", 6),
                                          (f"{len(v['rounds'])} 轮", 2), (v["detail"], 4)]))
        card = {"config": {"wide_screen_mode": True},
                "header": {"template": "red", "title": {"tag": "plain_text",
                           "content": f"🔴 重点 · {SECTION_LABELS[sec]}反复出现(新增 {len(items)} 个)"}},
                "elements": elements}
        if _client(webhook).send_card(card):
            pushed += len(items)

    db.commit()
    if pushed:
        logger.info("重点关键词推送 user=%s 跨板块=%s 反复=%s", user_id,
                    len(hits_cross), len(hits_repeat))
    return pushed
