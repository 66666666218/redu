"""B站扫码登录 —— 抽登录态 cookie 并**直接写进 cookie_store**(2026-10-05)。

用户口径:「**b站之前不都是扫码登入吗,你怎么还要 cookie**」—— 对,不该让人手抄。
跑一次这个脚本,**扫完码就完事**,之后 `cross_accounts._bili_signed_get` 会自动
把这份 cookie 带上(`get_cookies` 里 `platform="bilibili"`)。

跑法:
    python scripts/bili_login.py

**为什么走"借网页"而不是自己拼扫码接口**(与 `scripts/xunlei_login.py` 同一范式):
B站自己的登录页会**自动刷新二维码**(过期了它自己换)、**自己处理各种中间态**
(未扫码/已扫码待确认/已确认)。我们自己轮询 `qrcode/poll` 得把这些状态全实现一遍,
而它们**没一个是"出错"却长得像"没登录"**的 —— 正是本仓最怕的静默失败。
既然浏览器已经在了,让它去跑最稳。

⚠️ **取哪些 cookie**:`SESSDATA`(登录态本体,搜索用)、`bili_jct`(CSRF,写操作用)、
`DedeUserID`(uid)。三个一起存,免得以后要做别的操作又回来补。
⚠️ **判断"登上了没"的判据是 `SESSDATA` 出现**,不是页面跳转了 —— 页面跳转在
"已扫码待确认"阶段也会发生。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# Windows 控制台默认 GBK,直接 print 非 ASCII(如 ✓)会 UnicodeEncodeError
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

LOGIN_URL = "https://passport.bilibili.com/login"
NAV = "https://api.bilibili.com/x/web-interface/nav"
WANT = ("SESSDATA", "bili_jct", "DedeUserID", "buvid3", "buvid4")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _cookie_map(ctx) -> dict[str, str]:
    """把浏览器上下文里 **bilibili 域**的 cookie 摊成 {名: 值}。"""
    out: dict[str, str] = {}
    for c in ctx.cookies():
        dom = str(c.get("domain") or "")
        if "bilibili.com" in dom:
            out[str(c.get("name"))] = str(c.get("value"))
    return out


def _wait_login(ctx, timeout: int = 240) -> dict[str, str]:
    """等 `SESSDATA` 出现。⚠️ 只有它出现才算登录(扫码待确认不算)。"""
    t0 = time.time()
    last_tip = 0.0
    while time.time() - t0 < timeout:
        got = _cookie_map(ctx)
        if got.get("SESSDATA"):
            return got
        left = int(timeout - (time.time() - t0))
        if time.time() - last_tip > 20:              # 每 20 秒提醒一次,别刷屏
            print(f"  … 等你扫码(还剩 {left}s)", flush=True)
            last_tip = time.time()
        time.sleep(1.5)
    return {}


def _verify(cookie: str) -> dict:
    """拿 nav 接口验活:⚠️ B站**失败也回 HTTP 200**,判据只能是 `data.isLogin`。"""
    import requests

    try:
        r = requests.get(NAV, headers={"User-Agent": UA, "Cookie": cookie,
                                       "Referer": "https://www.bilibili.com/"}, timeout=20)
        d = (r.json() or {}).get("data") or {}
        return {"is_login": bool(d.get("isLogin")), "uname": d.get("uname") or "",
                "mid": d.get("mid") or 0, "vip": bool((d.get("vipStatus") or 0))}
    except Exception as exc:  # noqa: BLE001
        return {"is_login": False, "error": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    from playwright.sync_api import sync_playwright

    print("=== B站扫码登录 ===", flush=True)
    print("⚠️ 二维码在**弹出的浏览器窗口**里,不在这个终端(Alt+Tab 找一下)", flush=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context(user_agent=UA, locale="zh-CN")
        pg = ctx.new_page()
        try:
            pg.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:  # noqa: BLE001 - 导航失败也让它继续(页面可能已出来)
            print("(登录页导航异常,继续):", exc, flush=True)

        got = _wait_login(ctx)
        browser.close()

    if not got.get("SESSDATA"):
        print("✗ 超时没拿到 SESSDATA —— 没扫上或没点确认,重跑一次即可", flush=True)
        return 1

    missing = [k for k in WANT if not got.get(k)]
    print(f"=== 抽到:{sorted(got)} ===", flush=True)
    if missing:
        print(f"⚠️ 缺 {missing} —— 搜索还能跑,但**写操作会失败**;建议重扫一次", flush=True)

    cookie = "; ".join(f"{k}={got[k]}" for k in WANT if got.get(k))
    v = _verify(cookie)
    if not v.get("is_login"):
        print(f"✗ 存之前验活没过:{v} —— **不写入**(存一份死的凭据比不存更坏)", flush=True)
        return 1

    from sqlalchemy import select

    from app.db import get_session_local, init_db
    from app.db.models import User
    from app.services.cookie_store import set_cookie

    init_db()
    db = get_session_local()()
    try:
        uid = db.scalar(select(User.id).where(User.enabled.is_(True)).order_by(User.id))
        if not uid:
            print("没有启用的用户", flush=True)
            return 1
        set_cookie(db, uid, "bilibili", cookie)
        print(f"=== ✓ 已加密写入 cookie_store(user={uid}, platform=bilibili) ===", flush=True)
        print(f"=== 验证:{v.get('uname')}(mid={v.get('mid')}) 已登录 ===", flush=True)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
