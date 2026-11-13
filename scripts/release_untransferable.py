"""把**机制上搬不动**的积压标成终态 `skipped`,让它们别再吃额度。

用户口径(2026-11-13):两类积压「标 skipped 放弃」;且百度那批「清理百度盘或换一个号
后会自动重试」这句**现在做不到** —— 百度 3/4 号的登录态都已失效,池里只剩那个
超额 43 倍的主号,所以它们在可预见的将来**一条都搬不成**。

## 两族,判据完全不同(混在一个脚本里,但要分开数、分开写原因)

### ⒈ `discovered_pan_links` 里挂在百度盘满上的 pending
判据 = `message LIKE '%百度盘空间不足%'`(**不是**按平台/域名推的)。
实测(2026-11-13 12:00):**224 条**,tieba 164 + weibo 60,来源全是 `pan.baidu.com`。
这 224 条每轮都排在 `backlog` 前面、**占着 `pan_discovery_transfer_limit`(10)的名额**
—— 它们一件也搬不成,真正能搬的存量被挤在后面。

⚠️ 它们**不是坏链**:链本身是好的,是**盘超额 43 倍**。
⇒ 所以原因里必须写清**恢复动作**,而不是含糊的"放弃了":
`重新扫码登录 baidupan#2/#3(或清理主号)后,把 status 改回 pending 即可重搬`。

### ⒉ `xunlei_group_shares` 里「单个资源比剩余空间大」的 pending
判据 = `message LIKE '%单个资源比剩余空间大%'`。
实测(2026-11-13 15:5x):**39 条**,要 0.68~12.77 TiB,而四个迅雷号加起来只有约
**2.0 TiB 总空间**(1号16.04TiB/10GiB=超额1642倍、2号1234%、3号66%、4号42%)。
⇒ 这些包**在现有账号体系下永远搬不进**,每轮被挑中、探配额、失败、留 pending,
是纯粹的噪音(而且 `too_large` 那条路**刻意不标终态**,见 `xunlei_group.py:624`)。
⚠️ **与「盘满」分开**:盘满清一清就能搬;单包超大是**包本身就比整个盘大**。

## 为什么用脚本而不是改代码
代码里那条"保持 pending"是**刻意的**(清出空间后确实能搬,见 `xunlei_group.py:624`
与 `pan_discovery.py:678` 的注释)。现在改的是**存量数据的一次性处置**,不是判据 ——
判据照旧:以后新进来的超大包仍然保持 pending(换更大的号时还有救)。

用法:
    python scripts/release_untransferable.py            # 只报告(默认)
    python scripts/release_untransferable.py --apply    # 真改
    python scripts/release_untransferable.py --only baidu|xunlei
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from sqlalchemy import func, select  # noqa: E402

#: 恢复动作要写进 `message` —— 三个月后看到 `skipped` 的人必须知道**怎么让它回来**。
BAIDU_WHY = ("百度盘超额 43 倍且池里其余两个号登录态已失效(2026-11-13 判)⇒ 放弃排队;"
             "重新扫码登录 baidupan#2/#3 或清理主号后,把 status 改回 pending 即可重搬")
XUNLEI_WHY = ("单包比整个盘还大(现有 4 个迅雷号合计约 2 TiB,这些包要 0.68~12.77 TiB)⇒ 放弃排队;"
              "换更大的账号后可把 status 改回 pending 重搬")


def _release_baidu(db, *, apply: bool) -> tuple[int, int]:
    """返回 `(命中数, 改动数)`。"""
    from app.db.models import DiscoveredPanLink as D

    rows = list(db.scalars(select(D).where(
        D.status == "pending", D.message.like("%百度盘空间不足%"))))
    if apply:
        for r in rows:
            r.status, r.message = "skipped", BAIDU_WHY[:200]
    return len(rows), len(rows) if apply else 0


def _release_xunlei(db, *, apply: bool) -> tuple[int, int]:
    from app.db.models import XunleiGroupShare as X

    rows = list(db.scalars(select(X).where(
        X.status == "pending", X.message.like("%单个资源比剩余空间大%"))))
    if apply:
        for r in rows:
            r.status, r.message = "skipped", XUNLEI_WHY[:200]
    return len(rows), len(rows) if apply else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="真改(默认只报告)")
    ap.add_argument("--only", default="", choices=["", "baidu", "xunlei"])
    a = ap.parse_args()

    from app.db import get_session_local

    db = get_session_local()()
    try:
        if a.only in ("", "baidu"):
            n, ch = _release_baidu(db, apply=a.apply)
            print(f"百度盘满积压: 命中 {n} 条 → {'已标 skipped' if a.apply else '未改(加 --apply)'}"
                  f"{'' if ch or not a.apply else '  ⚠️ 一条都没改'}")
        if a.only in ("", "xunlei"):
            n, ch = _release_xunlei(db, apply=a.apply)
            print(f"迅雷超大包:   命中 {n} 条 → {'已标 skipped' if a.apply else '未改(加 --apply)'}")
        if a.apply:
            db.commit()
        # 改完把「还剩多少 pending」说出来 —— 否则下一次看库还是只看到一个大数字
        from app.db.models import DiscoveredPanLink as D
        from app.db.models import XunleiGroupShare as X

        print("剩余 pending: discovered_pan_links =",
              db.scalar(select(func.count()).select_from(D).where(D.status == "pending")),
              "/ xunlei_group_shares =",
              db.scalar(select(func.count()).select_from(X).where(X.status == "pending")))
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
