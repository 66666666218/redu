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


#: 哪些盘的凭据是「**整条网页 cookie 串**」(不是迅雷那种含 `refresh_token` 的 JSON)。
#: ⚠️ **判据必须按平台分流**(2026-10-16):以前无论加哪个盘都拿迅雷那套 JSON+refresh_token 去卡
#: ⇒ **夸克/百度的凭据根本进不来**(它们本来就没有 refresh_token)⇒ 池子只能加迅雷。
#: 反方向也一样:迅雷那枚是 JSON,不能当 cookie 串收。
_COOKIE_STR_PLATFORMS = ("quark", "baidupan", "baidu")


def _validate(platform: str, raw: str) -> tuple[str, str]:
    """校验并归一化凭据。返回 `(存进库的串, 错误信息)` —— **错误信息非空 = 拒收**。"""
    raw = (raw or "").strip()
    if not raw:
        return "", "凭据是空的"
    if platform in _COOKIE_STR_PLATFORMS:
        if "=" not in raw:
            return "", (f"{platform} 要的是**整条网页 cookie 串**(形如 `k=v; k2=v2; …`),"
                        f"收到的既不像 cookie 串、也不是 JSON")
        return raw, ""
    #: 其余(目前只有迅雷)按「JSON + refresh_token」收
    try:
        data = json.loads(raw)
    except ValueError:
        return "", "凭据不是合法 JSON(迅雷这枚是 JSON,不是网页 cookie 串)"
    if not isinstance(data, dict) or not data.get("refresh_token"):
        return "", "凭据里没有 refresh_token —— 它刷不出新 token,不该进池"
    return json.dumps(data, ensure_ascii=False), ""


def _probe(platform: str, key: str) -> tuple[object, str]:
    """探这个账号的使用率。返回 `(比例或 None, 说明)`。

    ⚠️ **只有迅雷有配额接口** —— 别的盘如实说「探不到」,别假装探过(那会变成假数字)。
    """
    if platform != "xunlei":
        return None, f"{platform} 没有配额接口 ⇒ 不探(不是失败)"
    from app.services import xunlei_transfer as xt

    cred = xt._credentials(None, cookie_key=key)
    if not cred:
        return None, "取不到凭据(没登记或解不开)"
    return xt.quota_ratio(cred), ""


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
                    ratio, why = _probe(args.platform, r.cookie_key)
                    if ratio is None:
                        print(f"      {why or '探不到'}")
                        continue
                    pan_accounts.report(db, r, ratio=ratio, full=False)
                    print(f"      实测使用率:{ratio * 100:.1f}%")
            if not (args.cookie or args.cookie_file):
                return 0

        raw = args.cookie
        if args.cookie_file:
            raw = Path(args.cookie_file).read_text(encoding="utf-8")
        blob, err = _validate(args.platform, raw)
        if err:
            print(f"❌ {err}")
            return 2
        label = args.label or f"{args.platform}-{len(pan_accounts.accounts(db, args.platform, user_id=uid)) + 1}"
        key = pan_accounts.add_account(db, args.platform, label, blob, user_id=uid)
        print(f"✅ 已登记:{args.platform}/{label}(cookie_key={key};凭据已进**加密库**,值不打印)")
        ratio, why = _probe(args.platform, key)
        print(f"   使用率:{'探不到' if ratio is None else f'{ratio * 100:.1f}%'}"
              + (f"({why})" if why else ""))
        print("   从此转存会**挑最空的**那个账号用;它满了会自动换下一个。")
        print("   ⚠️ 前提:**那条转存路径得真的从池里取号** —— 目前只有迅雷那条接了池;"
              "夸克/百度的取号还没接,登记了也不会被用(见 `pan_accounts` 的门面说明)。")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
