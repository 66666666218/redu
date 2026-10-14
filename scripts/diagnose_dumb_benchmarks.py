"""把**哑号**(既无 `biz` 也无 `weread_book_id` 的对标号)分类:到底是**作品被封**,还是**号没有了**。

## 为什么要这条诊断(2026-10-14 用户口径)
按名解析修好之后,仍有 51 个哑号**搜不到精确同名**。但"搜不到"是**一句话盖了三件事**:
  · 号**改名**了(名字对不上,但号还在 —— 这种**能救**);
  · 号/内容**被封**(有明确的页面原因);
  · 号**从来就不存在**(搜狗候选名本来就不对)。
不问清就一律当"救不回来",等于把**能救的那批**一起埋掉 —— 本仓最贵的那类错。

## 判据(全部要**证据**,不许猜)
| 证据 | 结论 |
| --- | --- |
| 它历史文章页上的**当前号名**与库里不同 | **改名**(可用现名重解 id ⇒ 能救) |
| 文章页命中 `_detect_ban_reason` 的封文标记 | **内容/账号被封**(带违规类别) |
| 有历史文章但正文抓不到 | 抓不到(证据不足,不猜是哪种) |
| **一篇文章都没抓到过** | 无法证明该号存在过(可能名字就不对) |

## 跑法
    python scripts/diagnose_dumb_benchmarks.py                  # 只报告(默认)
    python scripts/diagnose_dumb_benchmarks.py --limit 5        # 先看几条
    python scripts/diagnose_dumb_benchmarks.py --apply          # 顺手把"改名"那批用现名救回来

⚠️ `--apply` **只做一件事**:把改名的号**改成现名**并重解 id(旧名记进 note);封号/无内容的一律不动。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import select  # noqa: E402

from app.db import get_session_local  # noqa: E402
from app.db.models import WechatArticle, WechatBenchmark  # noqa: E402


def _norm(text: str) -> str:
    from app.services.wechat._source import _norm_mp_name

    return _norm_mp_name(text or "")


def _search_shape(session, name: str) -> tuple[str, str]:
    """后台按名搜这个号 —— **形态本身就是证据**:
      · 精确命中 ⇒ 名字没变(那它早该被救回来,属于漏网);
      · **零命中** ⇒ 微信侧**完全没有这个名字**(改名/注销/被封到查不到);
      · 只有形近号 ⇒ 名字接近的号还在(把形近名打出来,让人看是不是改了名)。
    """
    from app.services.wemp_cred import load as _load
    from app.services.wechat.wemp_client import WempClient, fakeid_to_mp_id

    cred = _load(session, 1) or {}
    if not cred.get("cookie") or not cred.get("token"):
        return "没凭据", ""
    try:
        raw = WempClient(cred["cookie"], cred["token"]).search_biz(name, limit=10)
    except Exception as exc:  # noqa: BLE001 - 查不动也要如实说,不静默
        return f"查不动({type(exc).__name__})", ""
    same = [h for h in raw if _norm(h["nickname"]) == _norm(name)]
    if same:
        return "精确命中", fakeid_to_mp_id(same[0]["fakeid"])
    if raw:
        return "只有形近号", "、".join(h["nickname"][:12] for h in raw[:4])
    return "零命中(微信侧没有这个名字)", ""


def classify(wm, session, row: WechatBenchmark) -> tuple[str, str, str]:
    """返回 `(类别, 证据/现名, 建议动作)`。**先看历史文章(能否证伪/证实),再看搜索形态。**"""
    shape, detail = _search_shape(session, row.nickname)
    art = session.scalars(select(WechatArticle).where(
        WechatArticle.user_id == row.user_id,
        WechatArticle.author == row.nickname,
    ).order_by(WechatArticle.created_at.desc()).limit(1)).all()
    if art:
        url = str(art[0].url or "")
        if url:
            body = wm.fetch_article_content(url) or ""
            reason = wm._detect_ban_reason(body)
            cur = str((wm.extract_article_meta(url) or {}).get("name") or "").strip()
            if reason:
                return ("内容/账号被封", f"文章页给出:{reason}", "**不动**(被封是既定事实)")
            if cur and _norm(cur) != _norm(row.nickname):
                return ("改名(有铁证)", f"旧文现名={cur}", "**可用现名重解 id(能救)**")
            if body:
                return ("号还在(旧文可取)", f"现名={cur or '(未取到)'};搜索:{shape}", "看看能不能绕过命名差异")
    # 没有历史文章 ⇒ 只能靠搜索形态
    if shape == "精确命中":
        return ("漏网(精确命中)", detail, "**直接补 id 即可**")
    if shape == "只有形近号":
        return ("疑似改名/同名相近", detail, "人工对照形近名,别自动猜")
    if shape.startswith("零命中"):
        return ("微信侧查无此名", "搜索零命中 + 从无历史文章", "**建议停用**(再重试也不会有结果)")
    return ("无法判断", f"搜索:{shape}", "证据不足")



def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--user", type=int, default=1)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--sleep", type=float, default=1.5, help="每条之间停多久(秒)")
    ap.add_argument("--apply", action="store_true", help="把「改名」那批用现名救回来")
    args = ap.parse_args()

    from app.services import wechat_monitor as wm      # 门面(子模块直接 import 会循环)
    import app.services.wechat._source as src

    db = get_session_local()()
    try:
        rows = list(db.scalars(select(WechatBenchmark).where(
            WechatBenchmark.user_id == args.user,
            (WechatBenchmark.biz == "") | (WechatBenchmark.biz.is_(None)),
            (WechatBenchmark.weread_book_id == "") | (WechatBenchmark.weread_book_id.is_(None)),
        ).order_by(WechatBenchmark.id)))
        todo = rows[:args.limit] if args.limit else rows
        print(f"哑号 {len(rows)} 个,本轮诊断 {len(todo)} 个({'只报告' if not args.apply else '并救改名那批'})")
        tally: dict[str, int] = {}
        for i, row in enumerate(todo, 1):
            kind, ev, action = classify(wm, db, row)
            tally[kind] = tally.get(kind, 0) + 1
            print(f"  [{i}/{len(todo)}] {kind:<12} {row.nickname!r:<26} {ev[:58]}")
            if args.apply and kind == "改名":
                new = ev.split("=", 1)[1].strip()
                biz, why = src._biz_from_wemp(db, args.user, new)
                if biz:
                    row.note = f"{(row.note or '')} 旧名:{row.nickname}"[:255]
                    row.nickname = new[:128]
                    row.biz = biz[:64]
                    row.weread_book_id = biz[:64]
                    db.commit()
                    print(f"        → ✅ 已按现名救回:{biz}")
                else:
                    print(f"        → ⛔ 按现名也没拿到:{why[:50]}")
            if i < len(todo):
                time.sleep(max(0.0, args.sleep))
        print("\n分类小结:")
        for k, n in sorted(tally.items(), key=lambda kv: -kv[1]):
            print(f"  {k}:{n}")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
