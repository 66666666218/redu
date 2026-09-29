"""人工回填建议效果:把从夸克官方渠道拿到的粗颗粒数据记进建议表。

⚠️ 事实修正(2026-09-29 运营者确认):夸克官方「分享管理」只能看每条链接
是否失效,**没有转存/保存人数统计**——update_list 接口的 save_pv/click_pv
(-1/0)就是数据全集,链接级转存数在夸克当前产品形态下不可得。
本脚本保留用于录入从其他渠道获得的效果数据(如官方拉新活动后台的
总拉新数,按周人工归因),用法不变:

    python scripts/record_saves.py 世界杯赛程=80 考公真题=35

链接级"结算"目前走替代信号:发文后的文章阅读增量(wechat_traffic_samples
已有采样)——见 CHANGELOG 2026-09-29 v5 结算端降级讨论。
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
