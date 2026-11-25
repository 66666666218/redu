# -*- coding: utf-8 -*-
"""回放:**「又火了」的分板块门槛该定多少**(2026-11-25)。

回答一个问题 —— *`push_hot_rise_pct` 一个全局比例,对每个板块都合适吗?*

## 为什么要有这个脚本

2026-11-19 上线 `hot_rise_pct`(涨 30% 才判"又火了")时,用的是 7 天台账的
**分板块涨幅直方图**:bilibili 99 次"涨了"全在 10% 以内、贴吧 32 次没一次超 50%。
结论「30% 能挡掉噪音」是对的,**但没回答"30% 在小量级板块上意味着什么"**。

30 天 8553 行按生产口径重放(盘链身份优先、`item_key` 兜底)后发现:
各板块的热度口径**量级差 4 个数量级** —— `kuaishou` 播放中位 12.7 万,
`tieba` 回复中位 **2**。在 2 的基础上"涨 30%" = 多一个回复,那不是资源火了。

⇒ 判据从**比例**换成**绝对增量**(`push_ledger.HOT_RISE_MIN_ABS`)。

## ⚠️ 为什么不是"把比例抬高"

同批数据里 `277239 → 497704` 只涨 **80%**,增量却有 **22 万**,是 bilibili 排前几的
真爆。把 `hot_rise_pct` 抬到 1.0 会把它和 `tieba 2→3` 一起挡掉 —— 比例这把尺子
在量级不同的板块之间**没有共同的判据**。

## 怎么读输出

1. **各板块涨幅分位数**:看 30% 落在哪个位置(中位数以下 = 门槛松到没意义)。
2. **过 30% 那批的绝对增量分档**:找到"噪音消失、真爆还在"的那个断点。
3. **门槛试算**:改 `--gate 板块:数值` 复跑,看「多挡多少行」与「切掉的样本」——
   被切掉的**必须全是小量级**,被保留的**必须都像真爆**。逐条看,别只看数字。

用法::

    python scripts/backtest_hot_rise.py                # 默认 30 天,打印标定依据
    python scripts/backtest_hot_rise.py 60             # 换窗口
    python scripts/backtest_hot_rise.py 30 --gate xiaohongshu:800 --gate wechat:100

⚠️ **纯读库**,不发群、不转存、不写任何东西。
⚠️ 台账的 `metrics` 是**当时推出去那一刻**的快照;回放算的是"两次推送之间涨了多少",
   不是"资源最终有多热"—— 后者要等观察期走完,那是 `backtest_promo_forecast` 的事。
"""
from __future__ import annotations

import collections
import json
import os
import sys
from datetime import datetime, timedelta

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.database import get_session_local          # noqa: E402
from app.db.models import PushLedger                   # noqa: E402
from app.services.push_ledger import (                 # noqa: E402
    HOT_RISE_MIN_ABS, pan_key, peak_metric)

#: 涨幅分位(与 `push_hot_rise_pct` 的候选档位对齐)
PCTS = (0.30, 0.50, 1.00, 2.00)
#: 绝对增量分档(跨 4 个数量级 —— 各板块的热度口径本来就差这么多)
BANDS = ((0, 5), (5, 20), (20, 100), (100, 500), (500, 2000),
         (2000, 10000), (10000, 50000), (50000, 10 ** 18))


def _peak_of(raw) -> float:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except ValueError:
            return 0.0
    return peak_metric(raw) if isinstance(raw, dict) else 0.0


def _pairs(session, days: int):
    """按生产口径把台账还原成 `(section, prev_hot, hot)` —— 只保留"重复"的那些。

    ⚠️ 顺序**必须**与 `drop_recent` 一致:`pan_id` 优先、空则回落 `pan_key(pan_url)`,
    内容身份(`item_key`)与盘链身份**各记一份**,命中任一就算重复。
    这两把尺子混着用过一次,数就差了一大截(只看 item_key 会漏掉"换篇文章推同一份资源")。
    """
    since = datetime.now() - timedelta(days=days)
    rows = (session.query(PushLedger.section, PushLedger.item_key, PushLedger.pan_url,
                          PushLedger.pan_id, PushLedger.metrics, PushLedger.pushed_at,
                          PushLedger.title)
            .filter(PushLedger.pushed_at >= since)
            .order_by(PushLedger.pushed_at.asc(), PushLedger.id.asc()).all())
    state: dict[str, dict] = collections.defaultdict(lambda: {"items": {}, "pans": {}})
    out: dict[str, list[tuple[float, float, str]]] = collections.defaultdict(list)
    for sec, key, pu, pid, m, _t, title in rows:
        pk = (pid or "").strip() or pan_key(pu or "")
        hot = _peak_of(m)
        st = state[sec]
        dup_item = bool(key) and key in st["items"]
        dup_pan = bool(pk) and pk in st["pans"]
        if dup_item or dup_pan:
            #: ⚠️ `dup_item` 优先 —— 与 `drop_recent` 里那一段**逐字一致**,
            #: 否则"同一条内容又推一遍"和"同一份资源换了篇文章"会取到不同的基准。
            prev = st["items"].get(key, 0.0) if dup_item else st["pans"].get(pk, 0.0)
            if prev > 0 and hot > 0:
                out[sec].append((prev, hot, title or ""))
        if key:
            st["items"][key] = hot
        if pk:
            st["pans"][pk] = hot
    return out


