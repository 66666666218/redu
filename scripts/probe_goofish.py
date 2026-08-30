"""闲鱼 mtop 接口探测脚本。

用登录 Cookie(取自 data/goofish_cookie.txt)打开闲鱼,抓取页面发起的
mtop(h5api.m.taobao.com)请求,还原真实 api/v/data,用于确认"搜索/热卖"接口。
"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from playwright.sync_api import sync_playwright

COOKIE_FILE = Path("data/goofish_cookie.txt")
SEARCH = "ps教程"


def load_cookies() -> list[dict]:
    raw = COOKIE_FILE.read_text(encoding="utf-8").strip()
    doms = [".taobao.com", ".goofish.com", "h5api.m.taobao.com"]
    out: list[dict] = []
    for pair in raw.split(";"):
        pair = pair.strip()
        if "=" not in pair:
            continue
        name, _, value = pair.partition("=")
        for d in doms:
            out.append({"name": name.strip(), "value": value.strip(), "domain": d, "path": "/"})
    return out


def main() -> None:
    captured: list[dict] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=(
            "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1"
        ))
        context.add_cookies(load_cookies())
        page = context.new_page()

        def on_request(req) -> None:  # type: ignore[no-untyped-def]
            if "h5api.m.taobao.com" in req.url or "mtop" in req.url:
                captured.append({"url": req.url, "method": req.method, "post": req.post_data or ""})

        page.on("request", on_request)
        for url in ["https://www.goofish.com/search?q=" + SEARCH, "https://www.goofish.com/"]:
            print("→ 打开", url)
            try:
                page.goto(url, timeout=60_000)
                page.wait_for_timeout(6_000)
            except Exception as exc:  # noqa: BLE001
                print("   ERR:", str(exc)[:100])
            if len(captured) > 0:
                break
        browser.close()

    print(f"\n捕获 h5api/mtop 请求 {len(captured)} 个:")
    seen = set()
    for c in captured:
        q = parse_qs(urlparse(c["url"]).query)
        api = q.get("api", ["?"])[0]
        v = q.get("v", ["?"])[0]
        data = q.get("data", [""])[0]
        if api in seen:
            continue
        seen.add(api)
        print(f"  api={api} v={v}")
        print(f"    data={data[:220]}")
        if c["post"]:
            print(f"    POST={c['post'][:220]}")
    Path("goofish_captured.json").write_text(json.dumps(captured, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
