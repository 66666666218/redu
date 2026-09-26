"""把 WeRSS 的订阅 id 按名称回填进对标号的 `biz` 列(接上免费全量文章列表)。

用法:
    python scripts/werss_backfill_biz.py              # 只看计划,不写库
    python scripts/werss_backfill_biz.py --apply      # 确认后写入
    python scripts/werss_backfill_biz.py --apply --user 3

前提:.env 里配好 WECHAT_WERSS_URL / WECHAT_WERSS_AK / WECHAT_WERSS_SK,并且这些公众号
已经在 WeRSS 后台添加为订阅。已有 biz 的行不动;重名与找不到的都只报告不猜。
退出码: 0=成功(含"无需回填");1=有歧义/缺订阅需要人工处理。
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from app.db import get_session_local  # noqa: E402
from app.services import wechat_monitor  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="按名称回填 WeRSS 订阅 id 到 biz 列")
    parser.add_argument("--apply", action="store_true", help="真正写库(默认只打印计划)")
    parser.add_argument("--user", type=int, default=None, help="只处理指定用户 id(默认全部有对标号的用户)")
    args = parser.parse_args()

    db = get_session_local()()
    Benchmark = wechat_monitor.WechatBenchmark
    try:
        if args.user:
            uid_filter = [args.user]
        else:
            uid_filter = list(db.scalars(select(Benchmark.user_id).distinct()))
        need_human = 0
        try:
            for uid in uid_filter:
                out = wechat_monitor.match_biz_from_werss(db, uid, apply=args.apply)
                tag = "已写入" if out["applied"] else ("待写库" if out["matched"] else "无动作")
                print(f"user={uid} {tag}: 匹配 {out['matched']} / 已有 biz {out['already']} "
                      f"/ 重名 {len(out['ambiguous'])} / 未订阅 {len(out['missing'])}")
                for item in out["detail"]:
                    print(f"    + {item['nickname']} -> {item['biz']}")
                for item in out["ambiguous"]:
                    print(f"    ! 重名 {item['nickname']}:候选 {item['candidates']}"
                          "(请在 WeRSS 后台把订阅名改得可区分,或手工 UPDATE biz)")
                for name in out["missing"]:
                    print(f"    ? 未找到订阅:{name}(确认它已加进 WeRSS 且名称一致)")
                need_human += len(out["ambiguous"]) + len(out["missing"])
        except ValueError as exc:      # WeRSS 没配置:给一句人话,别甩 traceback
            print(f"无法回填:{exc}")
            return 1
        if not args.apply:
            print("(以上只是计划,加 --apply 才写库)")
        return 1 if need_human and args.apply else 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
