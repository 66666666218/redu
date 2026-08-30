"""抖音指数(巨量算数)可行性探测脚本。

驱动无头 Chromium 打开巨量算数算术指数页,判断:
1. 是否登录墙/需要抖音登录;
2. 是否有搜索框可输入关键词;
3. 搜索后是否拦截到返回指数数据的 XHR/JSON。
"""
from __future__ import annotations

import json
import sys

from playwright.sync_api import sync_playwright

URL = "https://trendinsight.oceanengine.com/arithmetic-index/index"
KEYWORD = sys.argv[1] if len(sys.argv) > 1 else "世界杯"


def main() -> None:
    captured: list[dict] = []

    def on_response(response) -> None:  # type: ignore[no-untyped-def]
        captured.append(
            {
                "url": response.url,
                "ct": response.headers.get("content-type", ""),
                "path": response.url.split("?")[0],
            }
        )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.on("response", on_response)
        print(f"→ 打开 {URL}")
        page.goto(URL, timeout=60_000)
        page.wait_for_timeout(6_000)
        print("标题:", page.title())
        print("当前 URL:", page.url)

        # 是否有搜索输入框
        inputs = page.query_selector_all("input")
        print(f"input 数量: {len(inputs)}")
        for i, el in enumerate(inputs[:5]):
            ph = el.get_attribute("placeholder") or el.get_attribute("name") or ""
            print(f"   input[{i}] placeholder/name={ph!r}")

        # 页面文字是否出现登录相关
        body_text = page.inner_text("body")[:2000]
        for kw in ("登录", "请先登录", "未登录", "扫码"):
            if kw in body_text:
                print(f"⚠ 页面含登录相关字样: {kw}")
        try:
            page.fill("input", KEYWORD)
            page.press("input", "Enter")
            page.wait_for_timeout(5_000)
            print(f"→ 已尝试搜索: {KEYWORD}")
        except Exception as exc:  # noqa: BLE001
            print(f"⚠ 无法自动输入搜索: {type(exc).__name__}: {str(exc)[:120]}")
        browser.close()

    # 分析拦截到的响应
    api_responses = [c for c in captured if c["ct"].startswith("application/json")]
    print(f"\n拦截到 response 共 {len(captured)} 个,其中 JSON {len(api_responses)} 个")
    for c in api_responses:
        print("  JSON:", c["path"][:160])

    # 把网络清单写入文件便于进一步分析
    with open("douyin_captured.json", "w", encoding="utf-8") as f:
        json.dump(captured, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
