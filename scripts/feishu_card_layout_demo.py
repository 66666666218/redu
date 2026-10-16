"""把**抖音同行作品卡的两种版式**用**真实数据**渲染出来,发到管理群让人挑。

用户口径(2026-10-16):「第一个可以先发到管理群给我看一下两个的具体格式我再选择」。

两种版式的差别只有一处:**「对标作品」那一列点开去哪** (其余列两边都用真数据):

    方案A(用户这次描述的)  「对标作品」→ **原视频**;网盘链另占「可用链」列
    方案B(2026-10-06 定的) 「作品」    → **我方网盘链**;原视频在「▶视频」列

两种都带**发布时间标记**(用户已定:标时间 + 按新→旧排序)。

用法:
    python scripts/feishu_card_layout_demo.py              # 只打印(默认)
    python scripts/feishu_card_layout_demo.py --send       # 发管理群
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from app.services.feishu._cards import _col_set_row, _md_safe, strip_others  # noqa: E402


def _rows(n: int = 5) -> list[dict]:
    """取最近几条**真的搬成了**的抖音线索(带发布时间的优先)。"""
    con = sqlite3.connect("data/platform.db")
    out = []
    for title, author, url, our, mark, sc, pub in con.execute(
            "select title, coalesce(author,''), url, our_url, coalesce(mark,''), "
            "coalesce(share_count,0), publish_at from douyin_leads "
            "where coalesce(our_url,'') <> '' order by publish_at desc nulls last, id desc "
            "limit ?", (n,)):
        out.append({"title": title, "author": author, "url": url, "our": our,
                    "mark": mark, "share": sc, "pub": pub})
    con.close()
    return out


def _when(pub: str) -> str:
    """发布时间标记:`一个钟内` 用 🆕,其余给 `·MM-DD HH:MM`(用户口径:一眼看出"刚出的")。"""
    from datetime import datetime

    if not pub:
        return ""
    try:
        dt = datetime.fromisoformat(str(pub)[:19])
    except ValueError:
        return ""
    return "🆕" if (datetime.now() - dt).total_seconds() <= 3600 else f"·{dt:%m-%d %H:%M}"


def _cells(r: dict) -> dict:
    from app.services.douyin_leads import _pan_name

    title = _md_safe(strip_others(r["title"] or ""))
    shown = title[:20] + ("…" if len(title) > 20 else "")
    when = _when(r["pub"])
    pan = _pan_name(r["our"] or "")
    return {
        "author": _md_safe(r["author"]) or "—",
        "work_txt": f"{shown} {when}".strip(),
        "work_orig": f"[{shown} {when}]({_md_safe(r['url'] or '')})" if r["url"] else shown,
        "work_pan": f"[{shown} {when}]({_md_safe(r['our'] or '')})" if r["our"] else shown,
        "video": f"[▶视频]({_md_safe(r['url'] or '')})" if r["url"] else "—",
        "res": _md_safe(r["mark"]) or "—",
        "data": f"↗{r['share']}" if r["share"] else "—",
        "link": f"[🔴{pan}]({_md_safe(r['our'] or '')})" if pan else "—",
        "pan": f"🔴{pan}" if pan else "—",
    }


def _hdr(t: str) -> dict:
    return {"tag": "div", "text": {"tag": "lark_md", "content": t}}


def card_a(rows: list[dict]) -> dict:
    """方案A:**对标账号 / 对标作品(→原视频) / 对标数据 / 资源 / 可用链(→网盘)**"""
    els = [_hdr("**方案A** —— 「对标作品」点开是**原视频**,网盘链单独一列(你这次描述的顺序)"),
           _col_set_row([("**对标账号**", 3), ("**对标作品**", 5), ("**对标数据**", 2),
                         ("**资源**", 3), ("**可用链**", 2)], grey=True)]
    for r in rows:
        c = _cells(r)
        els.append(_col_set_row([(c["author"], 3), (c["work_orig"], 5), (c["data"], 2),
                                 (c["res"], 3), (c["link"], 2)]))
    return {"config": {"wide_screen_mode": True},
            "header": {"template": "turquoise",
                       "title": {"tag": "plain_text", "content": "🅰️ 抖音同行卡 · 方案A(作品→原视频)"}},
            "elements": els}


def card_b(rows: list[dict], *, flex_mode: str = "none") -> dict:
    """方案B(现状口径):**作者 / 作品(→网盘) / 资源 / ▶视频(→原视频) / 转发数 / 条数**

    `flex_mode="flow"` = **手机友好版**:列宽按内容、放不下就换行(窄屏不再把 6 列挤成
    每格两三个字)。同一份数据两种渲染,发群里让人**在手机上比**。
    """
    tag = "🅱️" if flex_mode == "none" else "📱"
    note = ("**方案B** —— 「作品」点开是**我方网盘链**(2026-10-06 你定的口径),原视频在「▶视频」"
            if flex_mode == "none" else
            "**方案B · 手机版** —— 数据一样,只把**固定分栏换成流式换行**(窄屏不再挤成一列几个字)")
    els = [_hdr(note),
           _col_set_row([("**作者**", 3), ("**作品**", 5), ("**资源**", 2),
                         ("**▶视频**", 2), ("**转发数**", 2), ("**条数**", 1)],
                        grey=True, flex_mode=flex_mode)]
    for r in rows:
        c = _cells(r)
        els.append(_col_set_row([(c["author"], 3), (c["work_pan"], 5), (c["pan"], 2),
                                 (c["video"], 2), (c["data"], 2), ("1", 1)],
                                flex_mode=flex_mode))
    return {"config": {"wide_screen_mode": True},
            "header": {"template": "blue" if flex_mode == "none" else "green",
                       "title": {"tag": "plain_text",
                                 "content": f"{tag} 抖音同行卡 · 方案B"
                                            + ("(固定分栏)" if flex_mode == "none"
                                               else "(流式·手机友好)")}},
            "elements": els}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--send", action="store_true", help="真发管理群(默认只打印)")
    ap.add_argument("--mode", choices=("layout", "mobile"), default="layout",
                    help="layout=版式A/B对照(默认);mobile=手机端:固定分栏 vs 流式换行")
    ap.add_argument("--n", type=int, default=5)
    a = ap.parse_args()
    rows = _rows(a.n)
    if not rows:
        print("❌ 库里没有「搬成了」的抖音线索(our_url 非空)")
        return 2
    if a.mode == "mobile":
        ca, cb = card_b(rows), card_b(rows, flex_mode="flow")
    else:
        ca, cb = card_a(rows), card_b(rows)
    if not a.send:
        print(json.dumps({"A": ca, "B": cb}, ensure_ascii=False, indent=1)[:1200])
        print(f"\n[dry-run] 用 {len(rows)} 条真实线索渲染了两张;加 --send 发管理群。")
        return 0
    from config.settings import get_settings

    from app.services.feishu_client import FeishuClient, webhook_for

    st = get_settings()
    c = FeishuClient(webhook_for(st, "admin"), getattr(st, "feishu_secret", ""))
    if a.mode == "mobile":
        c.send("📱 **手机端对照**(数据是真的,取自最近搬成的 "
               f"{len(rows)} 条线索)\n"
               "两张**数据完全一样**,只有**排版**不同:\n"
               "· 第一张 = 现在的**固定分栏**(6 列按权重切) —— 手机上每格只剩两三个字\n"
               "· 第二张 = 改成**流式换行**(放不下就整块换行) —— 手机上应该好读很多\n"
               "⚠️ 请在**手机上**看这两张,告诉我「手机版要不要用流式」;"
               "PC 侧流式会少一点表格感,也可以只给手机用。")
    else:
        c.send("🔧 **抖音同行卡 · 两种版式请你挑一个**(数据是真的,取自最近搬成的 "
               f"{len(rows)} 条线索)\n"
               "两版**只有一处不同**:「对标作品」那列点开去哪。\n"
               "⚠️ 两版都带**发布时间标记**(🆕=一小时内,其余 `·MM-DD HH:MM`)且**按新→旧排序**。")
    ok_a = c.send_card(ca)
    ok_b = c.send_card(cb)
    print("已发:", "A", ok_a, "B", ok_b)
    return 0 if (ok_a and ok_b) else 1


if __name__ == "__main__":
    raise SystemExit(main())
