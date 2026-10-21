"""微博**扫码登录 → 验活 → 写入加密 Cookie 库**(2026-10-21)。

## 为什么需要
微博纯协议采集(`app/services/weibo_search.py`)只要一枚登录 Cookie,但它**没有
可自动重导的来源** —— 失效后只能人工重登。2026-10-21 16:25 就是这么断的
(`m.weibo.cn` 把我们重定向到 `passport.weibo.com`)。
本脚本把「开浏览器 → 人扫码 → 服务端验活 → 写库」合成一条命令。

## 用法
    python scripts/weibo_login.py                # 开 Edge,等人扫码(默认等 5 分钟)
    python scripts/weibo_login.py --wait 600
    python scripts/weibo_login.py --import-only  # 档案已经登录好,只导入并验活

## 纪律
- **只往加密库写,不打印、不落盘** Cookie 值;
- 判据是**服务端** `m.weibo.cn/api/config` 的 `data.login`,
  不是"cookie 里有 `SUB`" —— 档案里的死 `SUB` 会造成**假绿灯**;
- 档案目录默认与 MediaCrawler 的 `cdp_wb_user_data_dir` **同一个**,
  这样浏览器兜底和协议路共用同一份登录态。
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
DEFAULT_PROFILE = ROOT / "tools" / "MediaCrawler" / "browser_data" / "cdp_wb_user_data_dir"
HOME = ("https://passport.weibo.com/sso/signin?entry=wapsso&source=wapssowb"
        "&url=https%3A%2F%2Fm.weibo.cn%2F")
_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
       "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148")


def _verify(cookie_blob: str) -> tuple[bool, str]:
    """真问一次 `m.weibo.cn/api/config`。**只有 `data.login` 为 true 才算登录。**"""
    import requests

    try:
        r = requests.get("https://m.weibo.cn/api/config", timeout=20,
                         headers={"User-Agent": _UA, "Referer": "https://m.weibo.cn/",
                                  "Cookie": cookie_blob})
        d = r.json()
    except Exception as exc:  # noqa: BLE001 - 验活失败只影响判定
        return False, f"校验请求异常:{type(exc).__name__}"
    login = bool((d.get("data") or {}).get("login"))
    return login, "服务端确认已登录" if login else "服务端说未登录(cookie 可能是旧的)"


def _read_cookies(profile: Path) -> dict[str, str]:
    """从已登录的 Edge 档案读 cookie(**只读,不导航**)。"""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(profile), headless=True, no_viewport=True,
            channel="msedge", executable_path=EDGE)
        try:
            jar: list[dict] = []
            for url in ("https://m.weibo.cn", "https://weibo.com"):
                try:
                    jar.extend(ctx.cookies(url))
                except Exception:  # noqa: BLE001 - 单域取不到不影响其余
                    pass
        finally:
            ctx.close()
    out: dict[str, str] = {}
    for c in jar:  # 同名取最后写入的
        if c.get("name"):
            out[c["name"]] = c["value"]
    return out


def _wait_login(profile: Path, wait_s: int) -> dict[str, str]:
    """开浏览器等人扫码;检测到 `SUB` 后用服务端验活,验过才返回。"""
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
            except Exception as exc:  # noqa: BLE001 - 首屏慢不影响轮询
                print("(首屏导航异常,继续等登录):", exc, flush=True)
            deadline = time.time() + wait_s
            while time.time() < deadline:
                time.sleep(3)
                ck = {c["name"]: c["value"] for c in ctx.cookies() if c.get("name")}
                if "SUB" not in ck:
                    continue
                ok, why = _verify("; ".join(f"{k}={v}" for k, v in ck.items()))
                if ok:
                    print(f"✓ {why}", flush=True)
                    return ck
                print(f"… 档案里有 SUB,但{why};继续等(可能还没扫完码)", flush=True)
            raise TimeoutError(f"等了 {wait_s} 秒仍未登录")
        finally:
            ctx.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default=str(DEFAULT_PROFILE))
    ap.add_argument("--wait", type=int, default=300)
    ap.add_argument("--import-only", action="store_true")
    ap.add_argument("--user", type=int, default=1)
    args = ap.parse_args()
    profile = Path(args.profile)

    try:
        ck = _read_cookies(profile) if args.import_only else _wait_login(profile, args.wait)
    except Exception as exc:  # noqa: BLE001 - 把原因打给人看
        print(f"✗ 读取/登录失败:{type(exc).__name__}: {exc}", flush=True)
        return 1
    if not ck:
        print("✗ 一个 cookie 都没读到(档案可能没登录)", flush=True)
        return 1
    blob = "; ".join(f"{k}={v}" for k, v in ck.items())
    ok, why = _verify(blob)
    if not ok:
        print(f"✗ 不写库:{why}(用 --import-only 前请先在档案里登录)", flush=True)
        return 1

    from app.db.database import get_session_local, init_db
    from app.services.cookie_store import set_cookie

    init_db()
    db = get_session_local()()
    try:
        set_cookie(db, args.user, "weibo", blob)
    finally:
        db.close()
    print(f"✓ 已写入 weibo 凭据({len(ck)} 个 cookie,服务端确认登录)", flush=True)
    print("  下一轮「跨平台热度/盘链发现」会自动用上它。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
