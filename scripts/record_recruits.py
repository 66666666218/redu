"""人工录入夸克官方拉新后台的周度拉新总数(方案B 总账校准)。

⚠️ 背景:夸克官方「分享管理」不提供链接级转存统计(2026-09-29 确认),
逐条建议走发文阅读增量自动结算;本脚本录的总数用于每周对账,
验证自动结算信号与真实拉新的相关性。

用法(一条或多条,格式 周起始日=拉新数):
    python scripts/record_recruits.py 2026-09-22=12 2026-09-29=7
    python scripts/record_recruits.py 2026-09-22=12 --note "含国庆活动"

同周重录 = 覆盖更新。
"""
import sys
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.db.database import get_session_local, init_db  # noqa: E402
from app.db.models import PanRecruitWeekly  # noqa: E402


def main(argv: list[str]) -> int:
    init_db()
    note = ""
    if "--note" in argv:
        i = argv.index("--note")
        note = argv[i + 1] if i + 1 < len(argv) else ""
        argv = argv[:i] + argv[i + 2:]
    if not argv:
        print(__doc__)
        return 1
    db = get_session_local()()
    hit, miss = [], []
    for arg in argv:
        week_s, _, num = arg.partition("=")
        week_s, num = week_s.strip(), num.strip()
        try:
            week = dt.datetime.strptime(week_s, "%Y-%m-%d")
        except ValueError:
            miss.append(f"{arg}(日期格式应为 YYYY-MM-DD)")
            continue
        if not num.isdigit():
            miss.append(f"{arg}(拉新数应为非负整数)")
            continue
        row = db.scalar(select(PanRecruitWeekly).where(
            PanRecruitWeekly.user_id == 1, PanRecruitWeekly.week_start == week))
        if row is None:
            row = PanRecruitWeekly(user_id=1, week_start=week)
            db.add(row)
        row.recruits = int(num)
        if note:
            row.note = note
        hit.append(f"{week_s} 起的那周 → 拉新 {num}")
    db.commit()
    rows = db.scalars(select(PanRecruitWeekly).where(
        PanRecruitWeekly.user_id == 1).order_by(
        PanRecruitWeekly.week_start.desc()).limit(8)).all()
    db.close()
    print("已记录:", "; ".join(hit) if hit else "(无)")
    if miss:
        print("没录入(检查格式):", "; ".join(miss))
    print("\n最近 8 周(总账):")
    total = sum(r.recruits for r in rows)
    for r in rows:
        print(f"  {r.week_start.strftime('%Y-%m-%d')} 起: {r.recruits} 人"
              + (f" | {r.note}" if r.note else ""))
    print(f"  合计(近 {len(rows)} 周): {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
