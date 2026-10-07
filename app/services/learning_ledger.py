# -*- coding: utf-8 -*-
"""假设台账:把"我们相信什么"写成**可证伪的主张**,让数据每周自动判它一次(2026-10-07)。

## 为什么不能只是"再写一个分析脚本"
本项目已经有好几个分析模块(`chain_ordering` / `chain_delivery` / `agent_learning`),
但它们每跑一次都**从零推导**,结论**不留痕**。后果:

  · 同一件事每周重新算一遍,看不出"这个判断是**什么时候**变的";
  · 结论只活在**当次的卡片里**,翻完就没了 —— 下周没人记得"上周我们信的是另一套";
  · 更糟的是**看数的人的印象**会替代证据:用户问过一次"微博是不是更快",
    我当场算一遍说"是" —— 但那是一次性的,下次再问我还得重新算,而且记不得这次算过。

⇒ 本模块把主张**落库**(`system_config["learning_ledger"]`),每条带
**状态 / 样本数 / 判据 / 首次与最近评估时刻 / 状态变更历史**。
**只在状态变化时才值得说**(推送端照此做),其余时间它安静地积累。

## 三条纪律(都是本仓已经用血换来的)
1. **样本不足必须说"证据不足",不许下结论**(`ok=None`)。这是全模块存在的意义 ——
   一个会从 3 条样本里读出"规律"的学习器,比没有更糟。
2. **判据要写清"测的是什么机制"**,并检查**对照**:实测微博→抖音的中位滞后 47 小时,
   而两者**采集频率相同**(各约 46 次/天),所以那 47 小时不是"我们采样粗"造出来的。
   频率若不同,这个结论就是**假的** —— 见 `[证伪也要控制变量]` 那条教训。
3. **主张写在代码里,不自动生成**。自动造主张会造出一堆"看着像发现"的巧合;
   这条台账要的是**少而可复核**。
"""
from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.utils import get_logger

logger = get_logger(__name__)

#: 台账在 `system_config` 里的键(与 `agent_weights` / `agent_signal_stats` 同一存放习惯)
KEY = "learning_ledger"
#: 每条主张最多留多少条历史(够了;主要是看"什么时候变的")
HISTORY_MAX = 20

#: 状态三态。⚠️ `UNKNOWN` **不是错误**,是"数据还答不了" —— 它是最常见的合法状态。
OK, FAILED, UNKNOWN = "成立", "被证伪", "证据不足"


class Verdict:
    """一条主张这次评估的结果。"""

    def __init__(self, key: str, statement: str, ok: bool | None, n: int,
                 stat: str, judge: str, why: str, if_true: str, if_false: str) -> None:
        self.key = key
        self.statement = statement
        self.ok = ok
        self.n = int(n)
        self.stat = stat                 # 这次量到的数(人话)
        self.judge = judge               # 判据(成立的线画在哪)
        self.why = why                   # 它决定什么
        self.if_true = if_true
        self.if_false = if_false

    @property
    def status(self) -> str:
        return UNKNOWN if self.ok is None else (OK if self.ok else FAILED)

    def as_dict(self) -> dict:
        return {"key": self.key, "statement": self.statement, "status": self.status,
                "n": self.n, "stat": self.stat, "judge": self.judge}


# ══════════════════════════════════════════════════════════════════════════
# 主张的实现。**每条都必须自己判"样本够不够"**,不够就返回 ok=None。
# ══════════════════════════════════════════════════════════════════════════

def _hot_overlap(db: Session, uid: int) -> dict:
    """三个热榜里**同一归一化标题**各自最早出现时刻 → 谁最先、两两滞后多少。

    ⚠️ **必须同时输出"各表的采集频率"**:三个榜的采样密度不同时,
    "谁最先"量的其实是"谁的采集更密",整个结论作废。
    """
    from app.db.models import BaiduHotItem, DouhotWord, WeiboHotItem
    from app.services.events import normalize_title

    src = (("微博", WeiboHotItem, WeiboHotItem.captured_at),
           ("抖音", DouhotWord, DouhotWord.created_at),
           ("百度", BaiduHotItem, BaiduHotItem.captured_at))
    first: dict[tuple[str, str], datetime] = {}
    freq: dict[str, int] = {}
    for plat, model, tcol in src:
        rows = db.execute(select(model.title, tcol).where(
            model.user_id == uid, model.title.isnot(None), model.title != "")).all()
        stamps: set = set()
        for t, ts in rows:
            if ts is None:
                continue
            stamps.add(ts)
            nm = normalize_title(str(t))
            if len(nm) < 4:
                continue
            k = (plat, nm)
            if k not in first or ts < first[k]:
                first[k] = ts
        freq[plat] = len(stamps)          # 不同采集时刻数 = 采样密度
    by_norm: dict[str, dict[str, datetime]] = {}
    for (plat, nm), ts in first.items():
        by_norm.setdefault(nm, {})[plat] = ts
    return {"by_norm": by_norm, "freq": freq}


