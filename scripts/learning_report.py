"""假设台账 CLI(2026-10-07)。**只读**,除了它自己的状态(状态就是"学习"的载体)。

    python scripts/learning_report.py             # 近 14 天,跑一轮评估并打印
    python scripts/learning_report.py 30          # 换窗口
    python scripts/learning_report.py --json      # 机器可读
    python scripts/learning_report.py --show      # **不评估**,只看上次存下来的状态与历史

⚠️ 默认会**写状态**(那是"记住上次相信什么"的唯一方式),并据此判"这轮有没有变化"。
   只想看不想动状态就用 `--show`。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    from app.db import get_session_local
    from app.db.models import User
    from app.services import learning_ledger as ll

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    days = int(args[0]) if args and args[0].isdigit() else 14
    db = get_session_local()()
    try:
        uid = db.query(User).filter(User.enabled.is_(True)).first()
        if uid is None:
            print("没有启用的用户")
            return 1
        if "--show" in sys.argv:
            state = ll._load_state(db)
            if not state:
                print("还没有存过状态 —— 先跑一次不带 --show 的。")
                return 0
            print("=== 上次存下的假设状态 ===")
            for k, v in state.items():
                print(f"\n【{k}】{v.get('statement', '')}")
                print(f"  {v.get('status')}  自 {str(v.get('since'))[:16]}  "
                      f"n={v.get('n')}  {v.get('stat')}")
                for h in v.get("history") or []:
                    print(f"    · {str(h.get('at'))[:16]}  {h.get('from')} → {h.get('to')}"
                          f"  (n={h.get('n')})")
            return 0
        r = ll.evaluate(db, int(uid.id), days=days)
    finally:
        db.close()

    if "--json" in sys.argv:
        print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
        return 0
    for line in ll.render_lines(r):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