def _report_gates(pairs, days: int) -> None:
    print(f"\n=== 涨幅分位数({days} 天,各板块『涨了』的样本)===")
    print(f"{'section':12s} {'n':>6s} " + " ".join(f"≥{p:.0%}".rjust(8) for p in PCTS))
    for sec in sorted(pairs):
        ps = [(h - p) / p for p, h, _ in pairs[sec] if h > p]
        if not ps:
            continue
        n = len(ps)
        cells = " ".join(f"{sum(1 for x in ps if x >= c) / n:>8.1%}" for c in PCTS)
        print(f"{sec:12s} {n:6d} {cells}")

    print(f"\n=== 『过 30%』那批按**绝对增量**分档(占比)—— 断点就在这里 ===")
    lbl = ["0-5", "5-20", "20-100", "100-500", "500-2k", "2k-1万", "1万-5万", ">5万"]
    print(f"{'section':12s} {'n':>5s} " + " ".join(f"{x:>8s}" for x in lbl))
    for sec in sorted(pairs):
        ss = [(h - p, title) for p, h, title in pairs[sec]
              if h > p and (h - p) / p >= 0.30]
        if not ss:
            continue
        n = len(ss)
        cells = [f"{sum(1 for d, _ in ss if a <= d < b) / n:>8.1%}" for a, b in BANDS]
        print(f"{sec:12s} {n:5d} " + " ".join(cells))


def _report_cut(pairs, gates: dict[str, int], days: int) -> None:
    """门槛试算:**被切掉的必须是噪音,被保留的必须像真爆**。逐条看,别只看数字。"""
    print(f"\n=== 门槛试算({days} 天)—— 增量 >= 门槛 才放行 ===")
    print(f"{'section':12s} {'过30%':>6s} {'门槛':>6s} {'保留':>5s} {'切掉':>5s} | 切掉的最大几条")
    for sec in sorted(pairs):
        ss = [(h - p, title) for p, h, title in pairs[sec]
              if h > p and (h - p) / p >= 0.30]
        if not ss:
            continue
        gate = int(gates.get(sec, 0))
        kept = [x for x in ss if x[0] >= gate]
        cut = sorted([x for x in ss if x[0] < gate], key=lambda x: -x[0])[:4]
        ex = "; ".join(f"+{int(d)} {t[:14]}" for d, t in cut) or "—"
        print(f"{sec:12s} {len(ss):6d} {gate:6d} {len(kept):5d} {len(cut) and len(ss)-len(kept):5d} | {ex}")

    print("\n=== 保留里**增量最小**的几条(它们必须是真爆,不是噪音)===")
    for sec in sorted(pairs):
        ss = [(h - p, title) for p, h, title in pairs[sec]
              if h > p and (h - p) / p >= 0.30]
        gate = int(gates.get(sec, 0))
        kept = sorted([x for x in ss if x[0] >= gate], key=lambda x: x[0])[:4]
        if not kept:
            continue
        ex = "; ".join(f"+{int(d)} {t[:14]}" for d, t in kept)
        print(f"{sec:12s} | {ex}")


def main(argv: list[str]) -> int:
    days = 30
    gates = dict(HOT_RISE_MIN_ABS)
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--gate" and i + 1 < len(argv):
            name, _, val = argv[i + 1].partition(":")
            if not name or not val:
                print(f"❌ --gate 的格式是 `板块:数值`,收到 {argv[i + 1]!r}")
                return 2
            try:
                gates[name.strip()] = int(float(val))
            except ValueError:
                print(f"❌ --gate 的值解析不出数字:{argv[i + 1]!r}")
                return 2
            i += 2
            continue
        try:
            days = int(a)
        except ValueError:
            print(f"❌ 认不出参数 {a!r}(用法: backtest_hot_rise.py [天数] [--gate 板块:数值])")
            return 2
        i += 1

    session = get_session_local()()
    try:
        pairs = _pairs(session, days)
    finally:
        session.close()
    total = sum(len(v) for v in pairs.values())
    if not total:
        print(f"近 {days} 天台账里没有可配对的重复样本 —— 换个更长的窗口试试")
        return 0
    print(f"近 {days} 天台账,可配对(重复推送)样本 {total} 条,覆盖 {len(pairs)} 个板块")
    _report_gates(pairs, days)
    _report_cut(pairs, gates, days)
    print("\n⚠️ 改门槛就改 `push_ledger.HOT_RISE_MIN_ABS`(带数据出处),"
          "或临时用 `.env` 的 `push_hot_rise_min_abs=板块:数值,...` 覆盖。")
    print("⚠️ 这张表的每一档都要能回答『为什么不是相邻那一档』 —— 答不上就别改。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
