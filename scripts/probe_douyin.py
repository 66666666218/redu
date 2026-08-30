"""抖音指数(巨量算数)可行性探测脚本。

用抖音创作中心登录 Cookie(取自 data/douyin_cookie.txt)注入 Chromium,
打开算术指数页,判断:能否登录进入、是否有搜索框、搜索关键词后是否拦截到指数接口。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

URL = "https://creator.douyin.com/creator-micro/creator-count/arithmetic-index"
KEYWORD = sys.argv[1] if len(sys.argv) > 1 else "世界杯"
COOKIE_FILE = Path("data/douyin_cookie.txt")


def load_cookies() -> list[dict]:
    raw = COOKIE_FILE.read_text(encoding="utf-8").strip()
    out: list[dict] = []
    for pair in raw.split(";"):
        pair = pair.strip()
        if "=" not in pair:
            continue
        name, _, value = pair.partition("=")
        out.append({"name": name.strip(), "value": value.strip(), "domain": ".douyin.com", "path": "/"})
    return out


def main() -> None:
    captured: list[dict] = []

    def on_response(response) -> None:  # type: ignore[no-untyped-def]
        try:
            body = response.text()
        except Exception:  # type: ignore[no-untyped-def]
            body = ""
        captured.append(
            {
                "url": response.url,
                "ct": response.headers.get("content-type", ""),
                "path": response.url.split("?")[0],
                "body": body,
            }
        )

    cookies = load_cookies()
    print(f"→ 载入 Cookie {len(cookies)} 条")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36 Edg/147.0.0.0"
        ))
        context.add_cookies(cookies)
        page = context.new_page()
        page.on("response", on_response)
        print(f"→ 打开 {URL}")
        page.goto(URL, timeout=60_000)
        page.wait_for_timeout(8_000)
        print("标题:", page.title())
        print("当前 URL:", page.url)

        inputs = page.query_selector_all("input")
        print(f"input 数量: {len(inputs)}")
        for i, el in enumerate(inputs[:6]):
            ph = el.get_attribute("placeholder") or el.get_attribute("name") or ""
            print(f"   input[{i}] placeholder/name={ph!r}")

        body = page.inner_text("body")[:1500]
        for kw in ("请登录", "扫码登录", "登录"):
            if kw in body:
                print(f"⚠ 仍见登录相关: {kw}")

        try:
            page.fill("input", KEYWORD)
            page.press("input", "Enter")
            page.wait_for_timeout(6_000)
            print(f"→ 已尝试搜索: {KEYWORD}")
        except Exception as exc:  # noqa: BLE001
            print(f"⚠ 无法自动搜索: {type(exc).__name__}: {str(exc)[:120]}")

        browser.close()

    api = [c for c in captured if c["ct"].startswith("application/json")]
    print(f"\n拦截 response 共 {len(captured)} 个,其中 JSON {len(api)} 个")
    for c in api:
        print("  JSON:", c["path"][:150])
    print("\n=== /api/v2/index 请求与响应体(前 260 字)===")
    seen_idx: set[str] = set()
    for c in api:
        if "/api/v2/index/" not in c["path"]:
            continue
        q = c["path"]
        if q in seen_idx:
            continue
        seen_idx.add(q)
        print("── ", q.split("/api/v2/index/")[-1][:70])
        print("    ", c["body"][:260].replace("\n", " "))
    Path("douyin_captured.json").write_text(
        json.dumps(captured, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
