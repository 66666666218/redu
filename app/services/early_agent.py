"""早期苗头 Agent v2:全板块 感知→联想→评估→决策→行动 自主预测闭环(带生命周期记忆)。

业务目标:在热点刚起势(苗头期)就提醒用户跟进网盘推广,避开高峰/衰败期。

信号(每板块按各自序列计算,加权融合为 0~100 分):
- ① 增速:相邻两轮环比(≥50% +30 / ≥100% +40)
- ② 加速:增速比再升 ≥20 个百分点(+20)——起势最早的标志
- ③ 新上榜:近 24h 首次出现且样本 ≤2(+25)
- ④ 反复:连续出现 ≥3 轮(+15)
- ⑤ 量级:最新值 ≥200(+15)
- ⑥ 跨板块共振:同名/包含关键词出现在 ≥2 板块(各 +30)

决策(生命周期状态机,agent_stages 表持久化"思维记忆"):
- 阶段映射:苗头(≥threshold)→ 上升(≥70)→ 爆发(≥85);增速 ≤-30% → 回落
- 仅在**阶段跃迁**时推送(苗头→上升→爆发逐级升级;从上升/爆发跌入回落提醒止损)
- 每阶段带网盘推广行动建议;卡片展示跟踪时长与信号分解
与既有系统分工:focus_alert 管"出现型",实时阈值推送管"已爆发",本模块管"正在起势与演化"。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from config.settings import Settings, get_settings
from app.db import repository
from app.db.models import AgentStage
from app.services.feishu import _col_set_row
from app.utils import get_logger

logger = get_logger(__name__)

SECTION_LABELS = {"weibo": "微博", "xianyu": "闲鱼", "douhot": "抖音", "baidu": "百度"}
_JUNK_RE = re.compile(r"^[\d\s#.@" + chr(0x1F300) + "-" + chr(0x1FAFF) + r"\-—・·]+$")
STAGE_ORDER = {"回落": 0, "苗头": 1, "上升": 2, "爆发": 3}
STAGE_ADVICE = {
    "苗头": "准备素材与标题,持续观察确认",
    "上升": "立即改写发布,抢占流量窗口",
    "爆发": "流量高峰,全力推广 + 多渠道分发",
    "回落": "需求衰退,停止跟进,等待下一个苗头",
}


def _norm(title: str) -> str:
    return re.sub(r"\s+", "", title or "").lower()


def _to_dt(ts) -> datetime | None:
    """序列时间戳容错转换(微博/百度=datetime,闲鱼=snap_date 字符串)。"""
    if isinstance(ts, datetime):
        return ts
    try:
        return datetime.fromisoformat(str(ts))
    except ValueError:
        return None


def _board_series(db: Session, user_id: int) -> dict[str, dict[str, list[tuple]]]:
    return {
        "weibo": repository.weibo_heat_series(db, user_id),
        "xianyu": repository.xianyu_want_series(db, user_id),
        "douhot": repository.douhot_score_series(db, user_id),
        "baidu": repository.baidu_heat_series(db, user_id),
    }


def _md_safe_light(text: str) -> str:
    return (text or "").replace("[", "【").replace("]", "】")


def detect_signals(db: Session, user_id: int, settings: Settings) -> list[dict]:
    """全板块信号评分(纯函数式,可单测)。返回 [{board, kw, norm, score, parts, latest}] 按分排序。"""
    signals: list[dict] = []
    for sec, series in _board_series(db, user_id).items():
        for kw, pts in series.items():
            n = _norm(kw)
            if not n or len(n) < 4 or _JUNK_RE.match(n):
                continue
            values = [float(v) for _, v in pts]
            if not values:
                continue
            latest = values[-1]
            prev = values[-2] if len(values) >= 2 else None
            parts: list[str] = []
            score = 0
            v_now = None
            if prev is not None and prev > 0:
                v_now = (latest - prev) / prev * 100
                if v_now >= 100:
                    parts.append(f"增速+{v_now:.0f}%")
                    score += 40
                elif v_now >= 50:
                    parts.append(f"增速+{v_now:.0f}%")
                    score += 30
                elif v_now <= -30:
                    parts.append(f"回落{v_now:.0f}%")
            # ② 加速:增速比再升 ≥20 个百分点(起势最早的标志)
            if len(values) >= 3 and values[-3] > 0:
                v_prev = (prev - values[-3]) / values[-3] * 100 if prev is not None else 0
                if v_now is not None and v_now - v_prev >= 20 and v_now >= 20:
                    parts.append("加速上涨")
                    score += 20
            # ③ 新上榜:近 24h 首次出现且样本 ≤2(兼容 datetime/字符串时间戳)
            first_ts = _to_dt(pts[0][0])
            if len(values) <= 2 and first_ts and datetime.now() - first_ts <= timedelta(hours=24):
                parts.append("新上榜")
                score += 25
            # ④ 反复:连续出现 ≥3 轮
            if len(values) >= 3:
                parts.append(f"连续{len(values)}轮")
                score += 15
            # ⑤ 量级
            if latest >= 200:
                parts.append(f"量级{latest:.0f}")
                score += 15
            if not parts:
                continue
            signals.append({"board": sec, "kw": kw, "norm": n, "score": min(score, 100),
                            "parts": parts, "latest": latest})

    # ⑥ 跨板块联想:同名/包含出现在 ≥2 板块 → 共振加成 +30
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


def _stage_of(score: int, parts: list[str], settings: Settings) -> str:
    if any(p.startswith("回落") for p in parts):
        return "回落"
    if score >= 85:
        return "爆发"
    if score >= 70:
        return "上升"
    if score >= settings.agent_score_threshold:
        return "苗头"
    return "观察"


def agent_tick(db: Session, user_id: int, settings: Settings | None = None) -> int:
    """Agent 主循环:评分 → 生命周期决策(仅阶段跃迁推送)→ 行动建议。返回推送条数。"""
    settings = settings or get_settings()
    if not settings.agent_enabled:
        return 0
    from app.services.feishu_client import FeishuClient, webhook_for

    signals = detect_signals(db, user_id, settings)
    tracked = {(s.board, s.norm): s for s in db.scalars(select(AgentStage).where(
        AgentStage.user_id == user_id)).all()}
    now = datetime.now()
    to_push: list[dict] = []
    stage_rows: list[AgentStage] = []

    for s in signals[: settings.focus_max_items * 2]:
        key = (s["board"], s["norm"])
        st = tracked.get(key)
        new_stage = _stage_of(s["score"], s["parts"], settings)
        old_stage = st.stage if st else None

        # 决策:首次进入苗头以上、阶段跃迁(升级)、或跌入回落 → 行动
        escalated = st is None and STAGE_ORDER.get(new_stage, 0) >= 1
        upgraded = st is not None and STAGE_ORDER.get(new_stage, 0) > STAGE_ORDER.get(old_stage, 0)
        crashed = st is not None and new_stage == "回落" and old_stage in ("苗头", "上升", "爆发")
        if not (escalated or upgraded or crashed):
            # 无阶段变化:仅刷新记忆
            if st:
                st.score, st.parts, st.updated_at = s["score"], " ".join(s["parts"])[:255], now
            continue

        if st is None:
            st = AgentStage(user_id=user_id, board=s["board"], norm=s["norm"], kw=s["kw"][:255],
                            stage=new_stage, score=s["score"],
                            parts=" ".join(s["parts"])[:255], first_seen=now, updated_at=now)
            tracked[key] = st
            db.add(st)
        else:
            st.stage, st.score, st.parts, st.kw, st.updated_at = (
                new_stage, s["score"], " ".join(s["parts"])[:255], s["kw"][:255], now)
        s.update({"stage": new_stage, "old_stage": old_stage, "advice": STAGE_ADVICE[new_stage],
                  "tracked_h": int((now - st.first_seen).total_seconds() // 3600)})
        to_push.append(s)
        stage_rows.append(st)

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
        elements = [_col_set_row([("**板块**", 2), ("**关键词**", 4), ("**阶段·信号**", 4), ("**分**", 2)], grey=True)]
        for s in items:
            label = SECTION_LABELS.get(s["board"], s["board"])
            if len(s.get("boards", [])) >= 2:
                label = "+".join(SECTION_LABELS.get(b, b) for b in s["boards"])
            badge = {"苗头": "🌱", "上升": "📈", "爆发": "🚀", "回落": "📉"}.get(s["stage"], "")
            advice = f"<br/>建议:{s['advice']}"
            track = f" · 已跟踪 {s['tracked_h']}h" if s.get("tracked_h") else ""
            elements.append(_col_set_row([
                (label + track, 2),
                (f"🔴 **{_md_safe_light(s['kw'])[:18]}**", 4),
                (f"{badge}{s['stage']} · " + " · ".join(s["parts"]) + advice, 4),
                (f"**{s['score']}**", 2)]))
        return {"config": {"wide_screen_mode": True},
                "header": {"template": "red", "title": {"tag": "plain_text", "content": title}},
                "elements": elements}

    pushed = 0
    multi = [s for s in to_push if len(s.get("boards", [s["board"]])) >= 2]
    single = [s for s in to_push if s not in multi]
    if multi:
        if _client(settings.feishu_webhook).send_card(
                _card(multi, f"🧠 苗头 Agent · 跨板块({len(multi)})")):
            pushed += len(multi)
    by_sec: dict[str, list[dict]] = {}
    for s in single:
        by_sec.setdefault(s["board"], []).append(s)
    for sec, items in by_sec.items():
        webhook = webhook_for(settings, sec)
        if not webhook:
            continue
        if _client(webhook).send_card(_card(items, f"🧠 苗头 Agent · {SECTION_LABELS.get(sec, sec)}({len(items)})")):
            pushed += len(items)
    return pushed


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
