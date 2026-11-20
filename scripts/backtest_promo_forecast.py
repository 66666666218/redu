# -*- coding: utf-8 -*-
"""回测：**能不能用起势早期的信号，提前锁定爆款并自建链**（2026-11-20）。

用户口径：「能否总结然后找寻规律自己主动发现爆款然后改写成自建链」。

本脚本用**真实台账回放**回答三件事，每一件都是可被推翻的量化结论：

## ① 早窗口的推手数，能不能预测「最终会爆」？

桶 = (用户, 链内作品名)，身份口径与 `pan_promo_rank` / `push_surge` **同一份**。
**只评估首次出现 ≥7 天前的桶** —— 否则"后来涨没涨"根本无从观测(右删失)。
爆款定义：观察期内的**不同作者总数 ≥ `--hit`**(默认 10)。

输出：基线精确率 vs 各规则精确率/召回率/提升倍数。

## ② 「有规律」到底是什么

对比爆款桶与非爆款桶在**早窗口**（默认 24h）里的形状差异：
跨几个板块、标题写法是否多样、是否带书名号、源链在哪个盘、作者名是否打码。

## ③ 真正**值得动手**的量有多大

早窗口规则只是预测层。要落成「搬一条」，还得同时满足：
我们**当时还没有**自有链、且那条源链在**早窗口内**就已出现。
（晚了才出现的源链，在决策那一刻并不存在。）

用法：``python scripts/backtest_promo_forecast.py [days] [early_hours] [hit]``

⚠️ 这是**离线回测**，纯读库、不发任何群、不转存。
⚠️ 所有数的已知低估边界见 `pan_promo_rank.py` 头注（`pan_id` 覆盖 2.1%、
各板块自建链聚不到一起、「没上榜」≠「没人推」）。
"""
from __future__ import annotations

import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta

os.environ.setdefault("PYTHONIOENCODING", "utf-8")

from sqlalchemy import or_, select, text                       # noqa: E402

from app.db import get_session_local                           # noqa: E402
from app.db.models import PushLedger                           # noqa: E402
from app.services.push_ledger import pan_key                   # noqa: E402
from app.services.push_surge import _bracket_names, _identity, _work_key  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pan_promo_rank import _is_cta, _src_type                  # noqa: E402

DIAG_DAYS = 7            # 至少观察这么久，否则「没爆」可能只是没看够
RULES = (2, 3, 4, 5, 6, 8)


def _owned_keys(session) -> dict[str, datetime]:
    """自有链身份 → **最早拿到它的时刻**。

    ⚠️ 必须带时刻，不能只用一个 set：回测问的是「**当时**我们有没有」。
    拿今天的自有链清单去判 30 天前的决策，会把「窗口之后才搬的」也算成当时已有
    ⇒ ③ 的可动手量被系统性**低估**（前视偏差,前视偏差让数字变小反而容易被当成"保守"放过）。
    夸克用 `pan_owned_shares.created_at`、迅雷用 `xunlei_group_shares.moved_at`。
    """
    from app.db.models import PanOwnedShare, XunleiGroupShare

    out: dict[str, datetime] = {}
    #: ⚠️ 走 ORM 而不是 `text()`：raw 查询在 sqlite 上回的是**字符串**，
    #: 直接和 `datetime` 比会抛 `TypeError`（本脚本第一版就栽在这）。
    for u, ts in session.execute(
            select(PanOwnedShare.url, PanOwnedShare.created_at)
            .where(PanOwnedShare.platform == "quark")).all():
        k = pan_key(str(u or ""))
        if k and ts is not None and (k not in out or ts < out[k]):
            out[k] = ts
    for u, ts in session.execute(
            select(XunleiGroupShare.our_url, XunleiGroupShare.moved_at)
            .where(XunleiGroupShare.status == "ok",
                   XunleiGroupShare.our_url.is_not(None),
                   XunleiGroupShare.our_url != "")).all():
        k = pan_key(str(u or ""))
        if k and ts is not None and (k not in out or ts < out[k]):
            out[k] = ts
    return out


def _load(session, days: int):
    now = datetime.now()
    rows = session.execute(
        select(PushLedger.user_id, PushLedger.pan_url, PushLedger.pan_id,
               PushLedger.section, PushLedger.author, PushLedger.title,
               PushLedger.pan_type, PushLedger.pushed_at)
        .where(PushLedger.pushed_at >= now - timedelta(days=days),
               or_(PushLedger.pan_url != "", PushLedger.pan_id != ""))
        .order_by(PushLedger.pushed_at.asc())
    ).all()

    #: 每个(用户, 盘链)的《…》语料 —— 与 `push_surge.detect_surges` 同一做法
    names: dict[tuple, list[str]] = defaultdict(list)
    for r in rows:
        pk = _identity(r)
        if not pk:
            continue
        bucket = names[(r.user_id, pk)]
        for n in _bracket_names(str(r.title or "")):
            if n not in bucket:
                bucket.append(n)
    return now, rows, names


