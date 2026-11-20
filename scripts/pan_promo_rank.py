# -*- coding: utf-8 -*-
"""**资源推广热度榜**（只读）：哪些内容有很多人在推、我们手上有没有。

## 回答的问题

「监控到这么多数据，能不能判断哪些信息值得做成网盘推广？」

## 判据（每一层都对着一次真实审计）

**身份 = 盘链 + 链内作品名**（复用 `push_surge` 的那两层，理由见其头注）：
一条盘链会被反复复用挂不同资源，只按链聚会把 A 的人算到 B 头上。

**推手数 = 不同作者数**，不是行数：行数会被我们自己（日结/补推/重试）污染。

**名字要先过 CTA 滤网，但只丢「纯套话」**：`链接自取可直接打开` `入口直达` `资料分享`
剥掉动作词后什么都不剩 ⇒ 丢。⚠️ 不能「命中动作词就丢」—— `七宗罪测试`(30 人)、
`犬系人格测试`(16 人) 含动作词却是**真资源**，一刀切会让它们整条消失。

**自有链 = 夸克 `pan_owned_shares`(6651) + 迅雷 `xunlei_group_shares.our_url`(2182)**。
同一作品既有自有链又有外部源链 = 我们已在做；只有外部源链 = **可搬**；
只有自有链 = 已在做但没有别人在推。⚠️ 只认夸克会把已搬过的迅雷链作品误报成可搬。

## ⚠️ 已知低估（别把「没上榜」读成「没人推」）

· `pan_id`（源链）近 30 天只有 155/7445 行(2.1%) ⇒ 身份基本靠 `pan_url`；
· 小红书/快手/贴吧/B站/知乎给的是**推广号自己建的链** ⇒ 每个号一条、聚不到一起，
  所以这几个板块的人数是**下界**（公众号那条链是我方转存链，反而数得准）；
· `《》` 在台账里只覆盖 22%，没有书名号的行退化成整条标题 ⇒ 同一作品的不同标题
  写法会被拆成两个桶；
· 打码作者名（快手 100%、抖音对标号 19.5%）会让同一个人被数成 2 个、或 2 个人
  被数成 1 个。

用法：``python scripts/pan_promo_rank.py [days] [min_hands]``
"""
from __future__ import annotations

import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta

os.environ.setdefault("PYTHONIOENCODING", "utf-8")

from sqlalchemy import or_, select, text                      # noqa: E402

from app.db import get_session_local                          # noqa: E402
from app.db.models import PushLedger                          # noqa: E402
from app.services.push_ledger import pan_key                  # noqa: E402
from app.services.push_surge import _bracket_names, _identity, _work_key  # noqa: E402

#: ⚠️ 套话滤网与源链识别**已挪到 `app.services.promo_forecast`**
#: (`is_cta` / `src_type`)—— 实时作业与这份只读清单必须用**同一套**判据,
#: 否则「榜单说可搬」和「预警报了」会分成两把尺子。判据原文与反例留在那边。
from app.services.promo_forecast import is_cta as _is_cta, src_type as _src_type  # noqa: E402

_NUM_RE = re.compile(r'"(?:play_count|liked_count|like_count|read_num|read_count)"\s*:\s*(\d+)')


