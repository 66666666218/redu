"""抖音**第二个号**登录并入库(多账号轮换用,2026-10-21)。

## 为什么需要
抖音的 `verify_check` 是**按账号/会话**下发的。只有一个号时,撞上风控就只能等 6 小时;
而 `douyin_leads` 现在会按 `cookie_store` 里的 `douyin` / `douyin#2` / … **轮换**
(见 `app/services/douyin_protocol_source.account_keys`)。这个脚本负责把**第二个号的
登录态**导进加密库。

## 用法
    # 1) 开一个**新的**档案(别复用第一个号的档案,否则会把它的登录态覆盖掉)
    python scripts/douyin_account_add.py --key douyin#2 --wait 300
    # 2) 在弹出来的 Edge 窗口里登录第二个抖音号(扫码/密码均可)
    # 3) 脚本检测到 sessionid 后自动导出并**解冻这个号的验证冷却**

    # 已经有一个登录好的档案,只导入:
    python scripts/douyin_account_add.py --key douyin#2 --profile <档案目录> --import-only

## 纪律
- **不打印、不落盘**任何 Cookie 值(只写进加密库);
- 缺 `sessionid` 时**照样报失败** —— 不静默成功(那正是"导了个空壳还以为修好了");
- 档案目录**必须与第一个号不同**:同一个目录只会读到后登录的那个号。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
DEFAULT_PROFILE = ROOT / "tools" / "MediaCrawler" / "browser_data" / "cdp_dy2_user_data_dir"
HOME = "https://www.douyin.com/"


def _wait_for_login(profile: Path, wait_s: int) -> bool:
    """开 Edge 档案;等到 `sessionid` 出现。返回是否登录。"""
    from playwright.sync_api import sync_playwright

    profile.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(profile), headless=False, no_viewport=True,
            channel="msedge", executable_path=EDGE)
        try:
            pg = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                pg.goto(HOME, wait_until="domcontentloaded", timeout=60000)
            except Exception as exc:  # noqa: BLE001 - 首屏慢不影响后续轮询
                print("(首屏导航异常,继续等登录):", exc, flush=True)
            deadline = time.time() + wait_s
            while time.time() < deadline:
                time.sleep(3)
                names = {c.get("name") for c in ctx.cookies()}
                if {"sessionid", "sessionid_ss"} & names:
                    return True
            return False
        finally:
            ctx.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", default="douyin#2", help="账号键(默认 douyin#2)")
    ap.add_argument("--profile", default=str(DEFAULT_PROFILE), help="Edge 档案目录")
    ap.add_argument("--wait", type=int, default=300, help="等人登录的秒数")
    ap.add_argument("--import-only", action="store_true", help="只从已有档案导入,不开浏览器")
    ap.add_argument("--user-id", type=int, default=1)
    args = ap.parse_args()
    if not args.key.startswith("douyin"):
        print("❌ --key 必须以 douyin 开头(例:douyin#2)", flush=True)
        return 2
    profile = Path(args.profile)

    if not args.import_only:
        print(f"=== 打开 {HOME} —— 请在 Edge 窗口里登录**第二个**抖音号 ===", flush=True)
        if not _wait_for_login(profile, args.wait):
            print("❌ 超时:没有检测到 sessionid(没登录成功)", flush=True)
            return 1
        print("✓ 检测到登录态,开始导出", flush=True)
    elif not profile.exists():
        print(f"❌ 档案不存在:{profile}", flush=True)
        return 2

    from app.db.database import get_session_local, init_db
    from app.services.douyin_cookie_export import export_from_profile

    init_db()
    with get_session_local()() as db:
        try:
            note = export_from_profile(db, args.user_id, profile, cookie_key=args.key)
        except Exception as exc:  # noqa: BLE001 - 把原因说给人听
            print(f"❌ 导出失败:{type(exc).__name__}: {exc}", flush=True)
            return 1
    print(f"✓ 已写入账号键 **{args.key}**:{note}", flush=True)
    print("  下一轮 douyin_leads 会自动在号之间轮换(默认号被风控时换这个)。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
