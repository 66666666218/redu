"""**受控探针**:大瓜那 7 个词到底有没有货(2026-11-20)。

## 它回答什么问题

「大瓜监控」近 10 天 12 轮 37 条线索里,**真正的大瓜 0 条**。查下来一个更基础的事实:
`_DA_GUA_WORDS` 的 7 个词**有 5 个从来没被搜过一次**(近 4 轮热榜词占满 8 个名额,
大瓜词被挤出窗口)。所以"大瓜搜不到"这件事**从来没有被验证过** ——
之前的结论都是拿「热榜 + 一句歌词 + 八卦泛词」的成绩去评价的。

这个脚本把 7 个大瓜词**单独、一次性**搜一遍,量两个**必须分开**的数:

    · `够格` = 带口令(《》/夸克令牌)或直贴网盘链 —— 这是**能进台账**的门槛
    · `像瓜` = 标题语义上真的是吃瓜(剥掉搜索词本身再判)—— 这是**内容**门槛

## ⚠️⚠️ 只量第一个会得出**假绿灯**(2026-11-20 实际栽过)

第一版判据只看「回来多少条带口令的」,跑出「4/7 有货」——**是错的**。
那 4 个词召回的是《复仇者联盟:毁灭日》《后会友期》《点点芬香》这类
**影视/游戏宣传顺带的口令**,不是吃瓜资源。加 `--raw`(不过口令漏斗)才看清真相:

    | 词     | 原始 | 标题像瓜 |     | 词     | 原始 | 标题像瓜 |
    |--------|------|----------|     |--------|------|----------|
    | 吃瓜群 | 254  | **31**   |     | 反差   | 16   | **0**    |
    | 塌房   | 16   | 5        |     | 爆料   | 16   | **0**    |
    | 吃瓜   | 16   | 2        |     | 小作文 | 5    | **0**    |
    | 实锤   | 15   | 1        |     | 合计   | 338  | 39       |

**词内对照(不是跨词比条数)**:`吃瓜群` 254 条里 31 条是吃瓜,但过了口令漏斗的
10 条**全是**短剧/游戏/郭静的歌 ⇒ **交集为 0**。那 31 条的形态是
`谁有吃瓜群啊拉我进去呗` / `来自@群聊·八卦视频群` / `🍉群拉到17群啦`
—— **是站内群邀请,不是网盘**。而 `_drop_panless` 明确「群口令不算」。
⇒ 抖音这条链上,**吃瓜内容不带盘**;这不是词表组装问题。

## 纪律

· **纯读 + 只出网**:不写 `douyin_leads`、不打 `fed_search_at` 戳、**不推送、不发任何群**;
· 花的是**抖音搜索额度**(一次一轮的量,8 个词) —— 这是这条链唯一要花的东西;
· 失败不吞:取数层硬失败会直接抛出来(协议+浏览器都挂了 / 登录态失效)。

用法:
    python scripts/probe_dagua_words.py             # 默认 7 个词,过口令漏斗
    python scripts/probe_dagua_words.py 吃瓜群 塌房  # 只跑指定词
    python scripts/probe_dagua_words.py --raw       # **不过漏斗**,看原始标题(判"有没有内容"用这个)
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")


#: 「这条标题像不像**吃瓜内容**」—— 判的是标题语义,不是「带不带口令」。
#: ⚠️ 必须在**剥掉搜索词本身之后**再判:否则搜「爆料」时每条都命中「爆料」,
#: 判据就变成恒真(本仓母题:恒真的判据 = 空转的绿灯)。
#: 实测反例:《复仇者联盟:毁灭日》史上最大爆料 —— 是**影视宣传**,不是吃瓜资源。
_GUA_RE = re.compile(
    r"吃瓜|大瓜|瓜田|塌房|实锤|黑料|内幕|绯闻|恋情|出轨|劈腿|分手|离婚|复合"
    r"|官宣|聊天记录|小作文|反转|爱豆|娱乐圈|内娱|爱豆|idol|明星八卦")


def _looks_gua(title: str, word: str) -> bool:
    return bool(_GUA_RE.search(str(title or "").replace(word, "")))


def _raw(argv: list[str]) -> int:
    """`--raw`:直读取数层,**不过口令漏斗**,把每条标题都打出来。

    ## 为什么要单开这个模式

    主模式量的是「带口令的吃瓜资源有多少」,但它有一个读不出来的盲区:
    **0 条可能是"根本没有吃瓜内容",也可能是"有吃瓜内容但不带盘"**。
    这两种结论指向完全不同的动作(砍赛道 vs 松漏斗),不能让它们长得一样。

    ⚠️ 这里**只读不判**:`_collect` 不做新鲜度过滤、不写库、不推送。
    """
    from app.db.database import get_session_local
    from app.services.douyin_leads import _DA_GUA_WORDS, _collect
    from config.settings import get_settings

    words = argv or list(_DA_GUA_WORDS)
    st = get_settings()
    print(f"raw 模式(不过口令漏斗)· 词 ({len(words)}): {words}\n")
    tot = tot_gua = 0
    with get_session_local()() as s:
        for w in words:
            try:
                rows, source = _collect("douyin", [w], session=s, user_id=1,
                                        pages=int(getattr(st, "douyin_leads_pages", 1) or 1))
            except Exception as exc:  # noqa: BLE001
                print(f"✗ {w!r} 取数失败: {type(exc).__name__}: {exc}")
                continue
            n_gua = sum(1 for r in rows if _looks_gua(r.get("snippet") or "", w))
            tot += len(rows)
            tot_gua += n_gua
            print(f"[{w}] 原始 {len(rows)} 条 · 来源 {source or '-'} · **标题像瓜 {n_gua} 条**")
            for r in rows:
                t = str(r.get("snippet") or "").replace(chr(10), " ")
                tag = "瓜" if _looks_gua(t, w) else "—"
                print(f"  ({tag}) {t[:110]}")
            print()
    print("=" * 78)
    print(f"raw 合计 {tot} 条,其中标题像瓜 {tot_gua} 条")
    if tot_gua:
        print("  ⇒ 抖音**有**吃瓜内容 ⇒ 0 产出是漏斗问题(内容不带盘 / 口令识别不到),不是没内容。")
    else:
        print("  ⇒ 标题里连吃瓜内容都没有 ⇒ 这几个词在抖音不指向吃瓜,换词或砍赛道。")
    return 0


def main() -> int:
    from app.db.database import get_session_local
    from app.services.douyin_leads import _DA_GUA_WORDS, collect_leads
    from config.settings import get_settings

    words = sys.argv[1:] or list(_DA_GUA_WORDS)
    st = get_settings()
    print(f"探针词 ({len(words)}): {words}")
    print(f"新鲜度窗口: push_fresh_days={getattr(st, 'push_fresh_days', '?')} "
          f"| 每词页数={getattr(st, 'douyin_leads_pages', 1)}\n")

    total_raw = total_kept = total_pan = total_gua = 0
    per_word: list[dict] = []
    with get_session_local()() as s:
        for w in words:
            stats: dict = {}
            try:
                leads, source = collect_leads(
                    [w], platform="douyin", session=s, user_id=1,
                    pages=int(getattr(st, "douyin_leads_pages", 1) or 1),
                    stats=stats)
            except Exception as exc:  # noqa: BLE001 - 取数硬失败要让人看见,不是"0 条"
                print(f"✗ {w!r} 取数失败: {type(exc).__name__}: {exc}")
                per_word.append({"word": w, "source": "失败", "raw": 0, "kept": 0,
                                 "pan": 0, "gua": 0, "leads": []})
                continue
            n_mark = sum(1 for x in leads if (x.get("mark") or "").strip())
            n_pan = sum(1 for x in leads if (x.get("pan_url") or "").strip())
            #: ★ **两条判据分开数**:`够格` = 带口令/盘链(进得了台账);
            #: `像瓜的` = 标题语义上真的是吃瓜(去掉了搜索词本身再判)。
            #: 只看前者会把「《复仇者联盟:毁灭日》史上最大爆料」算成吃瓜有货。
            n_gua = sum(1 for x in leads if _looks_gua(x.get("title") or "", w))
            per_word.append({"word": w, "source": source or "-", "raw": int(stats.get("raw") or 0),
                             "kept": len(leads), "pan": n_pan, "gua": n_gua, "leads": leads})
            total_raw += int(stats.get("raw") or 0)
            total_kept += len(leads)
            total_pan += n_pan
            total_gua += n_gua
            print(f"· {w:8s} 来源={source or '-':10s} 原始{stats.get('raw', 0):4d} "
                  f"过新鲜度{stats.get('kept', 0):3d} 口令{n_mark:3d} 直链{n_pan:3d} "
                  f"**像瓜{n_gua:3d}** "
                  f"(挡:旧{stats.get('old', 0)} 无时{stats.get('notime', 0)} 续{stats.get('cont', 0)})")

    #: ★ **13 条全打,不截断** —— 只看 4 条样例会把"影视宣发混在里头"读成"吃瓜有货"。
    print("\n" + "=" * 78)
    print("全部够格线索(逐条,`瓜` = 标题语义像吃瓜):\n")
    for r in per_word:
        if not r["leads"]:
            continue
        print(f"[{r['word']}] {len(r['leads'])} 条 · 来源 {r['source']}")
        for x in r["leads"]:
            tag = "瓜" if _looks_gua(x.get("title") or "", r["word"]) else "—"
            ident = x.get("mark") or x.get("pan_url") or "-"
            print(f"  ({tag}) [{ident}] {str(x.get('title') or '')[:100]}")
        print()

    print("=" * 78)
    print(f"合计:原始 {total_raw} 条 → 够格(带口令/盘链) {total_kept} 条,"
          f"直贴盘链 {total_pan} 条,**像瓜的 {total_gua} 条**")
    ok = [r for r in per_word if r["kept"] > 0]
    gua = [r for r in per_word if r["gua"] > 0]
    print(f"够格的词 {len(ok)}/{len(per_word)}: {[r['word'] for r in ok] or '无'}")
    print(f"像瓜的词 {len(gua)}/{len(per_word)}: {[r['word'] for r in gua] or '无'}")
    print("\n判读:")
    if not gua:
        print("  ⇒ **有口令不等于有吃瓜资源** —— 搜回来的都是影视/游戏宣传的顺带口令。")
        print("    这条赛道在抖音这几个泛词上不成立:带口令的帖子有,但**吃瓜内容不带盘**。")
        print("    ⚠️ 跑 `--raw` 复核一遍:0 像瓜可能是「根本没有」,也可能是「有但不带盘」,")
        print("       这两种结论指向完全不同的动作(砍赛道 vs 松漏斗),别让它们长得一样。")
    elif not ok:
        print("  ⇒ 像瓜的帖**不带口令/盘链** ⇒ 进不了客户端漏斗,这条线按原目标不可行。")
    else:
        print("  ⇒ 真吃瓜资源确实存在(见上面标 `(瓜)` 的行),此前是词表组装问题。")
        print("    修 `search_keywords` 大瓜分支:给 `_DA_GUA_WORDS` 保底名额即可。")
    return 0


if __name__ == "__main__":
    if "--raw" in sys.argv[1:]:
        raise SystemExit(_raw([a for a in sys.argv[1:] if a != "--raw"]))
    raise SystemExit(main())