def _hypo_weibo_first(ctx: dict) -> Verdict:
    """用户口径:「一些大瓜从微博中可以更快获取到」—— 这条主张一直没被验过。"""
    ho = ctx.get("hot") or {}
    multi = {n: d for n, d in (ho.get("by_norm") or {}).items() if len(d) >= 2}
    freq = ho.get("freq") or {}
    firsts: dict[str, int] = {}
    lags: dict[str, list[float]] = {}
    for d in multi.values():
        order = sorted(d.items(), key=lambda kv: kv[1])
        firsts[order[0][0]] = firsts.get(order[0][0], 0) + 1
        for i in range(len(order)):
            for j in range(i + 1, len(order)):
                lags.setdefault(f"{order[i][0]}→{order[j][0]}", []).append(
                    (order[j][1] - order[i][1]).total_seconds() / 3600)
    n = len(multi)
    win = firsts.get("微博", 0)
    # ⚠️ **对照检查**:采样密度差一倍以上时,"谁最先"量的是采集频率而不是平台行为。
    ft, fb = freq.get("抖音"), freq.get("微博")
    if ft and fb and max(ft, fb) > 2 * min(ft, fb):
        return Verdict(
            "weibo_first", "微博比抖音/百度更早捕捉到同一热词",
            None, n, f"两榜采样密度差 >2×({freq})", "微博最先占比 ≥50%(需两榜采样密度相当)",
            "决定要不要把微博热榜当大瓜的早期预警面", "", "")
    med = statistics.median(lags["微博→抖音"]) if lags.get("微博→抖音") else None
    stat = (f"{win}/{n} 条微博最先" + (f",微博→抖音中位 {med:.0f}h" if med else ""))
    return Verdict(
        "weibo_first", "微博比抖音/百度更早捕捉到同一热词",
        (win / n >= 0.5) if n >= 20 else None, n, stat,
        "微博最先占比 ≥50%,且样本 ≥20 条(三榜采样密度需相当)",
        "决定要不要把微博热榜当大瓜的早期预警面",
        "把微博热榜当**早期预警**面用:热词刚冒头就取词,别等它到抖音",
        "微博并不更快 ⇒ 不必为它单独加密,维持现状即可")


def _hypo_douyin_first_resource(ctx: dict) -> Verdict:
    """抖音在**资源**层面是否先于公众号(与热词层面是两回事)。"""
    o = ctx.get("ordering") or {}
    groups = o.get("groups") or []
    pair = [g for g in groups if "抖音" in g["order"] and "公众号" in g["order"]]
    dy = sum(1 for g in pair if g["order"].index("抖音") < g["order"].index("公众号"))
    n = len(pair)
    stat = f"{dy}/{n} 条抖音先于公众号"
    return Verdict(
        "douyin_first_resource", "抖音先于公众号看到同一份资源",
        (dy / n >= 0.6) if n >= 8 else None, n, stat,
        "抖音先的占比 ≥60%,且样本 ≥8 份",
        "决定抖音这条链值不值得继续加投入(取词/搬运)",
        "抖音确实是资源的**首发面** ⇒ 保持/加强抖音侧",
        "抖音不比公众号早 ⇒ 它的价值在别处(素材/账号),别为'快'投钱")


def _hypo_push_fast(ctx: dict) -> Verdict:
    d = (ctx.get("delivery") or {}).get("chains", {}).get("公众号") or {}
    n = int(d.get("latency_n") or 0)
    h = d.get("ingest_to_push_h")
    stat = f"入库→推出中位 {h}h(样本 {n} 篇)" if h is not None else "还没有样本"
    return Verdict(
        "push_fast", "公众号一旦拿到资源,1 小时内能推出去",
        (h <= 1.0) if (h is not None and n >= 30) else None, n, stat,
        "中位 ≤1 小时,且样本 ≥30 篇",
        "决定「慢」到底慢在发现还是在推送",
        "慢只可能出在**发现**端 ⇒ 优化探测频率,别动推送",
        "推送这一段就是瓶颈 ⇒ 先查推送链路",)