def _bucketize(rows, names, early_hours: float, owned: dict[str, datetime]) -> dict:
    """台账行 → `(用户, 作品名) -> 早期形状 + 全程事实`。"""
    out: dict[tuple, dict] = defaultdict(lambda: {
        "first": None, "early": set(), "all": set(), "early_sections": set(),
        "early_bracketed": False, "early_src": None, "early_masked": False,
        "early_own": False, "titles": set()})
    for r in rows:
        pk = _identity(r)
        who = str(r.author or "").strip()
        if not pk or not who:
            continue
        wk = _work_key(str(r.title or ""), names[(r.user_id, pk)])
        b = out[(r.user_id, wk)]
        b["first"] = b["first"] or r.pushed_at
        b["all"].add(who)
        b["titles"].add(str(r.title or ""))
        if (r.pushed_at - b["first"]).total_seconds() > early_hours * 3600:
            continue
        b["early"].add(who)
        b["early_sections"].add(str(r.section or ""))
        if "*" in who:                     # 打码作者名 = 快手/抖音对标号那一类
            b["early_masked"] = True
        if _bracket_names(str(r.title or "")):
            b["early_bracketed"] = True
        #: ⚠️ **自有链要按时刻判**：只认「在早窗口结束前就已拿到」才算当时有。
        own_ts = owned.get(pk)
        if own_ts is not None and own_ts <= b["first"] + timedelta(hours=early_hours):
            b["early_own"] = True
        elif b["early_src"] is None and _src_type(r.pan_url):
            b["early_src"] = (str(r.pan_url or ""), _src_type(r.pan_url))
    return out


def report_rule(buckets: dict, now: datetime, early_hours: float, hit: int) -> None:
    ev = [(k, b) for k, b in buckets.items()
          if b["first"] <= now - timedelta(days=DIAG_DAYS) and not _is_cta(k[1])]
    hits = [(k, b) for k, b in ev if len(b["all"]) >= hit]
    base = len(hits) / max(1, len(ev))
    print(f"\n=== ① 早窗口规则（可评估 {len(ev)} 桶 · 爆款≥{hit}人 {len(hits)} 个"
          f" · 基线精确率 {base:.1%}）")
    print(f"{'规则':<18}{'候选':>7}{'真爆款':>8}{'精确率':>9}{'召回':>8}{'提升':>8}")
    for k in RULES:
        sel = [(kk, b) for kk, b in ev if len(b["early"]) >= k]
        tp = sum(1 for kk, b in sel if len(b["all"]) >= hit)
        prec = tp / len(sel) if sel else 0
        rec = tp / len(hits) if hits else 0
        print(f"早{early_hours:g}h ≥{k:<2d}人{'':<7}{len(sel):>7}{tp:>8}{prec:>8.1%}"
              f"{rec:>8.1%}{(prec / base if base else 0):>7.1f}x")

    print("\n=== ② 爆款与非爆款的形状差异（早窗口内）")
    miss = [(k, b) for k, b in ev if len(b["all"]) < hit]

    def rate(rows_, fn):
        return sum(1 for _k, b in rows_ if fn(b)) / max(1, len(rows_))

    feats = {
        "跨 ≥3 板块": lambda b: len(b["early_sections"]) >= 3,
        "跨 ≥2 板块": lambda b: len(b["early_sections"]) >= 2,
        "标题带《》": lambda b: b["early_bracketed"],
        "多种标题写法": lambda b: len(b["titles"]) >= 2,
        "作者名被打码": lambda b: b["early_masked"],
        "早窗口有迅雷源链": lambda b: bool(b["early_src"] and b["early_src"][1] == "迅雷"),
        "早窗口有夸克源链": lambda b: bool(b["early_src"] and b["early_src"][1] == "夸克"),
    }
    print(f"{'特征':<22}{'爆款':>8}{'非爆款':>9}{'倍数':>7}")
    for lab, fn in feats.items():
        h, m = rate(hits, fn), rate(miss, fn)
        print(f"{lab:<22}{h:>7.0%}{m:>9.0%}{(h / m if m else float('inf')):>6.1f}x")


def report_actionable(buckets: dict, now: datetime, early_hours: float, days: int) -> None:
    """③ 真正能动手的量：早窗口达标 + 当时没有自有链 + 当时就有可转存源链。"""
    for k in RULES:
        ev = [(kk, b) for kk, b in buckets.items()
              if len(b["early"]) >= k and not _is_cta(kk[1])
              and b["first"] <= now - timedelta(days=1)]
        act = [(kk, b) for kk, b in ev if not b["early_own"] and b["early_src"]]
        if k == 4:
            print(f"\n=== ③ 可动手量（早{early_hours:g}h ≥{k}人）")
            print(f"候选 {len(ev)} 个（{len(ev)/days:.2f}/天）"
                  f" · 当时无自有链 {sum(1 for _k, b in ev if not b['early_own'])}"
                  f" · **可直接搬 {len(act)} 个 = {len(act)/days:.2f} 条/天**")
            c = Counter(b["early_src"][1] for _k, b in act)
            print(f"   盘型分布：{dict(c)}")
            for (uid, wk), b in sorted(act, key=lambda kv: -len(kv[1]["early"]))[:20]:
                print(f"   {len(b['early']):2d}人 {b['early_src'][1]:<3} "
                      f"{wk[:22]:<24} {b['early_src'][0][:70]}")
        else:
            print(f"    早{early_hours:g}h ≥{k}人 → 可直接搬 {len(act)} 个"
                  f"（{len(act)/days:.2f} 条/天）")


def main() -> None:
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    early_hours = float(sys.argv[2]) if len(sys.argv) > 2 else 24
    hit = int(sys.argv[3]) if len(sys.argv) > 3 else 10
    session = get_session_local()()
    try:
        now, rows, names = _load(session, days)
        owned = _owned_keys(session)
        buckets = _bucketize(rows, names, early_hours, owned)
        print(f"窗口 {days} 天 · 带盘行 {len(rows)} · 作品桶 {len(buckets)}"
              f" · 自有链身份 {len(owned)}")
        report_rule(buckets, now, early_hours, hit)
        report_actionable(buckets, now, early_hours, days)
    finally:
        session.close()


if __name__ == "__main__":
    main()