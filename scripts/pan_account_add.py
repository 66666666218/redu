"""**网盘账号池**的登记/查看工具(2026-10-14,配合 `pan_accounts`)。

用户口径:「一个网盘账号内存不足**增加多个账号补上**」。这个工具就是"加账号"那一步:
把**新账号的凭据**加密存进 `cookie_store`,并让它进池(转存时会**挑最空的**那个用,
满了自动换下一个 —— 见 `app/services/pan_accounts.py`)。

## 迅雷那一枚长什么样
不是网页 cookie 串,而是**一份 JSON**(至少含 `refresh_token`)—— 与 `cookie_store`
里现有那枚同源(导出办法见 `tools/thunder_cdp.py` / `tools/thunder_sniff.py`)。
本工具会**校验**它有 `refresh_token`(没有就直接拒,别让一枚不能刷新的凭据进池)。

## 用法
    python scripts/pan_account_add.py --list                       # 看池里有哪些账号
    python scripts/pan_account_add.py --check                      # 逐个探测使用率(写回池)
    python scripts/pan_account_add.py --label 小号 --cookie-file data/_xunlei2.json
    python scripts/pan_account_add.py --label 小号 --cookie '{"refresh_token":"...", ...}'

⚠️ **只加不删**:要停用某个账号,把池里那条的 `label` 改掉或手工清 `pan_account_pool`
(刻意不提供"删账号"—— 那属于人工决定,不该被一个脚本一键做掉)。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import select  # noqa: E402

from app.db import get_session_local  # noqa: E402
from app.db.models import User  # noqa: E402
from app.services import pan_accounts  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--platform", default="xunlei", help="网盘(默认 xunlei)")
    ap.add_argument("--label", default="", help="账号别名(给人看的;重名 = 覆盖它的凭据)")
    ap.add_argument("--cookie", default="", help="凭据 JSON 串")
    ap.add_argument("--cookie-file", default="", help="凭据 JSON 文件")
    ap.add_argument("--user", type=int, default=0, help="归属用户(默认:第一个启用用户)")
    ap.add_argument("--list", action="store_true", help="列出池里的账号")
    ap.add_argument("--check", action="store_true", help="逐个探配额(并把结果写回池)")
    args = ap.parse_args()

    db = get_session_local()()
    try:
        uid = args.user
        if not uid:
            uid = db.scalar(select(User.id).where(User.enabled.is_(True)).order_by(User.id)) or 1

        if args.list or args.check or not (args.cookie or args.cookie_file):
            refs = pan_accounts.ordered(db, args.platform, user_id=uid)
            print(f"{args.platform} 账号池({len(refs)} 个;顺序 = 转存时的尝试顺序,最空的在前):")
            for r in refs:
                mark = "(默认账号;池里没配)" if r.is_default else ""
                print(f"  · {r.label:<14} cookie_key={r.cookie_key:<12} "
                      f"最近使用率={r.last_ratio if r.last_ratio is not None else '未知'} "
                      f"满到={r.full_until or '-'} {mark}")
                if args.check and not r.is_default:
                    from app.services import xunlei_transfer as xt

                    cred = xt._credentials(None, cookie_key=r.cookie_key)
                    if not cred:
                        print("      ⚠️ 取不到凭据(没登记或解不开)")
                        continue
                    ratio = xt.quota_ratio(cred)
                    pan_accounts.report(db, r, ratio=ratio,
                                        full=None if ratio is None else False)
                    print(f"      实测使用率:{'探不到' if ratio is None else f'{ratio * 100:.1f}%'}")
            if not (args.cookie or args.cookie_file):
                return 0

        raw = args.cookie
        if args.cookie_file:
            raw = Path(args.cookie_file).read_text(encoding="utf-8")
        try:
            data = json.loads(raw)
        except ValueError:
            print("❌ 凭据不是合法 JSON(迅雷这枚是 JSON,不是网页 cookie 串)")
            return 2
        if not isinstance(data, dict) or not data.get("refresh_token"):
            print("❌ 凭据里没有 refresh_token —— 它刷不出新 token,不该进池")
            return 2
        label = args.label or f"{args.platform}-{len(pan_accounts.accounts(db, args.platform, user_id=uid)) + 1}"
        key = pan_accounts.add_account(db, args.platform, label,
                                       json.dumps(data, ensure_ascii=False),
                                       user_id=uid)
        print(f"✅ 已登记:{args.platform}/{label}(cookie_key={key})")
        from app.services import xunlei_transfer as xt

        ratio = xt.quota_ratio(xt._credentials(None, cookie_key=key))
        print(f"   使用率:{'探不到(接口问题?)' if ratio is None else f'{ratio * 100:.1f}%'}")
        print("   从此转存会**挑最空的**那个账号用;它满了会自动换下一个。")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