def _hypo_move_rate(ctx: dict) -> Verdict:
    c = (ctx.get("delivery") or {}).get("chains") or {}
    parts, bad, n = [], [], 0
    for name in ("抖音", "迅雷群"):
        v = c.get(name) or {}
        if not v.get("seen"):
            continue
        n += int(v["seen"])
        parts.append(f"{name} {v['move_rate'] * 100:.0f}%")
        if v["move_rate"] < 0.7:
            bad.append(name)
    stat = "、".join(parts) or "还没有样本"
    return Verdict(
        "move_rate", "抖音/迅雷群 的搬成率都在 70% 以上",
        (not bad) if n >= 20 else None, n, stat,
        "两条链都 ≥70%,且合计样本 ≥20 条",
        "决定'看到了搬不回来'是不是真问题",
        "搬运这一段是通的 ⇒ 继续往'更快发现'上投",
        f"**{('/'.join(bad))} 搬成率不足** ⇒ 优先修搬运(token/空间/口令解析)")


def _hypo_no_broken_segment(ctx: dict) -> Verdict:
    d = ctx.get("delivery") or {}
    x = (d.get("current_24h") or {}).get("迅雷群") or {}
    n = int(x.get("seen") or 0)
    bad = int(x.get("moved") or 0)
    failed = n - bad
    stat = f"近 24h 迅雷群 {bad}/{n} 搬成" if n else "近 24h 没有样本"
    return Verdict(
        "no_broken_segment", "当前没有在坏的搬运段落(近 24h)",
        (failed / n <= 0.2) if n >= 5 else None, n, stat,
        "近 24h 失败率 ≤20%,且样本 ≥5 条 —— **只看当前**,不看窗口里的历史",
        "决定现在该不该停下手上的事去救火",
        "没有在烧的火 ⇒ 做长期优化,别乱动正在好的链路",
        "**有段落在坏** ⇒ 先救火(见交付看板的失败原因),占住这一周")


#: 主张清单(**写在代码里**,不自动生成 —— 见模块头纪律 3)。
#: 顺序 = 展示顺序:先"流"再"路"再"卫生"。
HYPOTHESES: tuple[Callable[[dict], Verdict], ...] = (
    _hypo_no_broken_segment,
    _hypo_weibo_first,
    _hypo_douyin_first_resource,
    _hypo_move_rate,
    _hypo_push_fast,
)


# ══════════════════════════════════════════════════════════════════════════

#: 组内最小两两相似度低于这个值 ⇒ 该组的**种子可能太泛**(形成一个"星形":很多不相干的东西
#: 都跟它像)。这**不是**误并的证据 —— 实测最低的那组(0.084,11 种写法)人工核过确实是同一份资源,
#: 它反映的是"抖音/公开平台的长标题写法差异大"。所以它只当**复核指针**,不当通过/不通过判据:
#: 拿它判"匹配质量"会把**真热门资源**一律误报(写法多),那正是我第一版犯的错。
REVIEW_SIM = 0.3


def review_flags(ctx: dict) -> list[str]:
    """需要**人工看一眼**的组(不是结论,是线索)。人看一条的成本极低,而误配的代价是错结论。"""
    groups = ((ctx.get("ordering") or {}).get("groups")) or []
    out = []
    for g in groups:
        if g.get("min_pair_sim", 1.0) < REVIEW_SIM:
            out.append(f"{g['name'][:40]}({g['members']} 种写法,最小相似 "
                       f"{g['min_pair_sim']})")
    return out


def build_context(db: Session, uid: int, days: int = 14) -> dict:
    """一次性备好各报告(**别让每条主张自己去查库重复算一遍**)。

    单项失败不影响其余:取不到的填 `{}`,对应主张自然落成"证据不足"。
    """
    ctx: dict[str, Any] = {}
    try:
        from app.services.chain_ordering import ordering_report

        ctx["ordering"] = ordering_report(db, uid, days=days)
    except Exception:  # noqa: BLE001
        logger.exception("学习台账:跨链台账取数失败(相关主张记证据不足)")
    try:
        from app.services.chain_delivery import delivery_report

        ctx["delivery"] = delivery_report(db, uid, days=days)
    except Exception:  # noqa: BLE001
        logger.exception("学习台账:交付看板取数失败(相关主张记证据不足)")
    try:
        ctx["hot"] = _hot_overlap(db, uid)
    except Exception:  # noqa: BLE001
        logger.exception("学习台账:热榜重叠取数失败(相关主张记证据不足)")
    return ctx


def _load_state(db: Session) -> dict:
    from app.db.models import SystemConfig

    row = db.scalar(select(SystemConfig).where(SystemConfig.key == KEY))
    if row and row.value:
        try:
            return json.loads(row.value)
        except (ValueError, TypeError):
            logger.warning("学习台账状态坏了,按空的重新起(历史会丢,但不是静默)")
    return {}


