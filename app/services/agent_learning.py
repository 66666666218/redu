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
    "resonance": 30,  # 跨板块共振(同一个词在多个板块冒头)
    # **跨平台同资源共振**(2026-10-07):同一份资源在 ≥2 个平台被多个号在推。
    # 给 35 而不是 30:它比"同词跨板块"更接近"**有人已经在靠它拉新,而且不止一处**"
    # 这个事实。但**只是起点** —— 它会被回测按命中率上下调(±30%)。
    "cross_resonance": 35,
    "rank_jump": 15,  # 排名前移≥3
    "accel": 20,      # 加速上涨
}


#: 信号标签 → 权重键。⚠️ **顺序即语义**:「跨平台共振」必须排在「共振」**前面** ——
#: 判定是 `startswith(sig) or sig in part` 且**首个匹配胜**,排在后面它永远被"共振"吃掉,
#: 新信号就白加了(而且**不报错**,只是永远学不到)。
_SIG_KEYS: tuple[tuple[str, str], ...] = (
    ("增速", "velocity"), ("加速", "accel"), ("新上榜", "new_entry"),
    ("连续", "repeat"), ("量级", "volume"),
    ("跨平台共振", "cross_resonance"),      # ← 必须在"共振"之前
    ("共振", "resonance"), ("排名", "rank_jump"),
)


def sig_of_part(part: str) -> str:
    """一个信号标签属于哪个权重键;认不出返回空串。"""
    p = str(part or "")
    for sig, key in _SIG_KEYS:
        if p.startswith(sig) or sig in p:
            return key
    return ""


def sig_of_part_name(part: str) -> str:
    """同上,返回**信号名**(记账用);认不出返回空串。"""
    p = str(part or "")
    for sig, _ in _SIG_KEYS:
        if p.startswith(sig) or sig in p:
            return sig
    return ""


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


