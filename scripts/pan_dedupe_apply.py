"""把「重复资源包」的清理计划**落到迅雷回收站**(2026-10-07)。

## 安全设计(删除不可逆,所以每一条都是故意的)
1. **默认 dry-run** —— 不加 `--yes` 时**只打印计划,一个字节都不删**;
2. **删进回收站**(`trash_files`,可捞回),不是彻底删;
3. 判据与报告**用同一份代码**(`_pan_dedupe.build_plan`),不另写一套;
4. **没扫完要说出来** —— `complete=False` 时打警告,别让"扫了一半"看着像"就这些";
5. `--max N` 可以只处理前 N 组(先小批试)。

用法:
    python scripts/pan_dedupe_apply.py            # 只看计划(默认)
    python scripts/pan_dedupe_apply.py --max 2    # 只看前 2 组
    python scripts/pan_dedupe_apply.py --yes      # 真删(进回收站)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._pan_dedupe import build_plan  # noqa: E402


def main() -> int:
    yes = "--yes" in sys.argv
    depth = 3
    cap = None
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--max" and i + 1 < len(args):
            cap = int(args[i + 1])
        elif a.isdigit():
            depth = int(a)

    out = build_plan(depth)
    plan = out["plan"][:cap] if cap else out["plan"]
    print(f"扫了 {out['scanned_dirs']} 个目录,{out['calls']} 次 API 调用"
          f"{'' if out['complete'] else ' —— ⚠️ **撞预算,没扫完,下面的清单不完整**'}")
    print(f"重复组 {len(plan)} 组,拟删 {sum(len(p['drop']) for p in plan)} 个整包,"
          f"约省 {sum(s['size'] for p in plan for s in p['drop']) / 2**30:.2f} GiB\n")
    for p in plan:
        k, d = p["keep"], p["drop"]
        print(f"【{p['name'][:40]}】  同处 {str(p['parent'])[:36] or '/'}")
        print(f"   ✔留  {k['items']:>4} 项 / {k['size'] / 2**20:>9.1f} MiB  {k['path'][:70]}")
        for s in d:
            print(f"   ✂删  {s['items']:>4} 项 / {s['size'] / 2**20:>9.1f} MiB  {s['path'][:70]}")
            print(f"        fid={s['id']}")
    fids = [s["id"] for p in plan for s in p["drop"]]
    if not fids:
        print("\n没有要删的。")
        return 0
    if not yes:
        print(f"\n=== DRY-RUN:上面 {len(fids)} 个**一个都没删** ===")
        print("确认无误后加 `--yes` 真删(会进**迅雷回收站**,可捞回)。")
        return 0

    from app.services import xunlei_transfer as xt

    print(f"\n=== 开始删 {len(fids)} 个(删进回收站)===")
    res = xt.trash_files(fids)
    print("   接口返回:", str(res)[:300])
    print("   ⚠️ 删完请**自己去回收站确认一遍**,别只信这一行。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