def _save_state(db: Session, state: dict) -> None:
    from app.db.models import SystemConfig

    blob = json.dumps(state, ensure_ascii=False)
    row = db.scalar(select(SystemConfig).where(SystemConfig.key == KEY))
    if row is None:
        db.add(SystemConfig(key=KEY, value=blob))
    else:
        row.value = blob
    db.commit()


def evaluate(db: Session, uid: int, days: int = 14,
             now: datetime | None = None) -> dict:
    """跑一轮评估 → `{"verdicts", "changes", "state"}`。**会写状态**(这是"学习"的载体)。

    `changes` 只含**状态真变了**的(证据不足→成立 也算变)——
    推送端只该说这些,否则每周把同一批结论念一遍,人会看瞎。
    """
    now = now or datetime.now()
    ctx = build_context(db, uid, days=days)
    state = _load_state(db)
    verdicts: list[Verdict] = []
    for fn in HYPOTHESES:
        try:
            verdicts.append(fn(ctx))
        except Exception:  # noqa: BLE001 - 一条主张算挂了,不拖垮其余
            logger.exception("学习台账:主张 %s 评估失败", getattr(fn, "__name__", "?"))
    changes: list[dict] = []
    for v in verdicts:
        prev = state.get(v.key) or {}
        old = prev.get("status")
        entry = {
            "status": v.status, "n": v.n, "stat": v.stat,
            "statement": v.statement, "updated": now.isoformat(" ", "seconds"),
            "since": prev.get("since") or now.isoformat(" ", "seconds"),
            "history": (prev.get("history") or [])[-HISTORY_MAX:],
        }
        if old != v.status:
            entry["since"] = now.isoformat(" ", "seconds")     # 状态变了,since 重开
            entry["history"] = entry["history"] + [{
                "at": now.isoformat(" ", "seconds"), "from": old or "(首次)", "to": v.status,
                "n": v.n, "stat": v.stat}]
            changes.append({"key": v.key, "statement": v.statement, "from": old, "to": v.status,
                            "n": v.n, "stat": v.stat,
                            "if_true": v.if_true, "if_false": v.if_false})
        state[v.key] = entry
    # 主张是**写在代码里**的,所以代码删掉一条时,库里那行会变成"永远不再更新的僵尸":
    # 它还留在报告中会让"证据不足"看起来像"还在攒" —— 其实那条主张已经没了。剔掉。
    live = {v.key for v in verdicts}
    for stale in [k for k in state if k not in live]:
        logger.info("学习台账:主张 %s 已从代码里下线,清掉它的历史", stale)
        state.pop(stale, None)
    _save_state(db, state)
    return {"verdicts": [v.as_dict() for v in verdicts], "changes": changes,
            "review": review_flags(ctx),
            "state": state, "ctx": {"freq": (ctx.get("hot") or {}).get("freq") or {}}}


def render_lines(result: dict) -> list[str]:
    """台账 → 可读几行。**每条都带样本数**,免得"成立"被当成"确定"。"""
    icon = {OK: "✅", FAILED: "❌", UNKNOWN: "⏳"}
    lines = ["=== 假设台账(数据每周自动判一次)==="]
    for v in result["verdicts"]:
        lines.append(f"  {icon.get(v['status'], '?')} {v['status']} | {v['statement']}"
                     f" —— {v['stat']}(n={v['n']};判据:{v['judge']})")
    freq = (result.get("ctx") or {}).get("freq") or {}
    if freq:
        lines.append(f"  ⚠️ 三榜采样密度(对照用,差 >2× 时'谁最先'就作废):"
                     f"{freq}")
    flags = result.get("review") or []
    if flags:
        lines.append(f"  👀 **待人工复核 {len(flags)} 组**(相似度低 ≠ 误并,但值得看一眼):"
                     + ";".join(flags[:3]))
    if result["changes"]:
        lines.append("  **本轮变化**:")
        for c in result["changes"]:
            lines.append(f"    · 「{c['statement']}」{c['from'] or '(首次)'} → "
                         f"**{c['to']}**({c['stat']})")
            act = c["if_true"] if c["to"] == OK else (c["if_false"] if c["to"] == FAILED else "")
            if act:
                lines.append(f"      ⇒ {act}")
    else:
        lines.append("  (本轮没有状态变化 —— 安静积累也是正常的,不必每周都有'发现')")
    lines.append("  ⚠️ 「成立」= **当前证据支持**,不等于「确定」;每条都带样本数与判据,自己看一眼。")
    return lines
