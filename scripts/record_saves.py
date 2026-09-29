"""人工回填夸克分享数据:把你在夸克 App「我的分享」里看到的保存人数记进建议表。

用法(一条或多条,格式 关键词=保存人数):
    python scripts/record_saves.py 世界杯赛程=80 考公真题=35

这些数字喂给效果回填:建议 → 你发文 → 转存增长,Agent 逐步学会
哪类热点/哪种资源的拉新效率最高。数字看哪里:手机夸克 App → 网盘 →
分享管理,每条链接后面显示「N 人保存」。
"""
import sys
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.db.database import get_session_local, init_db  # noqa: E402
from app.db.models import HotspotSuggestion  # noqa: E402


def main(argv: list[str]) -> int:
    init_db()
    if not argv:
        print(__doc__)
        return 1
    db = get_session_local()()
    now = dt.datetime.now()
    hit, miss = [], []
    for arg in argv:
        kw, _, num = arg.partition("=")
        kw, num = kw.strip(), num.strip()
        if not kw or not num.isdigit():
            miss.append(arg)
            continue
        row = db.scalar(select(HotspotSuggestion).where(
            HotspotSuggestion.user_id == 1,
            HotspotSuggestion.keyword.contains(kw.strip())
        ).order_by(HotspotSuggestion.created_at.desc()).limit(1))
        if row is None:
            miss.append(arg)
            continue
        row.saves = int(num)
        row.saves_at = now
        hit.append(f"{row.keyword} → 保存 {num} 人")
    db.commit()
    db.close()
    print("已记录:", "; ".join(hit) if hit else "(无)")
    if miss:
        print("没匹配到建议(检查关键词):", "; ".join(miss))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