def _to_dt(ts) -> datetime | None:
    """序列时间戳容错转换(微博/百度/抖音=datetime,闲鱼 snap_date=日期字符串)。

    与 early_agent._to_dt 同款:闲鱼 xianyu_want_series 的 t 是 "YYYY-MM-DD" 字符串,
    直接和 AgentStage.updated_at(datetime)比较会抛 TypeError,回测整个用户当轮回测中断。
    """
    if isinstance(ts, datetime):
        return ts
    try:
        return datetime.fromisoformat(str(ts))
    except (ValueError, TypeError):
        return None


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

    # ★★ **更接近真相的标签:结算结果**(2026-10-07)
    #
    # `HotspotSuggestion.repost_gain`(每晚 22:00 结算)= 发文后**全网新增的该文盘链数**,
    # 也就是"这条资源**真的被疯转了**没有" —— 比"关键词热度涨没涨"更接近拉新要的东西。
    # **它一直在库里算着,却从没被回测用过**(只被结算端自己消费)。
    # 这里把它接上:
    #   · 该关键词**有已结算的建议** ⇒ **以结算为准**(命中 = 真的传播开了);
    #   · 没有 ⇒ 退回原来的热度标签 —— **不能因为"没结算"就不记样本**,那会让样本大量流失。
    #
    # ⚠️ 归一化必须与 `AgentStage.norm` **同一口径**(去空格 + 小写),否则两边对不上:
    #    实测 `kw='小米18Pro 防窥屏'` 的 `norm='小米18pro防窥屏'`,而建议表存的是**原始 keyword**。
    # ⚠️ 懒导入:模块级 import hotspot_agent 会成环(它反过来调本模块)。
    from app.services.hotspot_agent import _norm as _norm_kw

    settled: dict[str, int] = {}
    try:
        from app.db.models import HotspotSuggestion

        for sug in db.scalars(select(HotspotSuggestion).where(
                HotspotSuggestion.user_id == user_id,
                HotspotSuggestion.settled_at.isnot(None))).all():
            _k = _norm_kw(str(sug.keyword or ""))
            if _k:
                settled[_k] = int(sug.repost_gain or 0)
    except Exception:  # noqa: BLE001 - 读不到结算就退回热度标签,别让整轮回测停摆
        logger.exception("读结算结果失败(本轮退回热度标签)")
    label_counts = {"结算": 0, "热度": 0}

    backtested = hits = 0
    two_days_ago = dt.now() - timedelta(days=2)
    # ★★ 2026-10-14 **改按「推送」配对**(用户口径 A)—— 原实现取 `AgentStage` 里
    #    stage∈{苗头,上升} 且 2 天前的行,而那张表是**一行一个词、阶段**原地升级**
    #    (`stage` 记当前、`first_seen` 记首次)⇒ **早期阶段根本存不下来**:远程实测
    #    **287 行全是「爆发」、苗头/上升 0 行** ⇒ 取样**恒为空** ⇒ `backtested` 恒 0
    #    ⇒ 权重/命中率/游标**永远写不出来**(作业天天跑、日志无错、指标恒 0 = 静默空转)。
    #    ⇒ 换成以 `HotspotSuggestion`(**真实推送记录**:词/时间/结算)为样本源:
    #      样本 0 → ~239;平台从 `platforms` 认,信号归因仍取 `AgentStage.parts`。
    from app.db.models import HotspotSuggestion

    pushes = db.scalars(select(HotspotSuggestion).where(
        HotspotSuggestion.user_id == user_id,
        HotspotSuggestion.created_at <= two_days_ago)).all()
    #: 词 → 阶段行(只为拿 `parts` 里的信号标签;拿不到也照样回测,只是不记信号归因)
    stage_by_kw: dict[str, AgentStage] = {}
    for _st in db.scalars(select(AgentStage).where(AgentStage.user_id == user_id)).all():
        stage_by_kw.setdefault(_norm_kw(str(_st.norm or _st.kw or "")), _st)

    # 已回测游标:同一阶段行(updated_at 未变)只回测一次,防止静止行每日重复记账
    # (旧实现把同一行天天计入 stats,total 虚胖且同一行"昨天 miss 今天 hit"自相矛盾)
    seen_key = "agent_backtest_seen"
    seen_row = db.scalar(select(SystemConfig).where(SystemConfig.key == seen_key))
    seen: dict[str, str] = {}
    if seen_row and seen_row.value:
        try:
            seen = json.loads(seen_row.value)
        except (ValueError, TypeError):
            seen = {}

    for sug in pushes:
        kw = _norm_kw(str(sug.keyword or ""))
        if not kw:
            continue
        # 游标键尾仍是 ISO 时间戳 ⇒ 与下面的"按保留期剪枝"同一形状,孤儿键能被剪掉
        row_sig = f"sug|{kw}|{sug.created_at.isoformat() if sug.created_at else ''}"
        if seen.get(row_sig):
            continue
        # ★★ **顺序很要紧:先看结算,再退回热度**(2026-10-14 实测定的序)——
        #    结算标签**不需要任何热度序列**;而热度标签需要该词在四路序列里有 ≥2 个点。
        #    实测:这 198 条建议的词是**资源/需求类**(`下载`/`问卷`/`人格测试`/剧名…),
        #    与热榜词表**几乎不重叠(只有 2/198 能对到序列)** ⇒ 若先判序列,
        #    **41 条有结算的样本会被一起挡掉**,`backtested` 还是接近 0(等于没修)。
        if kw in settled:
            # 结算口径:`repost_gain` = 发文后**全网新增的该文盘链数** ⇒ "真被传播开了没有"
            hit = settled[kw] > 0
            label = "结算"
        else:
            # 退路:热度涨了 ≥50%。平台从 `platforms` 认(字段为空的老行再挨个板块找一遍)。
            series = None
            for _b in [x.strip() for x in str(sug.platforms or "").split(",") if x.strip()]:
                _c = series_map.get(_b, {}).get(kw)
                if _c and len(_c) >= 2:
                    series = _c
                    break
            if series is None:
                for _m in series_map.values():
                    if _m.get(kw) and len(_m[kw]) >= 2:
                        series = _m[kw]
                        break
            if series is None:
                continue
            # 推送时基线:取 created_at(**推送时刻**)之前的最后一个样本。
            # 旧实现取 values[-2](=昨天的值),系统性低估增长率 → 命中率虚低 → 权重漂移。
            cutoff = sug.created_at or two_days_ago
            # 时间戳可能是 datetime(微博/百度/抖音)或日期字符串(闲鱼):先归一,
            # 否则闲鱼行会让 `t <= cutoff` 抛 TypeError,连带打断该用户本轮全部板块回测。
            base_pts = [v for t, v in series if (td := _to_dt(t)) is not None and td <= cutoff]
            if not base_pts:
                continue
            base = float(base_pts[-1])
            now_v = float(series[-1][1])
            if base <= 0:
                continue
            growth = (now_v - base) / base
            hit = growth >= 0.5
            label = "热度"
        label_counts[label] += 1
        seen[row_sig] = "1"
        backtested += 1
        if hit:
            hits += 1
        # 解析**该词**的信号标签,逐信号记账(拿不到阶段行就不记归因,但样本照样算)
        _stage = stage_by_kw.get(kw)
        for part in ((_stage.parts if _stage else "") or "").split():
            sig = sig_of_part_name(part)      # 顺序语义见 _SIG_KEYS 的注释
            if sig:
                bucket = signal_stats.setdefault(sig, {"hits": 0, "total": 0})
                bucket["total"] += 1
                if hit:
                    bucket["hits"] += 1

    # ⚠️⚠️ **样本有了、归因没有 ⇒ 权重还是不会动** —— 必须说出来(2026-10-14 实测):
    #    信号标签只存在 `AgentStage.parts` 里,而**被推送的那些词从没被早期信号打过标签**
    #    (实测 41 条已结算样本里 **0 条**能在阶段表找到对应行)⇒ 每个信号的 `total` 都是 0
    #    ⇒ 下面那道 `total >= 5` 永远过不去。**不说的话,这里会静默成"学习在跑、只是没调"**,
    #    与"没有样本"长得一模一样(本仓母题)。⇒ 计数并告警,让"缺的是哪一环"一眼可见。
    attributed = sum(int(v.get("total") or 0) for v in signal_stats.values())
    if backtested and not attributed:
        logger.warning("Agent 学习:样本 %d 条但**信号归因 0 条** ⇒ 权重不会动。"
                       "原因:标签只存在 `AgentStage.parts` 里,而**被推送的词没被早期信号打过标签**"
                       "(2026-10-14 实测 41 条已结算样本里 0 条能对上阶段表)。"
                       "⇒ 要么在推送时把信号记进建议表,要么改用板块粒度归因(见 CHANGELOG)",
                       backtested)

    # 权重自适应(样本 ≥10 才动,防小样本抖动;幅度 ±30%)
    if backtested >= 10:
        for sig, stat in signal_stats.items():
            if stat["total"] < 5:
                continue
            rate = stat["hits"] / stat["total"]
            wkey = dict(_SIG_KEYS).get(sig)   # 与上面同一份表,不再各写一份(会漂)
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

    # 命中率样本 + 回测游标持久化(无论是否动权重都要写):
    # - signal_stats 必须写回,否则每次调用都从空重建,"累计≥10样本"永远凑不齐,自学习断裂;
    # - seen 游标不写的话静止行明天又被回测一遍。
    # seen 剪枝(无条件跑):键尾是 `st.updated_at.isoformat()`,对应 AgentStage 行
    # 超过 data_retention_days 会被维护 job 删掉,游标键成为孤儿并单调增长——
    # 每天 06:00 全量 json.loads/写回,体积与耗时线性劣化。按时间戳剪掉超出
    # 保留期 + 缓冲的键,与主表生命周期对齐。
    retention_cut = dt.now() - timedelta(days=int(getattr(settings, "data_retention_days", 30)) + 5)
    pruned: dict[str, str] = {}
    for k, v in seen.items():
        ts_iso = k.rsplit("|", 1)[-1]
        try:
            if dt.fromisoformat(ts_iso) < retention_cut:
                continue
        except (ValueError, TypeError):
            pass  # 解析失败保留,宁可多留一天也别误删导致静止行被重复计入
        pruned[k] = v
    seen_changed = len(pruned) != len(seen)
    seen = pruned
    if backtested or seen_changed:
        if row:
            row.value = json.dumps(signal_stats, ensure_ascii=False)
        elif backtested:
            db.add(SystemConfig(key=stats_key, value=json.dumps(signal_stats, ensure_ascii=False)))
        seen_payload = json.dumps(seen, ensure_ascii=False)
        if seen_row:
            seen_row.value = seen_payload
        else:
            db.add(SystemConfig(key=seen_key, value=seen_payload))
        db.commit()

    # ⚠️ **把"用哪种标签学的"报出来**:否则接没接上、结算覆盖多少,运维都看不见
    # (本仓母题:只写不读等于没有)。
    if label_counts["结算"] or backtested:
        logger.info("Agent 回测:%d 例(结算标签 %d / 热度标签 %d),命中 %d",
                    backtested, label_counts["结算"], label_counts["热度"], hits)
    return {"backtested": backtested, "hits": hits, "weights": weights,
            "attributed": attributed,   # 有信号归因的样本数(0 = 权重不会动,见上面的告警)
            "labels": label_counts}
