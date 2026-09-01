"""抓取 douhot 各榜单接口的真实请求(URL/请求体),并逐个做"最小直连"复放。

`probe_douhot_direct.py` 已证明内容词接口不需要 a_bogus;本脚本把结论推广到其余榜单
(搜索榜/视频榜/话题榜/订阅),为 `app/services/douhot.py` 去浏览器化改造提供请求体规格。

产物:data/douhot_endpoints.json —— {path: {method, url, body}},即各接口的调用规格。

用法:
    python scripts/probe_douhot_apis.py            # 抓包 + 复放
    python scripts/probe_douhot_apis.py --replay   # 只复放已抓到的接口
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import requests

sys.stdout.reconfigure(encoding="utf-8")

COOKIE_FILE = Path("data/douhot_cookie.txt")
OUT_FILE = Path("data/douhot_endpoints.json")
API_MARK = "/douhot/v1/"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36 Edg/147.0.0.0"
)
# (页面, 进入后要点的子Tab) —— 子Tab 触发对应榜单接口
PAGES = [
    ("https://douhot.douyin.com/square/trend?active_tab=hotword_all", None),
    ("https://douhot.douyin.com/square/hotspot?active_tab=hotspot_all", None),
    ("https://douhot.douyin.com/square/hotspot?active_tab=hotspot_all", "视频榜"),
    ("https://douhot.douyin.com/square/hotspot?active_tab=hotspot_all", "话题榜"),
]


def load_cookie() -> str:
    if not COOKIE_FILE.exists():
        sys.exit(f"缺少 Cookie 文件:{COOKIE_FILE}")
    return COOKIE_FILE.read_text(encoding="utf-8").strip()


def to_playwright_cookies(raw: str) -> list[dict]:
    out: list[dict] = []
    for pair in raw.split(";"):
        pair = pair.strip()
        if "=" not in pair:
            continue
        name, _, value = pair.partition("=")
        out.append({"name": name.strip(), "value": value.strip(), "domain": ".douyin.com", "path": "/"})
    return out


def capture(cookie: str) -> dict[str, dict]:
    """逐页打开并点子Tab,记录所有 /douhot/v1/ 请求(按 path 去重)。"""
    from playwright.sync_api import sync_playwright

    found: dict[str, dict] = {}

    def on_request(request) -> None:  # type: ignore[no-untyped-def]
        if API_MARK not in request.url:
            return
        path = request.url.split("?")[0].split("douhot.douyin.com")[-1]
        found.setdefault(
            path,
            {"method": request.method, "url": request.url, "body": request.post_data, "referer": ""},
        )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=_UA, viewport={"width": 1400, "height": 950})
        context.add_cookies(to_playwright_cookies(cookie))
        for url, tab in PAGES:
            page = context.new_page()
            before = set(found)
            page.on("request", on_request)
            print(f"→ 打开 {url.split('?')[0]}" + (f" 并点击「{tab}」" if tab else ""))
            try:
                page.goto(url, timeout=60_000)
                page.wait_for_timeout(5_000)
                if tab:
                    page.get_by_text(tab, exact=True).first.click(timeout=10_000)
                    page.wait_for_timeout(5_000)
            except Exception as exc:  # noqa: BLE001
                print(f"  ⚠ {type(exc).__name__}: {str(exc)[:100]}")
            for path in set(found) - before:
                found[path]["referer"] = url
            page.close()
        browser.close()

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(found, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"→ 抓到 {len(found)} 个接口,已存 {OUT_FILE}\n")
    return found


def probe(path: str, spec: dict, cookie: str) -> None:
    """最小直连:只带 cookie/UA/content-type/referer,不带任何签名参数。"""
    try:
        resp = requests.request(
            spec["method"],
            f"https://douhot.douyin.com{path}",
            headers={
                "user-agent": _UA,
                "content-type": "application/json",
                "referer": spec.get("referer") or "https://douhot.douyin.com/",
                "cookie": cookie,
            },
            data=spec.get("body"),
            timeout=20,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  {path:<52} 异常 {type(exc).__name__}: {str(exc)[:80]}")
        return

    try:
        obj = resp.json()
    except Exception:  # noqa: BLE001
        print(f"  {path:<52} HTTP {resp.status_code} 非 JSON:{resp.text[:80]!r}")
        return

    lists = _list_fields(obj.get("data"))
    status = obj.get("status_code", obj.get("code"))
    detail = ", ".join(f"{k}={n}" for k, n in lists) or "无列表字段"
    print(f"  {path:<52} HTTP {resp.status_code} status={status} {detail}")


def _list_fields(data: object) -> list[tuple[str, int]]:
    """列出 data 下所有非空数组字段及长度,用于判断有没有真拿到榜单。"""
    if not isinstance(data, dict):
        return []
    return [(k, len(v)) for k, v in data.items() if isinstance(v, list) and v]


def main() -> None:
    cookie = load_cookie()
    if "--replay" in sys.argv and OUT_FILE.exists():
        found = json.loads(OUT_FILE.read_text(encoding="utf-8"))
        print(f"→ 复用 {OUT_FILE}({len(found)} 个接口)\n")
    else:
        found = capture(cookie)

    print("=== 各接口请求体 ===")
    for path, spec in found.items():
        print(f"  {path}\n      {spec['method']} body={(spec.get('body') or '(无)')[:160]}")

    print("\n=== 最小直连复放(无签名参数)===")
    for path, spec in found.items():
        probe(path, spec, cookie)


if __name__ == "__main__":
    main()
