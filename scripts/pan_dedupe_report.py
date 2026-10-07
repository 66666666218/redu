"""盘内**重复资源包**只读报告(2026-10-07)。**不删任何东西。**

判据与执行器**共用一份**(`app/services/pan_dedupe.py`)—— 那种"两处各写一遍、迟早飘"的事
本仓吃过大亏,这里不再来一次。

⚠️ 三条别忘(都是实测踩出来的,详见 `app.services.pan_dedupe` 的注释):
  ① 判据是 **`(父目录, 只差 (N) 的名字)`** —— 按文件名/按归一化名都试过,都会误判;
  ② 「老」**算不出来**:盘上所有时间都是入库那天,没有区分度 ⇒ 本报告只做**重复**;
  ③ 「要删的比留的还大」的组**自动跳过** —— 条目多 ≠ 内容全,那种多半是两份不同的东西。

用法:`python scripts/pan_dedupe_report.py [深度]`(默认 3)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.pan_dedupe import build_plan  # noqa: E402

# ⚠️ **脚本自己定 stdout 编码,不依赖调用环境**(2026-10-07 实测踩到):
# 后台跑时 stdout 不是终端,默认编码是 **GBK**,而本脚本要打印 ✔/✂
# ⇒ `UnicodeEncodeError` 直接崩,而且**崩在打印第一组时** —— 前面几分钟的扫描全白跑。
# 上一次没崩只是因为那个 shell 恰好 export 了 PYTHONIOENCODING,那种'靠环境'的稳定是假的。
sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    depth = 4
    for a in sys.argv[1:]:
        if a.isdigit():
            depth = int(a)

    out = build_plan(depth)
    print(f"扫了 {out['scanned_dirs']} 个目录,{out['calls']} 次 API 调用"
          f"{'' if out['complete'] else ' —— ⚠️ **撞预算,没扫完,下面的清单不完整**'}\n")

    if out["n_drop"]:
        print(f"=== ✅ 可自动删:{out['n_drop']} 个整包,约省 "
              f"**{out['freed'] / 2 ** 30:.2f} GiB** ===")
    for p in out["plan"]:
        if p["risky"]:
            continue
        k = p["keep"]
        print(f"\n  【{p['name'][:42]}】  同处 {str(p['parent'])[:38] or '/'}")
        print(f"     ✔留 {k['items']:>4} 项 / {k['size'] / 2 ** 20:>9.1f} MiB  {k['path'][:66]}")
        for s in p["drop"]:
            print(f"     ✂删 {s['items']:>4} 项 / {s['size'] / 2 ** 20:>9.1f} MiB  {s['path'][:66]}")

    if out["n_risky"]:
        print(f"\n=== ⚠️ 跳过 {out['n_risky']} 组(要删的比留的还大)—— **必须人工看** ===")
        for p in out["plan"]:
            if p["risky"]:
                print(f"  【{p['name'][:36]}】{p['risky_reason']}")

    print("\n⚠️ 本脚本**只读**。要真删请跑 `scripts/pan_dedupe_apply.py`(默认 dry-run)。")
    print("⚠️ 而且「老」这一维**没做** —— 盘上文件时间全是入库那天,判不出来。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
