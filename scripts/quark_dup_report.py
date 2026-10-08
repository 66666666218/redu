"""夸克盘**跨目录重复资源**只读报告(2026-10-08)。**不删任何东西。**

判据与执行器共用一份(`app/services/quark_dup.py`)—— 那种"两处各写一遍、迟早飘"的事
本仓吃过大亏,这里不再来一次。

用法:`python scripts/quark_dup_report.py [10]`(参数 = 只清哪个 MMDD 前缀的账,默认 10 月)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ⚠️ **脚本自己定 stdout 编码,不依赖调用环境**:后台跑时 stdout 不是终端,
# 默认编码是 GBK,而本脚本要打印 ✔/✂ ⇒ `UnicodeEncodeError` 直接崩,
# 而且崩在打印第一组时(前面几分钟的扫描全白跑)。与 `pan_dedupe_report.py` 同一条教训。
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

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
          f"{'' if out['complete'] else ' —— ⚠️ **撞预算,没扫完,下面的清单不完整**'}")
    print(f"我方分享 {out['shares']} 条,保护了 {out['protected']} 个 fid"
          f"(这些**永不删**:删掉 = 已发出的链接变「已失效」)\n")

    if out["n_drop"]:
        print(f"=== ✅ 可清理:{out['groups']} 组、{out['n_drop']} 个副本、"
              f"约省 **{out['freed'] / 2 ** 30:.2f} GiB** ===")
        for p in out["plan"]:
            print(f"\n ✂ {p['name'][:64]}  ({_mb(p['size'])} × {len(p['drops'])} 份)")
            print(f"   ✔ 留 {p['keep']['home']}/{p['keep']['path'].split('/', 1)[-1][:40]}")
            for d in p["drops"]:
                print(f"   ✂ 删 {d['home']}/{d['path'].split('/', 1)[-1][:40]}")
    else:
        print("=== 没有可清理的重复 ===")

    if out["skipped"]:
        print(f"\n=== ⏭ 跳过 {len(out['skipped'])} 组(没得删) ===")
        for k in out["skipped"][:15]:
            print(f"   {k['name'][:50]:50} {_mb(k['size']):>10}  ← {k['why']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
