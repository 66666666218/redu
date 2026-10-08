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

    prefix = next((a for a in sys.argv[1:] if a.isdigit()), "10")
    qt = QuarkTransfer(ck, fid_store=getattr(s, "quark_fid_store", "") or None)
    out = build_plan(qt, mmdd_prefix=prefix)

    print(f"扫了 {out['scanned_homes']} 个家、{out['scanned_files']} 个文件,"
          f"{out['calls']} 次 API 调用"
          f"{'' if out['complete'] else ' —— ⚠️ **撞预算,没扫完**'}")
    if not out["complete"]:
        print("    ⇒ ⚠️ 没扫完时「只出现在一个包里」这个判断是**假的**"
              "(没扫到的家里可能也有一份)。抬 `QUARK_CLEANUP_BUDGET` 重跑。")
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
