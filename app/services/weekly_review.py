# -*- coding: utf-8 -*-
"""选题复盘周报(v2.9.0):把一周的结算/共振数据变成"下周行动指南"。

**为什么**:扩散结算、共振金矿、品类风向分散在多个页面——运营没有"每周回看一次、
决定下周跟什么"的固定动作。本模块每周一自动汇总成一条可执行周报:

- 【执行概况】建议/已发/结算 + 平均扩散(来自 hotspot_suggestions);
- 【本周扩散榜】我发的哪条真有效(repost_gain 排序);
- 【同行共振金矿】对标号里多号同发的资源(需求被反复验证,下周可跟);
- 【品类风向】本周建议+同行文章的品类分布(复用 niche_fit 的验证品类词表);
- 【下周行动提示】规则化结论(共振最强的低竞争品类优先;扩散为 0 的降权观察)

纯规则+聚合(不调 LLM):稳、免费、可解释;推总群(与热榜卡同渠道)。
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import HotspotSuggestion, WechatArticle
from app.utils import get_logger

logger = get_logger(__name__)


def _category_of(text: str) -> str:
    from app.services.niche_fit import PROVEN_CATEGORIES

    blob = (text or "").lower()
    for cat, words in PROVEN_CATEGORIES.items():
        if any(w in blob for w in words):
            return cat
    return "未分类"


def build_weekly_review(session: Session, user_id: int, days: int = 7) -> str:
    """生成某用户的选题复盘周报文本。"""
    from app.services.resource_library import resonance_resources

    nl = chr(10)
    since = datetime.now() - timedelta(days=days)
    lines = [f"📊 选题复盘({since.strftime('%m-%d')} ~ {datetime.now().strftime('%m-%d')})", ""]

    # ① 执行概况 + 扩散榜(建议侧)
    sugs = session.scalars(select(HotspotSuggestion).where(
        HotspotSuggestion.user_id == user_id,
        HotspotSuggestion.created_at >= since)).all()
    acted = [s for s in sugs if s.acted]
    settled = [s for s in acted if s.settled_at is not None]
    gains = [int(s.repost_gain or 0) for s in settled]
    lines.append("【执行概况】")
    avg = f",平均扩散 +{sum(gains)/len(gains):.1f}" if gains else ""
    lines.append(f"建议 {len(sugs)} 条 · 已发 {len(acted)} 条 · 已结算 {len(settled)} 条{avg}")

    top = sorted(settled, key=lambda s: -(s.repost_gain or 0))[:5]
    if top:
        lines.append(nl + "【本周扩散榜】(发过的哪条真有效)")
        for i, s in enumerate(top, 1):
            lines.append(f"  {i}. {str(s.keyword)[:22]} → 盘链扩散 +{int(s.repost_gain or 0)}")
    else:
        lines.append(nl + "【本周扩散榜】暂无结算样本(发文后系统自动归因,次日 22:00 结算)")

    # ② 同行共振金矿(对标侧,有粒度)
    hot = resonance_resources(session, user_id, days=days, min_accounts=2, limit=8)
    if hot:
        lines.append(nl + "【同行共振金矿】(多号同发=需求已验证,下周可跟)")
        for r in hot[:6]:
            my = "已转存✓直接发" if r["my_link"] else "未转存"
            t0 = (r["titles"][0] if r["titles"] else "")[:26]
            lines.append(f"  ×{r['accounts']}号 {t0} | {my}")

    # ③ 品类风向(建议 + 同行文章)
    cats = Counter(_category_of(s.keyword or "") for s in sugs)
    arts = session.scalars(select(WechatArticle.title).where(
        WechatArticle.user_id == user_id, WechatArticle.created_at >= since)).all()
    peer = Counter(_category_of(t or "") for t in arts)
    if cats or peer:
        lines.append(nl + "【品类风向】(本周·建议 / 同行)")
        merge = {k: (cats.get(k, 0), peer.get(k, 0)) for k in set(cats) | set(peer)}
        for k, (a, b) in sorted(merge.items(), key=lambda kv: -(kv[1][0] + kv[1][1]))[:6]:
            lines.append(f"  {k}: 建议 {a} / 同行 {b}")

    # ④ 规则化行动提示
    lines.append(nl + "【下周行动提示】")
    if hot:
        cats_hot = Counter()
        for r in hot:
            cats_hot[_category_of((r["titles"] or [""])[0])] += 1
        best = cats_hot.most_common(1)
        if best and best[0][0] != "未分类":
            lines.append(f"  · 共振最强品类「{best[0][0]}」且量小竞争低,优先跟")
        unt = [r for r in hot if not r["my_link"]]
        if unt:
            lines.append(f"  · {len(unt)} 条金矿尚未转存——建议尽快转存,跟上扩散")
    zero = [s for s in settled if int(s.repost_gain or 0) == 0]
    if zero:
        lines.append(f"  · {len(zero)} 条已发建议扩散为 0——同形态下次降权观察")
    if not hot and not settled:
        lines.append("  · 本周样本少:先按「热点建议」页执行几条,系统自会积累复盘数据")
    lines.append(nl + "(口径:扩散=盘链全网转存增量;金矿=多号同发验证;全自动,无需人工录入)")
    # ⑤ **一句话结论放顶部**(2026-10-03 用户口径:"推送是为了用户更好总结")。
    # 周报本身就是总结,但**结论埋在最下面第四节** —— 提到标题下,一眼就能拿去用/转述。
    bits: list[str] = []
    if top:
        bits.append(f"扩散最好的是「{str(top[0].keyword or '')[:14]}」+{int(top[0].repost_gain or 0)}")
    elif settled:
        bits.append("已发建议本周扩散都为 0")
    else:
        bits.append("本周还没有结算样本")
    if hot:
        t0 = str((hot[0]["titles"] or [""])[0])[:14]
        unt = [r for r in hot if not r["my_link"]]
        bits.append(f"同行在猛推「{t0}」等 {len(hot)} 条" + (f",其中 {len(unt)} 条还没转存" if unt else ""))
    lines.insert(1, "👉 " + "；".join(bits) if bits else "👉 本周样本少,先按建议执行几条,系统自会积累复盘数据")
    return nl.join(lines)


def run_weekly_review_all_users(settings=None) -> int:
    """每周一 10:00 入口:给每个启用用户生成周报推总群。返回成功条数。"""
    from config.settings import get_settings
    from app.db import get_session_local
    from app.db.models import User
    from app.services.feishu_client import FeishuClient, webhook_for

    st = settings or get_settings()
    hook = webhook_for(st, "")
    if not hook:
        return 0
    db = get_session_local()()
    sent = 0
    try:
        for (uid,) in db.execute(select(User.id).where(User.enabled.is_(True))).all():
            try:
                text = build_weekly_review(db, uid)
                if FeishuClient(hook, st.feishu_secret).send(text[:3000]):
                    sent += 1
            except Exception:  # noqa: BLE001 - 单用户失败不影响其余
                logger.exception("选题复盘周报失败 user=%s", uid)
    finally:
        db.close()
    return sent
