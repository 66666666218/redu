"""百度网盘**扫码登录** → 自动登记进百度账号池(2026-10-27)。

用户口径:「我给你扫俩百度你弹出来二维码」。跑一次:
    python scripts/baidu_qr_login.py                 # 自动取名(百度3 / baidupan#2)
    python scripts/baidu_qr_login.py --label 百度3 --cookie-key baidupan#2
    python scripts/baidu_qr_login.py --no-open       # 不自动弹图(远程/无桌面)

它会:
1. 申请一张二维码,存成 `data/baidu_qr_<sign>.png` 并**自动弹开**;
2. 长轮询等你扫码 + 手机上点「确认登录」;
3. 用**业务接口**(`pan.baidu.com/api/quota`)验这枚凭据,验不过**不写入**;
4. 验过后加密写进 `cookie_store`,并登记进 `pan_account_pool`(转存时最空的先用)。

⚠️ 凭据**不打印、不落文件**;二维码 PNG 在 `data/`(已 gitignore)。
⚠️ 二维码文件名带 `sign` 后缀:固定名会被下一次覆盖,而看图软件可能缓存旧图 ⇒
   人会白扫一张**没人轮询的废码**。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from app.db import get_session_local, init_db  # noqa: E402
from app.db.models import User  # noqa: E402

QR_DIR = ROOT / "data"


def _cfg() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="百度网盘扫码登录并登记进账号池(纯协议,不开浏览器)",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--label", default="", help="账号别名(给人看的;不填自动取 百度N)")
    ap.add_argument("--cookie-key", default="", help="凭据键(不填自动取 baidupan#N)")
    ap.add_argument("--user", type=int, default=0, help="归属用户(默认:第一个启用用户)")
    ap.add_argument("--deadline", type=float, default=300, help="等待扫码秒数(默认 300)")
    ap.add_argument("--no-open", action="store_true", help="不自动弹开二维码图片")
    return ap.parse_args()


def _open_png(path: Path) -> None:
    try:
        os.startfile(str(path))          # noqa: S606 - 只是让人少一步找文件
    except Exception as exc:             # noqa: BLE001 - 弹不开不算错,路径已打印
        print(f"  (没能自动打开图片:{type(exc).__name__} —— 手动打开上面那个路径即可)")


def main() -> int:
    args = _cfg()
    from app.services import baidu_qr_login as L

    init_db()
    db = get_session_local()()
    try:
        uid = args.user or int(
            db.scalar(select(User.id).where(User.enabled.is_(True)).order_by(User.id)) or 1)
        auto_label, auto_key = L.next_pool_names(db, uid)
        label = args.label or auto_label
        cookie_key = args.cookie_key or auto_key
        print("=== 百度网盘扫码登录 ===")
        print(f"  目标账号名:{label}(凭据键 {cookie_key},用户 {uid})")

        try:
            ticket = L.create_qr()
        except L.BaiduQrError as exc:
            print(f"❌ 申请二维码失败({exc.kind}):{exc}")
            return 2

        png = QR_DIR / f"baidu_qr_{ticket.sign}.png"
        try:
            L.download_qr(ticket, png)
        except L.BaiduQrError as exc:
            print(f"❌ 下载二维码失败({exc.kind}):{exc}")
            return 2
        print(f"  二维码图片(本次唯一):{png}")
        if not args.no_open:
            _open_png(png)
        print("\n👉 用**百度网盘 APP**扫上面这张图,扫完在手机上点「确认登录」。")
        print(f"   认准文件名 `{png.name}`,别扫以前留下的旧图;"
              f"本次等待上限 {int(args.deadline)} 秒,过期了重跑本工具即可。\n")

        last: object = object()

        def _report(status: int | None, diag: str) -> None:
            nonlocal last
            if (status, diag) == last:
                return
            last = (status, diag)
            stamp = time.strftime("%H:%M:%S")
            if diag:
                print(f"  [{stamp}] 轮询异常({diag}),继续等…")
            elif status is None:
                print(f"  [{stamp}] 等待扫码…")
            else:
                print(f"  [{stamp}] {L.STATUS_TEXT.get(status, f'状态 {status}')}")

        try:
            L.wait_for_login(ticket, on_status=_report, deadline_s=args.deadline)
            print("  ✅ 手机已确认,正在取登录态…")
            cookie = L.complete(ticket)
        except L.BaiduQrError as exc:
            print(f"❌ 登录没完成({exc.kind}):{exc}")
            return 3

        missing = L.missing_required(cookie)
        if missing:
            #: 半登录**拒收**:登录检查会过,转存/分享却全失败(2026-10-04 踩过)
            print(f"❌ 取到的凭据缺 {'/'.join(missing)} —— **不写入**"
                  "(半登录:登录检查能过、转存必失败)。请确认扫码的是**百度网盘 APP**里的"
                  "账号,并完成「确认登录」。")
            return 4

        v = L.verify(cookie)
        if not v.get("ok"):
            print(f"❌ 业务接口验活没过:{v.get('why')} —— **不写入**"
                  "(存一份死的凭据比不存更坏)")
            return 5

        try:
            L.register_account(db, label=label, cookie=cookie, cookie_key=cookie_key,
                               user_id=uid, ratio=v.get("ratio"))
        except Exception as exc:  # noqa: BLE001 - 入库失败要说清楚是哪一步
            print(f"❌ 写入账号池失败:{type(exc).__name__}: {exc}")
            return 6

        used, total = (v.get("usage") or {}).get("used", 0), (v.get("usage") or {}).get("total", 0)
        print(f"✅✅ 登录成功并验活通过:凭据已加密写入(`{cookie_key}`),"
              f"并登记为「{label}」")
        print(f"   实测配额:已用 {used / 2**30:.1f} GiB / {total / 2**30:.1f} GiB"
              f"({float(v.get('ratio') or 0) * 100:.1f}%)")
        print("   ⇒ 下一轮转存会自动挑最空的号;要再加一个,直接再跑一次本工具。")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
