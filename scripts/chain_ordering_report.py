"""跨链「**谁先看到这份资源**」台账(2026-10-07)。**只读,不改任何东西。**

它回答用户那个假设 ——「抖音基本上是最先开始的,然后公众号,一些大瓜从微博更快」
—— 靠的是**把每条链自己记的时间摊到一张表上**(见 `app/services/chain_ordering.py` 的模块头)。

用法:
    python scripts/chain_ordering_report.py              # 近 90 天
    python scripts/chain_ordering_report.py 10           # 近 10 天(**各链都在线的窗口**)
    python scripts/chain_ordering_report.py 30 --json    # 机器可读
    python scripts/chain_ordering_report.py --push       # 手动推一次到管理群(定时作业也推)

⚠️ **窗口很关键**:各链的历史长度不一样(公众号从 09-08、抖音线索从 10-02),
拿 90 天去比"谁最先"会被**左截断**带偏 —— 报告开头会把各链的数据起止打出来,
看到哪条链天数明显短,就换个小窗口重跑。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 后台跑时 stdout 不是终端,默认编码是 GBK;本脚本要打印 ✔/⚠️ 一类符号,
# 不自己定编码就会 UnicodeEncodeError(本仓踩过两次,见 pan_dedupe_report 的注释)。
sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    from app.services import chain_ordering as co

    if "--push" in sys.argv:
        ok = co.chain_ordering_tick()
        print("已推送" if ok else "没推成(管理群 webhook 没配?)")
        return 0 if ok else 1

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    days = int(args[0]) if args and args[0].isdigit() else 90

    from app.db import get_session_local
    from app.db.models import User

    db = get_session_local()()
    try:
        uid = db.query(User).filter(User.enabled.is_(True)).first()
        if uid is None:
            print("没有启用的用户")
            return 1
        rep = co.ordering_report(db, int(uid.id), days=days)
    finally:
        db.close()

    if "--json" in sys.argv:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0

    print(f"=== 跨链先后台账(近 {days} 天)===")
    for line in co.summary_lines(rep):
        print(line)
    print()
    print(co.TRUNCATION_NOTE)
    print("\n=== 明细(逐组看原始名有没有误配)===")
    for g in rep["groups"]:
        flag = "  ⚠️膨胀(条数超上限,可能阈值太松)" if g["bloated"] else ""
        print(f"\n【{g['name'][:52]}】{g['chains']} 链 / {g['members']} 个写法  "
              f"{' → '.join(g['order'])}  跨 {g['lag_h']:.1f}h{flag}")
        for ch, t in g["timeline"].items():
            print(f"    {ch:5s} {t['ts'][:16]}  {t['name'][:62]}")
        if g["bloated"]:
            print("     组内全部写法:")
            for nm in g["all_names"]:
                print(f"       · {nm[:66]}")
    print("\n⚠️ 本报告**只读**。样本还小(十几份),**不足以定论** —— "
          "等它攒到几十份再看配对那一栏。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
