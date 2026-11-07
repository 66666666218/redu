"""抖音热点宝**扫码登录 → 验活 → 写入加密库**(2026-11-06)。

## 为什么需要它
`douhot.douyin.com` 是**独立的 OAuth 站点**,不吃 `www.douyin.com` 的登录态:
抖音档案里 61 个 cookie 里没有 `sessionid_douhot`,拿它打榜单接口一律回
`code=8 用户未登录`(2026-11-06 实测)。所以热点宝只能**单独扫码授权一次**。

## 与 `douyin` 凭据**分开存**
`user_cookies` 里 `douyin` 那一枚是**抖音搜索**在生产里用的(`douyin_leads`、
`douyin_cdp_source` 都读它);把热点宝的 cookie 覆盖进去会把搜索打挂。
本脚本写的是**新平台键 `douhot`**,`run_douhot` 优先读它、读不到才回落 `douyin`。

## 用法
    python scripts/douhot_login.py                 # 开 Edge,等人扫码(默认等 15 分钟)
    python scripts/douhot_login.py --wait 1800
    python scripts/douhot_login.py --import-only   # 档案里已授权,只导入并验活

## 纪律
- 判据是**服务端**:`fetch_search_words` 真拉回条目才算成功 ——
  档案里有 `sessionid_douhot` 不代表服务端认(与微博那次 `SUB` 假绿灯同源);
- 只往加密库 + `DOUHOT_COOKIE_FILE` 写,**不打印** cookie 值;
- ⚠️ 扫完码先别在别的客户端试(微博那次扫码后会话被立刻销过)。
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
DEFAULT_PROFILE = ROOT / "tools" / "MediaCrawler" / "browser_data" / "cdp_douhot_user_data_dir"
HOME = "https://douhot.douyin.com/square/trend?active_tab=hotword_all"

#: 热点宝登录态本体。实测它在**独立域**上签发,名字带 `_douhot` 后缀。
CORE = ("sessionid_douhot", "sessionid_ss_douhot", "sid_tt_douhot", "uid_tt_douhot")


def _merge(jar: list[dict]) -> dict[str, str]:
    """合成 `{name: value}`;**douhot 域的同名项优先**(我们只往那个域发请求)。"""
    out: dict[str, str] = {}
    for c in jar:
        if c.get("name"):
            out[c["name"]] = c["value"]
    for c in jar:
        if (c.get("domain") or "").lstrip(".").endswith("douhot.douyin.com") and c.get("name"):
            out[c["name"]] = c["value"]
    return out


def _jar_of(ctx) -> dict[str, str]:
    jar: list[dict] = []
    for url in ("https://douhot.douyin.com", "https://www.douyin.com", "https://douyin.com"):
        try:
            jar.extend(ctx.cookies(url))
        except Exception:  # noqa: BLE001 - 单域取不到不影响其余
            pass
    return _merge(jar)


def _verify(ck: dict[str, str]) -> tuple[bool, str]:
    """**真打一次榜单接口**。只有拉回条目才算登录成功。"""
    from config.settings import get_settings
    from app.services import douhot

    if not ck:
        return False, "一个 cookie 都没有"
    if not any(k in ck for k in CORE):
        return False, f"缺热点宝登录项({', '.join(CORE)})"
    blob = "; ".join(f"{k}={v}" for k, v in ck.items())
    try:
        rows = douhot.fetch_search_words(blob, get_settings())
    except Exception as exc:  # noqa: BLE001 - 验活失败只影响判定
        return False, f"{type(exc).__name__}: {str(exc)[:80]}"
    if not rows:
        return False, "接口回空(可能已登录但榜单为空;再等等)"
    return True, f"搜索榜拉回 {len(rows)} 条"


def _read_cookies(profile: Path) -> dict[str, str]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(profile), headless=True, no_viewport=True,
            channel="msedge", executable_path=EDGE)
        try:
            return _jar_of(ctx)
        finally:
            ctx.close()


def _wait_login(profile: Path, wait_s: int) -> dict[str, str]:
    """开浏览器等人扫码。**不做 settle 复验**:热点宝的会话不像微博那样几十秒就被销。"""
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
            print(f"→ 窗口已打开,请用抖音 App 扫码授权(最多等 {wait_s // 60} 分钟)", flush=True)
            deadline = time.time() + wait_s
            last = ""
            while time.time() < deadline:
                time.sleep(5)
                ck = _jar_of(ctx)
                if not any(k in ck for k in CORE):
                    continue
                ok, why = _verify(ck)
                if ok:
                    print(f"✓ {why}", flush=True)
                    return ck
                if why != last:
                    print(f"… {why}", flush=True)
                    last = why
            raise TimeoutError(f"等了 {wait_s} 秒仍未登录")
        finally:
            ctx.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default=str(DEFAULT_PROFILE))
    ap.add_argument("--wait", type=int, default=900)
    ap.add_argument("--user", type=int, default=1)
    ap.add_argument("--import-only", action="store_true")
    args = ap.parse_args()
    profile = Path(args.profile)

    try:
        ck = _read_cookies(profile) if args.import_only else _wait_login(profile, args.wait)
    except Exception as exc:  # noqa: BLE001 - 把原因打给人看
        print(f"✗ 登录失败:{type(exc).__name__}: {exc}", flush=True)
        return 1
    if not ck:
        print("✗ 一个 cookie 都没读到", flush=True)
        return 1

    ok, why = _verify(ck)
    if not ok:
        print(f"✗ 不写库:{why}", flush=True)
        return 1

    blob = "; ".join(f"{k}={v}" for k, v in ck.items())

    from config.settings import get_settings
    from app.db.database import get_session_local, init_db
    from app.services.cookie_store import set_cookie

    # 1) 文件(运维更新入口,`run_douhot` 的自愈读它)
    settings = get_settings()
    path = Path(getattr(settings, "douhot_cookie_file", "data/douhot_cookie.txt"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(blob, encoding="utf-8")
    print(f"✓ 已写入 {path}({len(blob)} 字节)", flush=True)

    # 2) 加密库(运行时读取口,平台键 = douhot)
    init_db()
    db = get_session_local()()
    try:
        set_cookie(db, args.user, "douhot", blob)
        db.commit()
    finally:
        db.close()
    print(f"✓ 已写入 douhot 凭据({len(ck)} 个 cookie,服务端已确认)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
