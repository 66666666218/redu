"""MediaCrawler 各平台**登录助手**(2026-10-04,由 `tools/tieba_login.py` 泛化而来)。

**为什么需要**:MediaCrawler 自带的扫码流程常卡在"页面上找不到登录入口元素",然后 30 秒超时退出——
根本轮不到你扫码。而它判断"登录了没"其实**只看 Cookie**。所以最省事的办法是:
用**同一个浏览器档案目录**手工登录一次,之后 MediaCrawler 一看 Cookie 齐全就直接跳过登录。

⚠️ **必须登进 MediaCrawler 用的那个 CDP 档案目录**(`cdp_<平台>_user_data_dir`):
MediaCrawler 开着 CDP 模式,连的是 Edge、档案在 `cdp_*`;登到非 cdp 的目录里是**白登**
(实测:登了照样走扫码)。

**跑法**(必须用 MediaCrawler 的 venv,Chromium 版本才与它一致):
    tools/MediaCrawler/.venv/Scripts/python.exe scripts/mc_login.py xhs
    ... 平台代号见下表(ks / tieba / dy / bili / wb / zhihu)

**登录完成的判据**:下面 `NEED` 里每组 cookie **各至少命中一个** —— 直接抄 MediaCrawler
自己的判据(`media_platform/<平台>/login.py` 的 `check_login_state`),免得"我们以为登上了、
它不认"。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

# ⚠️ Windows 控制台默认 GBK,直接 print 非 ASCII 符号(如 ✓)会 UnicodeEncodeError ——
# 而这一崩**崩在登录判定的那一行**,会让人以为是"登录失败"。统一转 utf-8。
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
MC = ROOT / "tools" / "MediaCrawler"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
WAIT_SECONDS = 300
# 本机没 Chrome,用 Edge —— 必须与 MediaCrawler 的 CDP 模式一致
CHANNEL = "msedge"

# 代号 → (中文名, 打开的页面, 判据:"每组各至少命中一个 cookie")
# ⚠️ 判据抄自 MediaCrawler `media_platform/<平台>/login.py` 的 check_login_state
NEED: dict[str, tuple[str, str, list[set[str]]]] = {
    "xhs":   ("小红书", "https://www.xiaohongshu.com/explore", [{"web_session"}]),
    "ks":    ("快手",   "https://www.kuaishou.com/",           [{"passToken"}]),
    "tieba": ("贴吧",   "https://passport.baidu.com/v2/?login", [{"BDUSS"}, {"STOKEN", "PTOKEN"}]),
    "dy":    ("抖音",   "https://www.douyin.com/",              [{"sessionid"}]),
    "bili":  ("B站",    "https://www.bilibili.com/",            [{"SESSDATA"}]),
    "wb":    ("微博",   "https://weibo.com/",                   [{"SUB"}]),
    "zhihu": ("知乎",   "https://www.zhihu.com/",               [{"z_c0"}]),
}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in NEED:
        print("用法:… python scripts/mc_login.py <" + "|".join(NEED) + ">")
        return 2
    code = sys.argv[1]
    label, home, need = NEED[code]
    profile = MC / "browser_data" / f"cdp_{code}_user_data_dir"
    profile.mkdir(parents=True, exist_ok=True)

    print(f"=== {label} 登录助手 ===", flush=True)
    print(f"=== 档案目录:{profile} ===", flush=True)
    print(f"=== 浏览器即将打开,请**登录**(最多等 {WAIT_SECONDS // 60} 分钟;"
          f"**登完直接关掉那个窗口**即可)===", flush=True)

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(profile), headless=False, no_viewport=True,
            channel=CHANNEL, executable_path=EDGE)
        pg = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            pg.goto(home, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:  # noqa: BLE001 - 页面加载慢/被挡都不该让助手崩掉
            print(f"(首页加载异常,不影响登录:{type(exc).__name__})", flush=True)

        # ⚠️ **检测到登录后不退出** —— 窗口开着让你自己关:
        # 万一档案里是**旧的/半失效的**登录态,"一检测到就退"会让你连重新登的机会都没有。
        deadline, ok, last_note = time.time() + WAIT_SECONDS, False, ""
        while time.time() < deadline:
            time.sleep(3)
            try:
                names = {c.get("name") for c in ctx.cookies()}
            except Exception:  # noqa: BLE001 - 窗口被关掉 → 正常结束
                print("=== 窗口已关闭,助手结束 ===", flush=True)
                break
            now_ok = all(g & names for g in need)
            note = "已登录 OK" if now_ok else "等待登录…"
            if note != last_note:
                print(f"=== {note} ===", flush=True)
                last_note = note
            ok = ok or now_ok
        else:
            print(f"=== 等待超时({WAIT_SECONDS}s) ===", flush=True)

        try:
            names = sorted({c.get("name") for c in ctx.cookies()})
            print(f"=== 档案里的 cookie 名:{names[:16]} ===", flush=True)
        except Exception:  # noqa: BLE001
            pass
        print(f"=== 最终:{'登录态已就绪,可以跑 ' + code + ' 采集了' if ok else '未检测到登录态'} ===",
              flush=True)
        try:
            ctx.close()
        except Exception:  # noqa: BLE001
            pass
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
