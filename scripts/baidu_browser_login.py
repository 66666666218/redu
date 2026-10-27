"""百度网盘**浏览器窗口登录** → 自动导出凭据并登记进账号池(2026-10-27)。

## 什么时候用它(而不是纯协议扫码 `scripts/baidu_qr_login.py`)
用户实测:纯协议那张码**被扫到**(状态 1)但 2 秒后**又退回等待**(状态 0)——
手机 App 里没有走到「确认登录」那一步。而百度自己的网页登录页是完整跑通过无数次的:
⇒ **直接弹出百度网盘的登录窗口,人扫窗口里的码**,我们在旁边等 Cookie 出现。

判据:**只有 `BDUSS` 与 `STOKEN` 同时出现才算登录成功**(2026-10-04 踩过
"只有 BDUSS 的半登录:登录检查能过、转存全失败");拿到后还要过一遍业务接口验活。

跑法:
    python scripts/baidu_browser_login.py                    # 自动取名(百度3 / baidupan#2)
    python scripts/baidu_browser_login.py --label 百度4 --cookie-key baidupan#3
    python scripts/baidu_browser_login.py --deadline 600

⚠️ 用**一次性浏览器档案**(系统临时目录):固定档案里可能已经登着**上一个**百度号,
   那样一打开就"已登录",会把旧号的凭据再存一遍 —— 看起来成功,实际白扫。
⚠️ 凭据**不打印、不落文件**;只写加密库。
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from app.db import get_session_local, init_db  # noqa: E402
from app.db.models import User  # noqa: E402

EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)
HOME = "https://pan.baidu.com/"          # 未登录会被它自己带到二维码登录页


def _cfg() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="弹出百度网盘登录窗口,登录一次后自动导出凭据并入池",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--label", default="", help="账号别名(给人看的;不填自动取 百度N)")
    ap.add_argument("--cookie-key", default="", help="凭据键(不填自动取 baidupan#N)")
    ap.add_argument("--user", type=int, default=0, help="归属用户(默认:第一个启用用户)")
    ap.add_argument("--deadline", type=float, default=600, help="等待登录秒数(默认 600)")
    return ap.parse_args()


def _edge_path() -> str | None:
    for p in EDGE_CANDIDATES:
        if Path(p).exists():
            return p
    return None


def _cookie_header(ctx) -> str:
    """把浏览器里 **baidu.com 域**的 cookie 拼成请求头串。

    同名只留一个:优先留 `.baidu.com` 这种**宽域**那份(网页版转存认它)。
    """
    out: dict[str, str] = {}
    for c in ctx.cookies():
        if not str(c.get("domain") or "").endswith("baidu.com"):
            continue
        name, value, domain = str(c.get("name") or ""), str(c.get("value") or ""), str(c.get("domain") or "")
        if not name:
            continue
        if name not in out or domain == ".baidu.com":
            out[name] = value
    return "; ".join(f"{k}={v}" for k, v in out.items() if v)


def _wait_login(ctx, deadline_s: float) -> bool:
    """等 `BDUSS` + `STOKEN` 同时出现。只在**状态变化**时打印,不刷屏。"""
    deadline = time.monotonic() + deadline_s
    last: tuple[bool, bool] | None = None
    while time.monotonic() < deadline:
        time.sleep(2)
        names = {str(c.get("name")) for c in ctx.cookies()
                 if str(c.get("domain") or "").endswith("baidu.com")}
        have = ("BDUSS" in names, "STOKEN" in names)
        if have != last:
            last = have
            print(f"  [{time.strftime('%H:%M:%S')}] BDUSS={'✓' if have[0] else '…'} "
                  f"STOKEN={'✓' if have[1] else '…'}")
        if all(have):
            return True
    return False


def main() -> int:
    args = _cfg()
    from app.services import baidu_qr_login as L

    edge = _edge_path()
    if not edge:
        print("❌ 找不到 Edge(msedge.exe)。请确认已安装,或把路径加到 EDGE_CANDIDATES。")
        return 2

    init_db()
    db = get_session_local()()
    profile = Path(tempfile.mkdtemp(prefix="baidu_login_"))
    try:
        uid = args.user or int(
            db.scalar(select(User.id).where(User.enabled.is_(True)).order_by(User.id)) or 1)
        auto_label, auto_key = L.next_pool_names(db, uid)
        label = args.label or auto_label
        cookie_key = args.cookie_key or auto_key
        print("=== 百度网盘浏览器登录 ===")
        print(f"  目标账号名:{label}(凭据键 {cookie_key},用户 {uid})")
        print("  正在打开 Edge 窗口,加载百度网盘登录页…")

        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=str(profile), headless=False, no_viewport=True,
                channel="msedge", executable_path=edge)
            try:
                pg = ctx.pages[0] if ctx.pages else ctx.new_page()
                pg.goto(HOME, wait_until="domcontentloaded", timeout=60000)
                print("👉 窗口里就是百度网盘的登录页:**用百度网盘 App 扫码**,"
                      "并点手机上的「确认登录」。")
                print(f"   窗口会在登录成功后自动关闭;本次等待上限 {int(args.deadline)} 秒。\n")
                if not _wait_login(ctx, args.deadline):
                    print("❌ 没等到登录态(缺 BDUSS 或 STOKEN)—— 凭据**不写入**。")
                    return 3
                cookie = _cookie_header(ctx)
            finally:
                ctx.close()
    finally:
        db.close()
        shutil.rmtree(profile, ignore_errors=True)

    missing = L.missing_required(cookie)
    if missing:
        print(f"❌ 导出的凭据缺 {'/'.join(missing)} —— **不写入**(半登录转存必失败)。")
        return 4
    v = L.verify(cookie)
    if not v.get("ok"):
        print(f"❌ 业务接口验活没过:{v.get('why')} —— **不写入**。")
        return 5

    db = get_session_local()()
    try:
        try:
            L.register_account(db, label=label, cookie=cookie, cookie_key=cookie_key,
                               user_id=uid, ratio=v.get("ratio"))
        except Exception as exc:  # noqa: BLE001 - 入库失败要说清楚是哪一步
            print(f"❌ 写入账号池失败:{type(exc).__name__}: {exc}")
            return 6
    finally:
        db.close()

    used = (v.get("usage") or {}).get("used", 0)
    total = (v.get("usage") or {}).get("total", 0)
    print(f"✅✅ 登录成功并验活通过:凭据已加密写入(`{cookie_key}`),登记为「{label}」")
    print(f"   实测配额:已用 {used / 2**30:.1f} GiB / {total / 2**30:.1f} GiB"
          f"({float(v.get('ratio') or 0) * 100:.1f}%)")
    print("   ⇒ 要再加一个,直接再跑一次本工具。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
