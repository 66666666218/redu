"""早期苗头 Agent:全板块 感知→联想→评估→决策→行动 的自主预测闭环。

业务目标:在热点刚起势(苗头期)就提醒用户跟进网盘推广,避开高峰/衰败期。

信号(每板块按各自序列计算,加权融合为 0~100 分):
- ① 增速:相邻两轮环比(≥50% +30 / ≥100% +40)
- ② 新上榜:近 24h 首次出现且样本 ≤2(+25)——刚起势的标志
- ③ 反复:连续出现 ≥3 轮(+15)——持续需求
- ④ 量级:最新值 ≥200(+15)——过滤噪音
- ⑤ 跨板块共振:同名/包含关键词出现在 ≥2 板块(各 +30)——全网级信号

决策:总分 ≥ agent_score_threshold(默认 55)即"苗头",≥80 为"强苗头";
每 (板块,关键词) agent_cooldown_hours(默认 12h)冷却;共振信号推总群,单板块推专属群。
与既有系统分工:focus_alert 管"出现型",实时阈值推送管"已爆发",本模块管"正在起势"。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from config.settings import Settings, get_settings
from app.db import repository
from app.db.models import FeishuAlert
from app.services.feishu import _col_set_row
from app.services import keyword_agent
from app.utils import get_logger

logger = get_logger(__name__)

SECTION_LABELS = {"weibo": "微博", "xianyu": "闲鱼", "douhot": "抖音", "baidu": "百度"}
_JUNK_RE = re.compile(r"^[\d\s#.@" + chr(0x1F300) + "-" + chr(0x1FAFF) + r"\-—・·]+$")


def _norm(title: str) -> str:
    return re.sub(r"\s+", "", title or "").lower()


def _board_series(db: Session, user_id: int) -> dict[str, dict[str, list[tuple]]]:
    return {
        "weibo": repository.weibo_heat_series(db, user_id),
        "xianyu": repository.xianyu_want_series(db, user_id),
        "douhot": repository.douhot_score_series(db, user_id),
        "baidu": repository.baidu_heat_series(db, user_id),
    }


def detect_signals(db: Session, user_id: int, settings: Settings) -> list[dict]:
    """全板块信号评分。返回 [{board, kw, score, parts, latest, boards}] 按分排序。"""
    signals: list[dict] = []
    for sec, series in _board_series(db, user_id).items():
        for kw, pts in series.items():
            n = _norm(kw)
            if not n or len(n) < 4 or _JUNK_RE.match(n):
                continue
            values = [v for _, v in pts]
            if not values:
                continue
            latest = values[-1]
            prev = values[-2] if len(values) >= 2 else None
            parts: list[str] = []
            score = 0
            if prev is not None and prev > 0:
                v = (latest - prev) / prev * 100
                if v >= 100:
                    parts.append(f"增速+{v:.0f}%")
                    score += 40
                elif v >= 50:
                    parts.append(f"增速+{v:.0f}%")
                    score += 30
            first_ts = pts[0][0]
            if len(values) <= 2 and isinstance(first_ts, datetime) and \
                    datetime.now() - first_ts <= timedelta(hours=24):
                parts.append("新上榜")
                score += 25
            if len(values) >= 3:
                parts.append(f"连续{len(values)}轮")
                score += 15
            if latest >= 200:
                parts.append(f"量级{latest}")
                score += 15
            if not parts:
                continue
            signals.append({"board": sec, "kw": kw, "norm": n, "score": min(score, 100),
                            "parts": parts, "latest": latest})

    # 跨板块联想:同名/包含出现在 ≥2 板块 → 共振加成 +30,并合并为一条(取最高分板块代表)
    for i, s1 in enumerate(signals):
        for s2 in signals[i + 1:]:
            if s1["board"] == s2["board"]:
                continue
            short, long_ = (s1["norm"], s2["norm"]) if len(s1["norm"]) <= len(s2["norm"]) else (s2["norm"], s1["norm"])
            if len(short) >= settings.focus_min_len and short in long_:
                for s in (s1, s2):
                    if "共振" not in s["parts"]:
                        s["parts"].append("共振")
                        s["score"] = min(100, s["score"] + 30)
                        s["boards"] = sorted({s1["board"], s2["board"]})
    return sorted(signals, key=lambda x: -x["score"])


def agent_tick(db: Session, user_id: int, settings: Settings | None = None) -> int:
    """Agent 主循环:评分 → 阈值决策 → 冷却去重 → 推送。返回推送条数。"""
    settings = settings or get_settings()
    if not settings.agent_enabled:
        return 0
    from app.services.feishu_client import FeishuClient, webhook_for

    signals = [s for s in detect_signals(db, user_id, settings)
               if s["score"] >= settings.agent_score_threshold]
    if not signals:
        return 0

    now = datetime.now()
    cooldown = timedelta(hours=settings.agent_cooldown_hours)
    to_push: list[dict] = []
    for s in signals[: settings.focus_max_items]:
        key = f"{s['board']}:{s['norm']}"
        existing = db.scalar(select(FeishuAlert).where(
            FeishuAlert.section == "agent_miao", FeishuAlert.user_id == user_id, FeishuAlert.title == key))
        if existing and (now - existing.alerted_at) < cooldown:
            continue
        if existing:
            existing.reason, existing.alerted_at = f"score={s['score']}", now
        else:
            db.add(FeishuAlert(section="agent_miao", user_id=user_id, title=key,
                               reason=f"score={s['score']}", alerted_at=now))
        s["boards"] = s.get("boards") or [s["board"]]
        to_push.append(s)
    db.commit()
    if not to_push:
        return 0

    multi = [s for s in to_push if len(s.get("boards", [s["board"]])) >= 2]
    single = [s for s in to_push if s not in multi]
    client_cache: dict[str, FeishuClient] = {}

    def _client(webhook: str) -> FeishuClient:
        if webhook not in client_cache:
            client_cache[webhook] = FeishuClient(webhook, settings.feishu_secret)
        return client_cache[webhook]

    def _card(items: list[dict], title: str) -> dict:
        elements = [_col_set_row([("**板块**", 2), ("**关键词**", 4), ("**信号分解**", 4), ("**总分**", 2)], grey=True)]
        for s in items:
            label = SECTION_LABELS.get(s["board"], s["board"])
            if len(s.get("boards", [])) >= 2:
                label = "+".join(SECTION_LABELS.get(b, b) for b in s["boards"])
            elements.append(_col_set_row([
                (label, 2), (f"🔴 **{_md_safe_light(s['kw'])[:20]}**", 4),
                (" · ".join(s["parts"]), 4), (f"**{s['score']}**", 2)]))
        return {"config": {"wide_screen_mode": True},
                "header": {"template": "red", "title": {"tag": "plain_text", "content": title}},
                "elements": elements}

    pushed = 0
    if multi:
        if _client(settings.feishu_webhook).send_card(
                _card(multi, f"🧠 苗头 Agent · 跨板块强信号({len(multi)})")):
            pushed += len(multi)
    for s in single:
        webhook = webhook_for(settings, s["board"])
        if not webhook:
            continue
        if _client(webhook).send_card(_card([s], f"🧠 苗头 Agent · {SECTION_LABELS.get(s['board'], s['board'])}")):
            pushed += 1
    return pushed


def _md_safe_light(text: str) -> str:
    return (text or "").replace("[", "【").replace("]", "】")


def agent_tick_all_users(settings: Settings | None = None) -> int:
    """调度入口:所有用户各跑一轮苗头 Agent。"""
    from app.db import get_session_local
    from app.db.models import User

    settings = settings or get_settings()
    if not settings.agent_enabled:
        return 0
    db = get_session_local()()
    pushed = 0
    try:
        for uid in db.scalars(select(User.id).order_by(User.id)).all():
            try:
                pushed += agent_tick(db, uid, settings)
            except Exception:  # noqa: BLE001
                db.rollback()
                logger.exception("苗头 Agent 失败 user=%s", uid)
    finally:
        db.close()
    return pushed
