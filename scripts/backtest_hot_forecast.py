# -*- coding: utf-8 -*-
"""回放:**「爆款预判」的趋势阈值该放宽到多少**(2026-11-25)。

回答一个问题 —— *`min_ratio=30` 是不是太严,严到连续多天零卡?*

## 为什么要有这个脚本

`hot_forecast` 上线后真实运行记录(11-24 两轮 + 11-25 早轮)全是
**「趋势确认 0 ⇒ 卡内 0」**。热点宝本身是通的(实测「麦穗与羊」仍能查出 6700 万),
所以零卡不是故障 —— 是**阈值把当天的候选全挡了**。

先手工核了两条被挡的:

    丑狐            2202 万 / 自身基线 1690 万 =  1.3 倍  ← 常驻热剧,挡得对
    我的有罪之人    3673 万 / 自身基线 2143 万 =  1.7 倍  ← 同上

⇒ 30 倍这把尺子确实挡的是**常驻剧**,不是"要炸"。但代价是**召回极低**:
11-25 那天 4 条形态候选、0 条过闸。用户 2026-11-25 决定「放宽阈值观察几天」。

## 这个脚本做什么

把最近 N 天**每天的形态候选**重跑一遍(和 `hot_forecast.detect` 的 ① 同一份判据),
对每个作品名去热点宝查一次历史日序列,然后**用同一份序列模拟多组阈值**,
输出每组的「命中次数 / 命中哪天 / 命中的是谁」,让人肉眼看:

- 放宽之后**多出来的是真作品还是噪音**(这一列必须逐条看,别只看数字);
- 30 倍挡掉的那批,在 10 倍 / 5 倍下会不会复活 —— 复活的是不是正是我们想要的。

## 怎么读输出

1. **`== 阈值档 ==`** 每档一行:命中天数、命中的 (日期, 作品名, 单日量, 倍数)。
2. **重点看「30 倍漏掉、10 倍捞回」那一节** —— 那才是放宽的收益。
3. 若捞回来的**全是常驻剧**(倍数低、单日量也低),说明 30 倍是对的,别改。

用法::

    python scripts/backtest_hot_forecast.py            # 默认 10 天
    python scripts/backtest_hot_forecast.py 20         # 换窗口

⚠️ **会出网**(只读热点宝,每天每个作品名一次);不写库、不发群、不转存。
⚠️ **纯读远程库**三源(`remote_candidates`),不改任何状态。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import get_settings                    # noqa: E402
from app.services import hot_forecast as hf                 # noqa: E402
from app.services.douhot_client import DouhotClient         # noqa: E402
from app.services.hot_cycle import read_cookie              # noqa: E402

#: 要试的阈值档:`(min_last, min_ratio, min_points)`。
#: `min_last` 保持 500 万 —— 回放里「谷雨」2828 万是常驻剧,降这条会把品类词放进来。
#: 变的只有倍数与"有量天数" —— 前者治常驻剧,后者治"一天蹿起来就没了"。
GRID = (
    ("现状 30x/2天", 5_000_000, 30.0, 2),
    ("20x/2天", 5_000_000, 20.0, 2),
    ("10x/2天", 5_000_000, 10.0, 2),
    ("10x/3天", 5_000_000, 10.0, 3),
    ("5x/2天", 5_000_000, 5.0, 2),
    ("5x/3天", 5_000_000, 5.0, 3),
)

#: 单日量下限也试一档更低的:有些真作品量级不到 500 万(如亚马尔澄清函)。
GRID_LOW = (
    ("10x/2天 · 200万", 2_000_000, 10.0, 2),
    ("5x/2天 · 200万", 2_000_000, 5.0, 2),
)


def _series_of(client: DouhotClient, seed: str) -> list[tuple[str, int]]:
    """`seed` 在热点宝里的日序列 `[(YYYYMMDD, value)]`,按日期升序。

    取词族里**末值最大**的那一支(与 `hot_forecast.douhot_confirm` 同一条口径:
    `麦穗与羊` / `麦穗与羊原视频` / `麦穗与羊原文` 说的是同一件事)。
    """
    try:
        raw = client.hot_search(limit=20, date_window=24, sub_type=3001, keyword=seed)
    except Exception as exc:  # noqa: BLE001 - 单条失败不挡整轮回放
        print(f"    ⚠️ 热点宝查询失败 {seed}: {type(exc).__name__}", file=sys.stderr)
        return []
    best: list[tuple[str, int]] = []
    for it in raw or []:
        word = str(it.get("key_word") or "").strip()
        if not word or word.strip("#") in hf.GENRE_STOP:
            continue
        if hf._cover(word, seed) < 0.99:
            continue
        vals = [(str(p.get("date") or ""), int(p["value"]))
                for p in it.get("trends") or [] if p.get("value") is not None]
        if len(vals) < 4:
            continue
        if not best or vals[-1][1] > best[-1][1]:
            best = vals
    return sorted(best)


def _evaluate(series: list[tuple[str, int]], cutoff: str,
              min_last: int, min_ratio: float, min_points: int) -> dict | None:
    """把阈值套到**截到 `cutoff` 为止**的序列上(与线上 `douhot_confirm` 逐字同口径)。

    ⚠️ `cutoff` 之后的数据必须切掉 —— 回放当天只能看到"昨天为止"的完整日,
    否则就是用未来数据判过去(那种回放必然 100% 准,但毫无意义)。
    """
    vals = [v for d, v in series if d <= cutoff]
    if len(vals) < 4:
        return None
    last, low = vals[-1], min(vals)
    if last < min_last:
        return None
    ratio = (last / low) if low > 0 else 0.0
    n_big = sum(1 for v in vals if v >= 1_000_000)
    if ratio < min_ratio or n_big < min_points:
        return None
    return {"last": last, "ratio": round(ratio, 1), "points": n_big}


def main(days: int = 10) -> None:
    settings = get_settings()
    cookie = read_cookie(settings)
    if not cookie:
        print("✗ 没配置热点宝 Cookie,无法回放(趋势确认这一级全靠它)")
        return
    client = DouhotClient(cookie, settings)

    #: ① 逐天重跑形态闸(与线上 detect 的 ① 同一份判据)
    today = datetime.now().replace(hour=8, minute=49, second=0, microsecond=0)
    per_day: list[tuple[datetime, list[str]]] = []
    cache: dict[str, list[tuple[str, int]]] = {}
    for i in range(days, 0, -1):
        now = today - timedelta(days=i - 1)
        cands = hf.remote_candidates(settings, now - timedelta(hours=24), now)
        seeds: set[str] = set()
        for c in cands:
            if not hf.FORM_RE.search(str(c.get("title") or "")):
                continue
            sd = hf._seed_of(str(c.get("title") or ""))
            if sd and sd not in hf.GENRE_STOP:
                seeds.add(sd)
        per_day.append((now, sorted(seeds)))
        print(f"{now.strftime('%m-%d')} 三源{len(cands):4d} 行 · 形态作品 {len(seeds)} 条"
              f" {sorted(seeds)}")

    #: ② 每个作品名查一次热点宝(去重后通常 40~50 个,可接受)
    all_seeds = sorted({s for _, ss in per_day for s in ss})
    print(f"\n查热点宝 {len(all_seeds)} 个作品名…")
    for i, seed in enumerate(all_seeds, 1):
        cache[seed] = _series_of(client, seed)
        if i % 10 == 0:
            print(f"  …{i}/{len(all_seeds)}", file=sys.stderr)

    #: ③ 每档阈值跑一遍
    for label, grid in (("== 基线档 ==", GRID), ("== 低量档 ==", GRID_LOW)):
        print(f"\n{label}")
        for name, min_last, min_ratio, min_points in grid:
            hits: list[str] = []
            for now, seeds in per_day:
                cutoff = (now - timedelta(days=1)).strftime("%Y%m%d")
                got = []
                for seed in seeds:
                    ev = _evaluate(cache.get(seed) or [], cutoff,
                                   min_last, min_ratio, min_points)
                    if ev:
                        got.append(f"{seed}({ev['last'] / 1e4:.0f}万/{ev['ratio']:.0f}x)"
                                   f"/{ev['points']}天")
                if got:
                    hits.append(f"    {now.strftime('%m-%d')}  {len(got)} 条:"
                                + " · ".join(got))
            print(f"  【{name}】命中 {len(hits)}/{len(per_day)} 天")
            for h in hits:
                print(h)

    #: ④ 专门列出「30 倍挡掉、放宽后捞回」的差集 —— 这才是放宽的收益,必须逐条看
    print("\n== 30x 漏掉、10x 捞回的那批(逐条看是不是真作品)==")
    seen: set[tuple[str, str]] = set()
    for now, seeds in per_day:
        cutoff = (now - timedelta(days=1)).strftime("%Y%m%d")
        for seed in seeds:
            ser = cache.get(seed) or []
            base = _evaluate(ser, cutoff, 5_000_000, 30.0, 2)
            loose = _evaluate(ser, cutoff, 5_000_000, 10.0, 2)
            if loose and not base:
                key = (now.strftime("%m-%d"), seed)
                if key in seen:
                    continue
                seen.add(key)
                print(f"    {key[0]}  {seed}  {loose['last'] / 1e4:.0f}万"
                      f" / {loose['ratio']:.0f}x / {loose['points']}天")
    if not seen:
        print("    (无)")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 10)
