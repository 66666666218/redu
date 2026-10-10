"""夸克盘清理**计划**(只读,2026-10-08)。**不删任何东西。**

判据与执行器共用一份(`app/services/quark_dup.py`)—— 那种"两处各写一遍、迟早飘"的事
本仓吃过大亏。**要真删请用 `scripts/quark_dup_apply.py --yes`。**

用法:`python scripts/quark_dup_report.py [10]`(参数 = 只清哪个 MMDD 前缀的账,默认 10 月)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ⚠️ **脚本自己定 stdout 编码,不依赖调用环境**:后台跑时 stdout 不是终端,默认编码是 GBK,
# 而本脚本要打印 ✔/✂ ⇒ `UnicodeEncodeError` 直接崩,而且崩在打印第一组时(前面几分钟的
# 扫描全白跑)。与 `pan_dedupe_report.py` 同一条教训。
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from app.services.quark_dup import build_plan  # noqa: E402
from app.services.quark_dup_cache import ScanCache  # noqa: E402


def _mb(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} MiB"


def main() -> int:
    from app.db import get_session_local
    from app.services.cookie_store import get_cookie
    from app.services.quark_transfer import QuarkTransfer
    from config.settings import get_settings

    s = get_settings()
    db = get_session_local()()
    try:
        ck = get_cookie(db, 1, "quark") or getattr(s, "quark_cookie", "") or ""
    finally:
        db.close()

    args = list(sys.argv[1:])
    # `--depth N`:扫几层。**默认 2**(家 → 资源目录 → 子目录 → 文件)。
    #
    # ⚠️⚠️ **`--depth 1` 不是"等价的快捷方式",它会漏扫**。同一个家实测:
    #      depth=1 → 491 个文件;depth=2 → 4953 个文件。**差 10 倍** ——
    #      因为资源多在 `家/资源名/子目录/文件` 这一层,只扫一级等于看不见它们。
    #      漏扫只会**漏删**(安全方向),但别指望它清得干净。
    #    ✅ **正确用法**:保持默认 depth=2,靠"多跑几轮 + 快照复用"收敛
    #      (实测一个家 728 次调用 ≈ 10.7 分钟,19 个家全扫完要 6~7 轮);
    #      或把 `QUARK_CLEANUP_BUDGET` 调大,一轮多扫几个家。
    depth = 2
    if "--depth" in args:
        i = args.index("--depth")
        depth = int(args[i + 1])
        del args[i:i + 2]
    prefix = next((a for a in args if a.isdigit()), "10")
    qt = QuarkTransfer(ck, fid_store=getattr(s, "quark_fid_store", "") or None)
    # ★ 扫描快照(**多轮收敛**的关键):扫完的家下一轮直接复用,没排上的家记下来接着扫。
    cache = ScanCache()
    out = build_plan(qt, mmdd_prefix=prefix, cache=cache, depth=depth)

    cs = cache.summary()
    print(f"扫了 {out['scanned_homes']} 个家、{out['scanned_files']} 个文件,"
          f"{out['calls']} 次 API 调用(depth={depth})"
          f"(本轮复用快照 {len(out.get('reused_homes') or [])} 个家 / 新扫 "
          f"{len(out.get('scanned_now') or [])} 个;快照共 {cs['homes']} 个家)")
    if out["remaining_homes"]:
        rem = out["remaining_homes"]
        part = out.get("partial_homes") or []
        print(f"   ⏳ **还差 {len(rem)} 个家没扫**(其中 {len(part)} 个扫了一半):"
              f"{'、'.join(rem[:6])}{'…' if len(rem) > 6 else ''}")
        if not out.get("scanned_now") and not out.get("reused_homes"):
            # ⚠️ **零进度必须说出来**:预算小到"一个家都扫不完"时,这一轮等于什么都没留下,
            #    而表面上它会打出一堆文件、看着像在干活(实测:预算 150 时收了 1763 个文件却零进度)。
            print("   ❌ **本轮零进度** —— 预算小到连一个家都扫不完,什么都没存进快照。")
            print("      请把 `QUARK_CLEANUP_BUDGET` 调大到**至少能扫完一个家**再跑,"
                  "否则重复多少轮都不会收敛。")
            print("      实测一个家的调用量:约 **728 次**(约 10.7 分钟)⇒ 预算至少设 **1000** 起步。")
            print("      ⚠️ **别用 `--depth 1` 来省事** —— 它会漏扫(实测同一家 491 vs 4953 个文件)。")
        else:
            print("      ⇒ 直接**再跑一次**即可接着扫(扫完的家会从快照复用,不会重扫);"
                  "全部扫完那一次 `complete` 才会是 True。")
    if not out["complete"]:
        print("   ⇒ ⚠️ 没扫完时「只出现在一个包里」这个判断是**假的**"
              "(没扫到的家里可能也有一份)⇒ **执行器默认拒绝删**。多跑几轮凑齐即可。")
    print(f"我方分享 {out['shares']} 条,保护了 {out['protected']} 个 fid"
          f"(这些**永不删**:删掉 = 已发出的链接变「已失效」)\n")

    by_why: dict[str, list] = {}
    for d in out["delete"]:
        by_why.setdefault(d["why"], []).append(d)
    if out["n_delete"]:
        print(f"=== ✂ 计划删除 {out['n_delete']} 个文件,约省 {out['freed'] / 2 ** 30:.2f} GiB ===")
        for why, items in sorted(by_why.items(), key=lambda kv: -len(kv[1])):
            print(f"\n--- {why}  ({len(items)} 个) ---")
            for d in items[:40]:
                print(f"   ✂ [{_mb(d['size']):>9}] {d['name'][:52]:52} ← {d['home']}")
            if len(items) > 40:
                print(f"   …… 还有 {len(items) - 40} 个")
    else:
        print("=== 没有要删的 ===")

    if out["review"]:
        print(f"\n=== ❓ 待确认 {len(out['review'])} 个(名字像引流,但只出现在一个包里)**不会删** ===")
        for r in out["review"][:40]:
            print(f"   ? [{_mb(r['size']):>9}] {r['name'][:52]:52} ← {r['home']}")
        if len(out["review"]) > 40:
            print(f"   …… 还有 {len(out['review']) - 40} 个")

    if out["protected_left"]:
        print(f"\n=== 🛡 被保护而跳过 {len(out['protected_left'])} 组 ===")
        for p in out["protected_left"][:20]:
            print(f"   🛡 {p['name'][:52]:52} ← {p['why']}")
        if len(out["protected_left"]) > 20:
            print(f"   …… 还有 {len(out['protected_left']) - 20} 组")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
