# -*- coding: utf-8 -*-
"""用「AI 分数 ↔ 真实结果」**自动校准**分数线(2026-10-29)。

用户口径:「可以根据之前的数据来进行策略决策」;上一轮的先验是**赛道级**的静态率,
这一层是**闭环**:AI 给的分数到底准不准,由后续真实结果检验,并据此调整
`ai_min_score:<板块>`。

## 标签从哪来

· `douyin_peer`(抖音线索):`ai_filter_decisions.item_key = douyin_leads.aweme_id`,
  join 出 `our_url != ''` = **搬成**。⚠️ 转存在推送**之前**做,所以**被 AI 挡掉的线索
  也有结果标签** —— 这正是能校准的前提。
· 其它板块暂时没有全量结果标签(只有"推没推"),**不参与校准**(别拿没标签的样本瞎调)。

## 选阈值的规则(写死,便于回测)

对 0~10 每个阈值:保留 = 分数 ≥ 阈值。
· 先砍掉**保留比例 < `min_keep_ratio`(默认 20%)** 的阈值 —— 不能为了好看把内容筛没;
· 在剩下的里选**保留部分的搬成率最高**的;同率取**更低阈值**(多留内容)。
· 平滑:一次最多动 1 分,避免一两批样本把线甩飞。
· **样本 < `min_samples`(默认 40)不调**,只记「样本不足」。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from app.utils import get_logger

logger = get_logger(__name__)


def heat_pairs(session, user_id: int, section: str, days: int = 21,
               hot_min: int = 100) -> list[tuple[int, int]]:
    """`[(score, hot)]` —— **热度**标签:`share_count >= hot_min`。

    ★ 为什么不用「搬成」当标签(2026-10-29 实测 975 条):
      搬成率在各分数档**几乎一样**(0~7 分都是 64~65%,9 分反而 43%) ——
      因为"搬成"取决于口令/迅雷机制,不取决于内容好不好。
      而 **AI 分数与转发数高度相关**(转发 0 → 均分 2.49;1000+ → 7.31) ——
      这正是用户要的价值口径「资源发的多、转存人数多」。
    ⇒ **用热度当标签**,否则会把一条有效的排序器判成"没用"。
    """
    from app.db.models import AiFilterDecision, DouyinLead
    from sqlalchemy import and_

    cutoff = datetime.now() - timedelta(days=max(1, int(days)))
    rows = session.execute(
        select(AiFilterDecision.score, DouyinLead.share_count).join(
            DouyinLead,
            and_(DouyinLead.user_id == AiFilterDecision.user_id,
                 DouyinLead.aweme_id == AiFilterDecision.item_key)).where(
            AiFilterDecision.user_id == user_id,
            AiFilterDecision.section == section,
            AiFilterDecision.decided_at >= cutoff)).all()
    return [(int(r[0] or 0), 1 if int(r[1] or 0) >= int(hot_min) else 0) for r in rows]


def outcome_pairs(session, user_id: int, section: str, days: int = 21) -> list[tuple[int, int]]:
    """`[(score, moved)]` —— "搬成"标签。**保留供分析**,但**不要拿它校准**(见 `heat_pairs`)。"""
    from app.db.models import AiFilterDecision, DouyinLead

    cutoff = datetime.now() - timedelta(days=max(1, int(days)))
    rows = session.execute(select(AiFilterDecision.item_key, AiFilterDecision.score).where(
        AiFilterDecision.user_id == user_id,
        AiFilterDecision.section == section,
        AiFilterDecision.decided_at >= cutoff)).all()
    if not rows:
        return []
    keys = [str(r[0]) for r in rows]
    moved = set(session.scalars(select(DouyinLead.aweme_id).where(
        DouyinLead.user_id == user_id,
        DouyinLead.aweme_id.in_(keys),
        DouyinLead.our_url != "")).all())
    return [(int(r[1] or 0), 1 if str(r[0]) in moved else 0) for r in rows]


def choose_threshold(pairs: list[tuple[int, int]], *, min_keep_ratio: float = 0.2,
                     min_samples: int = 40) -> dict:
    """纯函数:从 `[(score, moved)]` 选阈值。样本不足 / 约束不可行时返回 `action=skip`。"""
    n = len(pairs)
    if n < int(min_samples):
        return {"action": "skip", "reason": f"样本不足({n}<{int(min_samples)})", "n": n}
    best: tuple | None = None
    for t in range(0, 11):
        kept = [m for s, m in pairs if s >= t]
        if not kept:
            continue
        ratio = len(kept) / n
        if ratio < float(min_keep_ratio):
            continue
        rate = sum(kept) / len(kept)
        cand = (rate, -t, t, ratio, len(kept))
        if best is None or cand > best:
            best = cand
    if best is None:
        return {"action": "skip", "reason": "没有满足保留比例的阈值", "n": n}
    rate, _neg_t, t, ratio, kept = best
    return {"action": "ok", "threshold": t, "rate": round(rate, 4),
            "keep_ratio": round(ratio, 4), "kept": kept, "n": n}


def calibrate(session, user_id: int, section: str, settings, *, days: int = 21,
              min_samples: int = 40, min_keep_ratio: float = 0.2,
              max_step: int = 1) -> dict:
    """算一次并**平滑写入** `system_config: ai_min_score:<section>`。"""
    from app.db.models import SystemConfig
    from app.services.ai_filter import _effective_min_score

    pairs = heat_pairs(session, user_id, section, days,
                       hot_min=int(getattr(settings, "ai_calibration_hot_share", 100) or 100))
    pick = choose_threshold(pairs, min_keep_ratio=min_keep_ratio,
                            min_samples=min_samples)
    if pick.get("action") != "ok":
        return {**pick, "section": section}
    cur = _effective_min_score(session, settings, section)
    target = int(pick["threshold"])
    new = max(0, min(10, cur + max(-int(max_step), min(int(max_step), target - cur))))
    key = f"ai_min_score:{section}"
    row = session.get(SystemConfig, key)
    if row is None:
        session.add(SystemConfig(key=key, value=str(new)))
    else:
        row.value = str(new)
    session.flush()
    return {**pick, "section": section, "old": cur, "new": new}


def backfill(session, user_id: int, settings, *, days: int = 21,
             batch: int = 40) -> dict:
    """**回填历史线索的 AI 分数**(只读打分,不改任何业务数据)。

    为什么值得花这点钱:校准需要「分数 ↔ 搬成」配对,而从今天开始积攒要等好几天;
    库里已有近一个月的线索与结果,补一次分数就能**立刻**得到第一条校准结论。
    ⚠️ 提示词里**不包含该条自己的结果**(否则就是拿答案喂模型)。
    """
    from app.db.models import DouyinLead
    from app.services import ai_filter, category_topics, llm_client, value_prior

    api_key = str(getattr(settings, "deepseek_api_key", "") or "")
    if not api_key:
        return {"action": "skip", "reason": "没有 deepseek_api_key"}
    cutoff = datetime.now() - timedelta(days=max(1, int(days)))
    rows = session.execute(select(
        DouyinLead.aweme_id, DouyinLead.title, DouyinLead.author,
        DouyinLead.share_count, DouyinLead.mark, DouyinLead.keyword).where(
        DouyinLead.user_id == user_id,
        DouyinLead.found_at >= cutoff,
        DouyinLead.mark != "")).all()
    if not rows:
        return {"action": "skip", "reason": "窗口内没有线索"}

    rates = value_prior.niche_rates(session, user_id, days=days)
    note = value_prior.prompt_note(rates)
    judged = 0
    for start in range(0, len(rows), max(1, int(batch))):
        chunk = rows[start:start + int(batch)]
        items = [{"aweme_id": r[0], "title": r[1] or "", "author": r[2] or "",
                  "share_count": int(r[3] or 0), "mark": r[4] or "",
                  "keyword": r[5] or ""} for r in chunk]
        texts = [
            f"{it['title']} 作者:{it['author']} 转发:{it['share_count']} "
            f"赛道:{category_topics.niche_of(it['title'] + ' ' + it['mark'])}"
            for it in items]
        payload = [{"i": i + 1, "text": t} for i, t in enumerate(texts)]
        got = llm_client.judge_value(
            str(getattr(settings, "deepseek_base_url", "https://api.deepseek.com")),
            api_key, str(getattr(settings, "deepseek_model", "deepseek-chat")),
            "抖音", payload, timeout=int(getattr(settings, "ai_filter_timeout", 45) or 45),
            extra=note)
        if got is None:
            logger.warning("回填:第 %d 批打分失败,停止(已判 %d 条)", start // int(batch) + 1, judged)
            break
        decisions = {int(d["i"]) - 1: d for d in got}
        ai_filter._record_decisions(session, user_id, "douyin_peer", items, texts,
                                   decisions, key_of=lambda it: it["aweme_id"])
        session.commit()
        judged += len(decisions)
    return {"action": "ok", "judged": judged, "leads": len(rows)}


def calibrate_all(settings=None, session=None) -> int:
    """作业入口:每个启用用户各校一次 `douyin_peer`。返回**调过的板块数**。

    每周一次即可:样本是滚动 21 天,分数变化慢。
    """
    from app.db import get_session_local
    from app.db.models import User
    from app.services.tenant_base import _record_run

    from config.settings import get_settings

    settings = settings or get_settings()
    if not bool(getattr(settings, "ai_calibration_enabled", True)):
        return 0
    own = session is None
    session = session or get_session_local()()
    changed = 0
    try:
        for (uid,) in session.execute(select(User.id).where(User.enabled.is_(True))).all():
            notes: list[str] = []
            for sec in ("douyin_peer",):
                try:
                    res = calibrate(
                        session, uid, sec, settings,
                        days=int(getattr(settings, "ai_calibration_days", 21) or 21),
                        min_samples=int(getattr(settings, "ai_calibration_min_samples", 40) or 40))
                    if res.get("action") == "ok":
                        changed += 1 if res.get("old") != res.get("new") else 0
                        notes.append(
                            f"{sec}:{res['old']}→{res['new']}"
                            f"(搬成{res['rate']:.0%} 留{res['keep_ratio']:.0%} n={res['n']})")
                    else:
                        notes.append(f"{sec}:跳过({res.get('reason')})")
                except Exception as exc:  # noqa: BLE001 - 单板块失败不影响其余
                    session.rollback()
                    logger.exception("AI 分数线校准失败 user=%s section=%s", uid, sec)
                    notes.append(f"{sec}:失败:{str(exc)[:60]}")
            _record_run(session, uid, "ai_calibrate", "success", " ; ".join(notes)[:900])
            session.commit()
    finally:
        if own:
            session.close()
    return changed


def ai_calibrate_tick(settings=None) -> int:
    """调度入口(薄壳,与其他作业的 `_tick` 命名一致)。"""
    return calibrate_all(settings)
