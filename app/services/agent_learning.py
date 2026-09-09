"""苗头 Agent 自学习:回测已推送苗头的后续走势,自适应调整信号权重。

闭环:
  ① 推送苗头时记录该关键词当时的热度(learn_miao_snapshot 已由 AgentStage 承载)
  ② 每日回测:对"2 天前推送的苗头"检查后续热度变化
     - 增长 ≥50% → 命中(正样本);增长 <20% → 未命中(负样本)
  ③ 累计 ≥10 个样本后,统计各信号的命中率,命中率高的信号权重上调(在线学习)
     权重调整幅度限制在 ±30% 以内,避免小样本抖动

设计原则:纯本地计算、无外部依赖;学习结果持久化到 system_config 表(agent_weights)。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from config.settings import get_settings
from app.db import repository
from app.db.models import SystemConfig
from app.utils import get_logger

logger = get_logger(__name__)

# 信号 → 所在序列(回测用:按板块取关键词热度序列)
_WEIGHTS_KEY = "agent_weights"

DEFAULT_WEIGHTS = {
    "velocity": 40,   # 增速≥50%(+30)/≥100%(+40)按比例
    "new_entry": 25,  # 新上榜
    "repeat": 15,     # 连续≥3轮
    "volume": 15,     # 量级≥200
    "resonance": 30,  # 跨板块共振
    "rank_jump": 15,  # 排名前移≥3
    "accel": 20,      # 加速上涨
}


def load_weights(db: Session) -> dict[str, int]:
    """读取学习后的权重(无记录用默认值;键缺失补默认)。"""
    row = db.scalar(select(SystemConfig).where(SystemConfig.key == _WEIGHTS_KEY))
    weights = dict(DEFAULT_WEIGHTS)
    if row and row.value:
        try:
            saved = json.loads(row.value)
            for k, v in saved.items():
                if k in weights and isinstance(v, (int, float)):
                    weights[k] = int(v)
        except (ValueError, TypeError):
            pass
    return weights


def save_weights(db: Session, weights: dict[str, int]) -> None:
    row = db.scalar(select(SystemConfig).where(SystemConfig.key == _WEIGHTS_KEY))
    payload = json.dumps(weights, ensure_ascii=False)
    if row:
        row.value = payload
    else:
        db.add(SystemConfig(key=_WEIGHTS_KEY, value=payload))
    db.commit()


def _hit_rate(samples: list[tuple[bool, ...]]) -> float:
    """命中样本占比;无样本返回 -1(未知)。"""
    if not samples:
        return -1.0
    return sum(1 for s in samples if s) / len(samples)


def backtest_and_learn(db: Session, user_id: int, settings=None) -> dict:
    """每日回测:检查 N 天前 AgentStage 里"苗头/上升"阶段的关键词,后续是否真的爆发。

    爆发定义:当前最新热度 ≥ 推送时热度 × 1.5(增长 50%)。
    统计各信号的命中/未命中,调整权重(命中率高→+10%,低→-10%,幅度≤30%)。
    返回 {backtested: 回测数, hits: 命中数, weights: 当前权重}。
    """
    settings = settings or get_settings()
    from app.db.models import AgentStage
    from datetime import datetime as dt

    weights = load_weights(db)
    series_map = {
        "weibo": repository.weibo_heat_series(db, user_id),
        "xianyu": repository.xianyu_want_series(db, user_id),
        "douhot": repository.douhot_score_series(db, user_id),
        "baidu": repository.baidu_heat_series(db, user_id),
    }
    # 信号 → 命中样本累计(存 parts 文本,回测时解析)
    stats_key = "agent_signal_stats"
    row = db.scalar(select(SystemConfig).where(SystemConfig.key == stats_key))
    signal_stats: dict[str, dict] = {}
    if row and row.value:
        try:
            signal_stats = json.loads(row.value)
        except (ValueError, TypeError):
            signal_stats = {}

    backtested = hits = 0
    two_days_ago = dt.now() - timedelta(days=2)
    stages = db.scalars(select(AgentStage).where(
        AgentStage.user_id == user_id,
        AgentStage.stage.in_(("苗头", "上升")),
        AgentStage.updated_at <= two_days_ago)).all()

    for st in stages:
        series = series_map.get(st.board, {}).get(st.norm or st.kw)
        if not series or len(series) < 2:
            continue
        values = [float(v) for _, v in series]
        # 推送时热度:没有精确记录,用 updated_at 时刻附近的值近似(取倒数第2个样本)
        base = values[-2] if len(values) >= 2 else values[0]
        now_v = values[-1]
        if base <= 0:
            continue
        growth = (now_v - base) / base
        hit = growth >= 0.5
        backtested += 1
        if hit:
            hits += 1
        # 解析该阶段的信号标签,逐信号记账
        for part in (st.parts or "").split():
            for sig in ("增速", "加速", "新上榜", "连续", "量级", "共振", "排名"):
                if part.startswith(sig) or sig in part:
                    bucket = signal_stats.setdefault(sig, {"hits": 0, "total": 0})
                    bucket["total"] += 1
                    if hit:
                        bucket["hits"] += 1
                    break

    # 权重自适应(样本 ≥10 才动,防小样本抖动;幅度 ±30%)
    if backtested >= 10:
        for sig, stat in signal_stats.items():
            if stat["total"] < 5:
                continue
            rate = stat["hits"] / stat["total"]
            key_map = {"增速": "velocity", "加速": "accel", "新上榜": "new_entry",
                       "连续": "repeat", "量级": "volume", "共振": "resonance", "排名": "rank_jump"}
            wkey = key_map.get(sig)
            if not wkey:
                continue
            base_w = DEFAULT_WEIGHTS[wkey]
            factor = 1.1 if rate >= 0.5 else 0.9
            new_w = int(max(base_w * 0.7, min(base_w * 1.3, weights[wkey] * factor)))
            if new_w != weights[wkey]:
                logger.info("Agent 权重自适应:%s(%s 命中率 %.0f%%)%d → %d",
                            sig, wkey, rate * 100, weights[wkey], new_w)
                weights[wkey] = new_w
        save_weights(db, weights)

    return {"backtested": backtested, "hits": hits, "weights": weights}