def rank(session, *, days: int = 30, min_hands: int = 5,
         now: datetime | None = None) -> tuple[list[dict], dict]:
    """返回 `(按推手数降序的桶, 覆盖统计)`。**纯读**。"""
    now = now or datetime.now()
    start = now - timedelta(days=days)
    #: `pan_key -> 原链`：判断「这一行是不是我们自己的链」，并留着给人点开看。
    #: ⚠️ 自有链**不止夸克**：`pan_owned_shares` 只收夸克(6651)，而 `xunlei_group_shares`
    #: 里 status=ok 的 `our_url` 是我们转存后建出来的**迅雷**链(2182 条独立链)。
    #: 只认夸克会把「我们已有迅雷链」的作品误判成可搬 —— 实测差 5 个桶。
    owned: dict[str, str] = {}
    for (u,) in session.execute(
            text("select url from pan_owned_shares where platform='quark'")).all():
        k = pan_key(str(u or ""))
        if k:
            owned.setdefault(k, str(u))
    for (u,) in session.execute(text(
            "select our_url from xunlei_group_shares "
            "where status='ok' and our_url is not null and our_url<>''")).all():
        k = pan_key(str(u or ""))
        if k:
            owned.setdefault(k, str(u))

    rows = session.execute(
        select(PushLedger.user_id, PushLedger.pan_url, PushLedger.pan_id,
               PushLedger.section, PushLedger.author, PushLedger.title,
               PushLedger.pan_type, PushLedger.pushed_at, PushLedger.metrics)
        .where(PushLedger.pushed_at >= start,
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

    buckets: dict[tuple, dict] = defaultdict(
        lambda: {"hands": set(), "rows": 0, "our": 0, "src": defaultdict(int),
                 "secs": set(), "types": set(), "last": None, "pans": set(),
                 "plays": 0, "likes": 0, "src_urls": {}})
    n_no_author = 0
    for r in rows:
        pk = _identity(r)
        if not pk:
            continue
        who = str(r.author or "").strip()
        if not who:                        #: 数不出是谁 ⇒ 不能算一个推手，但要记账
            n_no_author += 1
            continue
        wk = _work_key(str(r.title or ""), names[(r.user_id, pk)])
        w = buckets[(r.user_id, wk)]
        w["hands"].add(who)
        w["rows"] += 1
        w["secs"].add(str(r.section or ""))
        w["types"].add(str(r.pan_type or ""))
        w["pans"].add(pk)
        w["last"] = r.pushed_at
        if pk in owned:
            w["our"] += 1
            w["own_url"] = owned[pk]
        else:
            t = _src_type(r.pan_url)
            if t:
                w["src"][t] += 1
                w["src_urls"].setdefault(t, str(r.pan_url or ""))
        m = _NUM_RE.search(str(r.metrics or ""))
        if m:
            key = "plays" if "play" in str(r.metrics) else "likes"
            w[key] += int(m.group(1))

    out: list[dict] = []
    for (uid, wk), w in buckets.items():
        n = len(w["hands"])
        if n < min_hands:
            continue
        out.append({
            "work": wk, "hands": n, "rows": w["rows"],
            "cta": _is_cta(wk), "own": w["our"] > 0,
            "own_url": w.get("own_url", ""),
            "sources": dict(w["src"]), "src_urls": dict(w["src_urls"]),
            "sections": sorted(w["secs"]),
            "pan_types": sorted(x for x in w["types"] if x),
            "pans": len(w["pans"]), "last": w["last"],
            "plays": w["plays"], "likes": w["likes"],
        })
    out.sort(key=lambda x: (-x["hands"], -(x["last"] or now).timestamp()))
    stats = {"rows": len(rows), "buckets": len(buckets), "no_author": n_no_author,
             "pans": len({_identity(r) for r in rows if _identity(r)}),
             "owned_pans": len(owned)}
    return out, stats


def render(items: list[dict], stats: dict, days: int) -> None:
    real = [x for x in items if not x["cta"]]
    print(f"窗口 {days} 天 · 带盘行 {stats['rows']} · 盘链 {stats['pans']} · "
          f"作品桶 {stats['buckets']} · 无作者行 {stats['no_author']}")
    print(f"≥ 门槛的桶 {len(items)} 个（真名 {len(real)} / CTA 套话 {len(items) - len(real)}）")
    for t in (5, 8, 10, 15, 20):
        sel = [x for x in real if x["hands"] >= t]
        miss = [x for x in sel if not x["own"]]
        print(f"   ≥{t:2d} 人: {len(sel):3d} 个 · 已有自有链 {len(sel) - len(miss):3d}"
              f" · **缺链可搬** {len(miss):3d}")
    print()
    print("%-3s %-4s %-4s %-11s %-26s %-6s %s" %
          ("人", "链", "板块", "源", "作品", "末见", "状态"))
    for x in real[:40]:
        src = "/".join(f"{k}{v}" for k, v in sorted(x["sources"].items())) or "无"
        state = "已有自有链" if x["own"] else ("可搬" if x["sources"] else "无源链")
        print("%-3d %-4d %-4d %-11s %-26s %-6s %s" % (
            x["hands"], x["pans"], len(x["sections"]), src[:11],
            x["work"][:26], (x["last"] or datetime.now()).strftime("%m-%d"), state))

    #: ★ 可执行的那一半:别人在推、我们**没有**自有链、且带着能直接转存的源链。
    #: 只列一条源链(每种盘型一条)——不必把同一作品的 20 条链都打出来。
    can = [x for x in real if not x["own"] and x["src_urls"]]
    print(f"\n=== 缺链可搬 {len(can)} 个（别人在推、我们没有自有链）")
    for x in can[:25]:
        print("%3d人 %-24s %s" % (x["hands"], x["work"][:24],
                                  next(iter(x["src_urls"].values()))[:90]))


def main() -> None:
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    min_hands = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    session = get_session_local()()
    try:
        items, stats = rank(session, days=days, min_hands=min_hands)
        render(items, stats, days)
    finally:
        session.close()


if __name__ == "__main__":
    main()