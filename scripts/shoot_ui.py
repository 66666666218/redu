"""截取关键页面,人工核对布局与配色(dataviz 规范第 7 步:渲染出来看)。

登录后把 token 写入 localStorage,依次截图 登录页 / 仪表盘 / 采集频率 / 管理后台(图表)。
产物落在 data/shots/(gitignored)。

用法(先起服务,凭据走环境变量,不写进仓库):
    UI_BASE=http://127.0.0.1:8097 UI_LOGIN=you@example.com UI_PASSWORD=xxx \
        python scripts/shoot_ui.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

sys.stdout.reconfigure(encoding="utf-8")

BASE = os.getenv("UI_BASE", "http://127.0.0.1:8097")
LOGIN = os.getenv("UI_LOGIN", "")
PASSWORD = os.getenv("UI_PASSWORD", "")
OUT = Path("data/shots")
OUT.mkdir(parents=True, exist_ok=True)
PAGES = [("login", "/login"), ("dashboard", "/"), ("schedule", "/schedule"), ("admin", "/admin")]


def get_token() -> str:
    if not LOGIN or not PASSWORD:
        sys.exit("请设置 UI_LOGIN / UI_PASSWORD 环境变量(勿把账号密码写进脚本)")
    r = requests.post(f"{BASE}/api/auth/login", json={"login": LOGIN, "password": PASSWORD}, timeout=15)
    r.raise_for_status()
    return r.json()["token"]


def main() -> None:
    token = get_token()
    print("登录成功,开始截图")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1440, "height": 950}, device_scale_factor=1)
        page = ctx.new_page()
        page.goto(BASE + "/login")
        page.evaluate("t => localStorage.setItem('token', t)", token)
        for name, path in PAGES:
            page.goto(BASE + path)
            page.wait_for_timeout(1800)          # 等接口返回与入场动画结束
            out = OUT / f"{name}.png"
            page.screenshot(path=str(out), full_page=(name != "login"))
            print(f"  {name:<10} {path:<12} -> {out}")
        browser.close()


if __name__ == "__main__":
    main()
