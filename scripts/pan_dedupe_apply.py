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

# ⚠️ **脚本自己定 stdout 编码,不依赖调用环境**(2026-10-07 实测踩到):
# 后台跑时 stdout 不是终端,默认编码是 **GBK**,而本脚本要打印 ✔/✂
# ⇒ `UnicodeEncodeError` 直接崩,而且**崩在打印第一组时** —— 前面几分钟的扫描全白跑。
# 上一次没崩只是因为那个 shell 恰好 export 了 PYTHONIOENCODING,那种'靠环境'的稳定是假的。
sys.stdout.reconfigure(encoding="utf-8", errors="replace")


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
    include_risky = "--include-risky" in sys.argv
    safe = [p for p in plan if not p["risky"]]
    risk = [p for p in plan if p["risky"]]
    print(f"扫了 {out['scanned_dirs']} 个目录,{out['calls']} 次 API 调用"
          f"{'' if out['complete'] else ' —— ⚠️ **撞预算,没扫完,下面的清单不完整**'}")
    print(f"重复组 {len(plan)} 组:可自动删 **{len(safe)} 组 / "
          f"{sum(len(p['drop']) for p in safe)} 个整包 / "
          f"{sum(s['size'] for p in safe for s in p['drop']) / 2**30:.2f} GiB**"
          f";另有 {len(risk)} 组**跳过待人工看**\n")
    for p in safe:
        k, d = p["keep"], p["drop"]
        print(f"【{p['name'][:40]}】  同处 {str(p['parent'])[:36] or '/'}")
        print(f"   ✔留  {k['items']:>4} 项 / {k['size'] / 2**20:>9.1f} MiB  {k['path'][:70]}")
        for s in d:
            print(f"   ✂删  {s['items']:>4} 项 / {s['size'] / 2**20:>9.1f} MiB  {s['path'][:70]}")
            print(f"        fid={s['id']}")
    if risk:
        # ⚠️⚠️ **这些组默认不碰** —— 见 `_pan_dedupe.build_plan` 里 RISKY_RATIO 的说明:
        # "留内容多的"是按"先条目数、再体积"判的,而**条目多 ≠ 内容全**
        # (实测「宝可梦朱紫」留 2.55 GiB、要删 27.76 GiB —— 那多半是两份**不同的东西**)。
        print(f"\n=== ⚠️ 跳过 {len(risk)} 组(要删的比留的还大)—— **必须人工看** ===")
        for p in risk:
            print(f"   【{p['name'][:36]}】{p['risky_reason']}")
            print(f"      ✔留 {p['keep']['path'][:70]}")
            for s in p["drop"]:
                print(f"      ✂删 {s['path'][:70]}   fid={s['id']}")
        print("   要我一起删?加 `--include-risky`(⚠️ 删下去就进回收站了,先想清楚)。")
    fids = [s["id"] for p in (plan if include_risky else safe) for s in p["drop"]]
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
