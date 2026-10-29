#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""知乎**浏览器窗口扫码登录** → 自动导出 Cookie 并写库(2026-10-29)。

## 为什么需要它

2026-10-29 实测:库里那枚 zhihu Cookie 调 `api/v4/search_v3` **直接 403**(连默认参数
都拿不到数据)⇒ 知乎这条源实际上是停的,而它的资源又最旧(7 天内只占 6.5%)。
网页登录页的扫码是完整跑通的 —— 所以**弹窗让人扫**,我们在旁边等 Cookie 出现。

**判据(两道都要)**:
  ① 浏览器里出现 `z_c0`(知乎登录态的核心 cookie);
  ② 拿这枚 Cookie 打一次 `search_v3` 回 200 且有 `data` —— 只有①不够,
     "半登录"会让接口继续 403(与百度 BDUSS/STOKEN 那次同一条教训)。

跑法:
    python scripts/zhihu_browser_login.py
    python scripts/zhihu_browser_login.py --deadline 900

⚠️ 用**一次性浏览器档案**(系统临时目录):固定档案里可能已经登着别的号,
   一打开就"已登录",会把旧 Cookie 再存一遍 —— 看起来成功,实际白扫。
⚠️ Cookie **不打印、不落文件**,只写加密库。
"""
from __future__ import annotations

import argparse
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

SIGNIN = "https://www.zhihu.com/signin"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def _cfg(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="弹出知乎登录窗口,扫码后自动导出 Cookie 写库")
    ap.add_argument("--user", type=int, default=0, help="归属用户(默认:第一个启用用户)")
    ap.add_argument("--deadline", type=float, default=540, help="等待登录秒数(默认 540)")
    return ap.parse_args(argv)


def _cookie_header(ctx) -> str:
    """把 zhihu.com 的 cookie 拼成请求头;**同名优先留宽域** `.zhihu.com` 那份。"""
    out: dict[str, str] = {}
    for c in ctx.cookies():
        if not str(c.get("domain") or "").endswith("zhihu.com"):
            continue
        name = str(c.get("name") or "")
        value = str(c.get("value") or "")
        domain = str(c.get("domain") or "")
        if not name:
            continue
        if name not in out or domain == ".zhihu.com":
            out[name] = value
    return "; ".join(f"{k}={v}" for k, v in out.items() if v)


def _has_login(ctx) -> bool:
    return any(str(c.get("name")) == "z_c0" and str(c.get("value") or "")
               for c in ctx.cookies() if str(c.get("domain") or "").endswith("zhihu.com"))


def _verify(cookie: str) -> tuple[bool, str]:
    """业务接口验活 —— 只有这一步过了才算真的登录。"""
    import requests

    try:
        resp = requests.get(
            "https://www.zhihu.com/api/v4/search_v3",
            params={"t": "general", "q": "网盘资源", "limit": 5},
            headers={"Cookie": cookie, "User-Agent": _UA,
                     "Referer": "https://www.zhihu.com/",
                     "Accept": "application/json, text/plain, */*"},
            timeout=20)
    except Exception as exc:  # noqa: BLE001
        return False, f"请求异常:{exc}"
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}"
    try:
        data = resp.json()
    except ValueError:
        return False, f"非 JSON:{resp.text[:80]}"
    items = data.get("data") if isinstance(data, dict) else None
    if not items:
        return False, f"返回体无 data:{str(data)[:100]}"
    return True, f"search_v3 OK,返回 {len(items)} 条"


def main(argv: list[str] | None = None) -> int:
    args = _cfg(argv)
    init_db()
    db = get_session_local()()
    try:
        uid = args.user or (db.scalar(select(User.id).where(User.enabled.is_(True))) or 1)
        from playwright.sync_api import sync_playwright

        tmp = tempfile.mkdtemp(prefix="zhihu_login_")
        print(f"拉起浏览器(一次性档案:{tmp})…", flush=True)
        with sync_playwright() as p:
            try:
                ctx = p.chromium.launch_persistent_context(
                    tmp, channel="msedge", headless=False,
                    args=["--start-maximized"], no_viewport=True)
            except Exception:  # noqa: BLE001 - 没装 Edge 就退回自带 Chromium
                ctx = p.chromium.launch_persistent_context(tmp, headless=False,
                                                           no_viewport=True)
            try:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto(SIGNIN, wait_until="domcontentloaded", timeout=60000)
                print("👉 请在刚弹出的窗口里**扫码登录知乎**(登录页默认有「扫码登录」)。", flush=True)
                print(f"   等待登录中,最多 {int(args.deadline)} 秒…", flush=True)
                deadline = time.time() + max(30, float(args.deadline))
                ok = False
                while time.time() < deadline:
                    time.sleep(3)
                    if _has_login(ctx):
                        ok = True
                        break
                if not ok:
                    print("⏰ 超时:没有看到 z_c0。请重新运行本脚本再扫一次。")
                    return 2
                #: 登录刚完成时 cookie 可能还在写,等两秒再导
                time.sleep(2)
                ck = _cookie_header(ctx)
            finally:
                ctx.close()
        if not ck:
            print("⚠️ 浏览器里拿到了登录态,但**导出的 Cookie 为空** —— 这不是成功,请重试。")
            return 3
        good, note = _verify(ck)
        if not good:
            print(f"⚠️ 已看到 z_c0,但业务接口仍不通({note})。"
                  "别急着存 —— 这很可能是「半登录」;请在窗口里完成一次任意页面访问后重试。")
            return 4
        from app.services.cookie_store import set_cookie

        set_cookie(db, int(uid), "zhihu", ck)
        print(f"✅ 已写入 zhihu Cookie(加密库,user={uid});{note}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
