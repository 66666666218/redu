"""夸克**整包重复**的识别与清理(2026-10-16)。

    python scripts/quark_pkg_dedupe.py            # 只报告(默认)
    python scripts/quark_pkg_dedupe.py --yes      # 真删(进**回收站**,可捞回)

## 它治的是什么

**同一个「家」下面的 `X` 与 `X(1)`/`X(2)` 并存** —— 夸克遇到重名**目录**会加后缀,
一条分享链被转存两次,整个资源包就多一份。实测快照里 **36 组 / 1984 文件 / 23.30 GiB**。

⚠️ 这与 `quark_dup` 那条**互补**:那条抓**跨家**的同名同大小文件;整包重复的目录名不同,
它**一条都抓不到**。

⚠️ **不依赖历史快照**:本脚本**当场列目录**(每个候选 1 次 `list_dir`)——
"删"要用**当前**事实,快照可能是几小时前的。

⚠️ 留哪份:**我方分享链保护的一票否决** > 文件多 > 体积大 > 不带后缀(打平时)。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.db import get_session_local  # noqa: E402
from app.services import pan_accounts  # noqa: E402
from app.services import quark_dup  # noqa: E402
from app.services.quark_transfer import QuarkTransfer  # noqa: E402


def _human(n: int) -> str:
    v = float(n or 0)
    for u in ("B", "KiB", "MiB", "GiB", "TiB"):
        if v < 1024:
            return f"{v:.2f} {u}"
        v /= 1024
    return f"{v:.2f} PiB"


def main() -> int:
    apply = "--yes" in sys.argv[1:]
    db = get_session_local()()
    try:
        ck = pan_accounts.cookie_for(db, "quark", user_id=1)     #: ★ 按池取号(见 pan_accounts)
    finally:
        db.close()
    if not ck:
        print("❌ 取不到夸克凭据(池里没有、.env 也没有)")
        return 2

    qt = QuarkTransfer(ck)
    print("=== 正在列目录(每个候选 1 次 list_dir,当场扫、不吃旧快照)… ===", flush=True)
    plan = quark_dup.build_package_plan(qt)
    print(f"\n★ 整包重复:**{plan['n_groups']} 组**,要删 **{plan['n_drop']} 份** / "
          f"**{_human(plan['bytes'])}**")
    print(f"  (我方分享链保护的 fid:{plan['protected_fids']} 个 —— 含被保护文件的整包**永不删**)\n")
    for g in plan["groups"]:
        k = g["keep"]
        print(f"  [{g['home']}] {g['base'][:40]}")
        print(f"      留 {k['name'][-24:]:<26} {k['files']:>4} 文件 {_human(k['bytes'])}")
        for d in g["drop"]:
            print(f"      删 {d['name'][-24:]:<26} {d['files']:>4} 文件 {_human(d['bytes'])}")
    if not plan["n_drop"]:
        print("  没有可删的(要么没有整包重复,要么全被保护)")
        return 0
    if not apply:
        print("\n(这是**只报告**;要真删加 `--yes` —— 删进回收站,可捞回)")
        return 0
    print("\n=== 真删(进回收站)… ===", flush=True)
    res = quark_dup.apply_package_plan(qt, plan, to_recycle=True)
    print(f"✅ 已删 {res['deleted']} 份整包,约省 {_human(res['bytes'])}"
          + (f";失败 {res['failed'][:3]}" if res["failed"] else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
