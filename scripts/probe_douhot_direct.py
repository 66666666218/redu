"""探测:douhot 内容词接口能否"去浏览器化"直连(裸 requests)。

背景:`app/services/douhot.py` 目前靠无头 Chromium 打开热点页拦截响应,单次采集 ~15s、
内存吃紧。若接口只依赖 Cookie(+ 固定业务参数),即可改为 requests 直连,性能大幅提升。

本脚本分两阶段:
  1) capture:用 Playwright 真实打开页面,抓下 query_list 的完整请求(URL/参数/请求头),
     落盘 data/douhot_capture.json。
  2) replay :用 requests 按若干"剥离档位"复放,判断到底哪些参数是必须的:
       A 原样复放          —— 验证复放通道本身是否成立
       B 去 a_bogus        —— 关键:签名是否真的必须
       C 去 a_bogus+msToken
       D 只留业务参数      —— 去掉所有指纹类参数
       E 只留业务参数+改参 —— 换 query_day/page,验证不是"重放旧签名"侥幸成功

用法:
    python scripts/probe_douhot_direct.py            # 抓包 + 复放
    python scripts/probe_douhot_direct.py --replay   # 复用已有抓包结果,只复放
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

import requests

sys.stdout.reconfigure(encoding="utf-8")  # Windows 控制台默认 GBK,打不出 ✓/→

URL = "https://douhot.douyin.com/square/trend?active_tab=hotword_all"
QUERY_API = "/douhot/v1/dashboard/hot_word/query_list"
COOKIE_FILE = Path("data/douhot_cookie.txt")
CAPTURE_FILE = Path("data/douhot_capture.json")
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36 Edg/147.0.0.0"
)
# 指纹/签名类参数(非业务语义),逐档剥离用
FINGERPRINT_KEYS = {
    "a_bogus", "msToken", "X-Bogus", "_signature", "verifyFp", "fp",
    "webid", "web_id", "device_id", "screen_width", "screen_height",
    "browser_language", "browser_platform", "browser_name", "browser_version",
    "browser_online", "engine_name", "engine_version", "os_name", "os_version",
    "cpu_core_num", "device_memory", "platform", "downlink", "effective_type",
    "round_trip_time", "pc_client_type", "version_code", "version_name",
    "cookie_enabled", "referer",
}


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


def capture(cookie: str) -> dict:
    """浏览器跑一次,抓下 query_list 的真实请求。"""
    from playwright.sync_api import sync_playwright

    hits: list[dict] = []

    def on_request(request) -> None:  # type: ignore[no-untyped-def]
        if QUERY_API in request.url:
            try:
                headers = request.all_headers()
            except Exception:  # noqa: BLE001
                headers = dict(request.headers)
            hits.append(
                {
                    "url": request.url,
                    "method": request.method,
                    "headers": headers,
                    "post_data": request.post_data,
                }
            )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=_UA, viewport={"width": 1400, "height": 900})
        context.add_cookies(to_playwright_cookies(cookie))
        page = context.new_page()
        page.on("request", on_request)
        print(f"→ 打开 {URL}")
        page.goto(URL, timeout=60_000)
        page.wait_for_timeout(9_000)
        browser.close()

    if not hits:
        sys.exit("✗ 未抓到 query_list 请求(Cookie 可能已失效,或页面结构变了)")
    print(f"→ 抓到 {len(hits)} 个 query_list 请求,取第 1 个")
    CAPTURE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CAPTURE_FILE.write_text(json.dumps(hits[0], ensure_ascii=False, indent=2), encoding="utf-8")
    return hits[0]


def summarize(body: str) -> str:
    """把响应压成一行判据:状态码 / word_list 条数 / 首条词(用于分辨换参后数据是否真的变了)。"""
    try:
        obj = json.loads(body)
    except Exception:  # noqa: BLE001
        return f"非 JSON,前 120 字:{body[:120]!r}"
    data = obj.get("data") or {}
    words = data.get("word_list") if isinstance(data, dict) else None
    status = obj.get("status_code", obj.get("code", obj.get("err_no")))
    msg = obj.get("status_msg") or obj.get("message") or obj.get("msg") or ""
    head = ""
    if isinstance(words, list) and words:
        head = f" 首条={str(words[0].get('word') or words[0].get('title') or '')[:12]!r}"
    return (
        f"status={status} msg={msg!r} "
        f"word_list={len(words) if isinstance(words, list) else 'None'}{head}"
    )


def replay(
    cap: dict,
    cookie: str,
    label: str,
    drop: set[str],
    body_override: dict | None = None,
) -> bool:
    """按档位剥离参数后复放一次,打印判据。返回是否拿到非空 word_list。

    `body_override` 用于改写 POST JSON 体里的业务参数(page_num/date_window 等),
    验证不是"重放旧签名"侥幸成功。
    """
    split = urlsplit(cap["url"])
    params = [(k, v) for k, v in parse_qsl(split.query, keep_blank_values=True) if k not in drop]
    url = f"{split.scheme}://{split.netloc}{split.path}"
    if params:
        url = f"{url}?{urlencode(params)}"

    body = cap.get("post_data")
    if body_override and body:
        try:
            obj = json.loads(body)
            obj.update(body_override)
            body = json.dumps(obj, separators=(",", ":"))
        except Exception:  # noqa: BLE001
            pass

    headers = {
        k: v
        for k, v in cap["headers"].items()
        # 逐跳/自动生成的头交给 requests 处理;cookie 用文件里的最新值
        if not k.startswith(":") and k.lower() not in {"accept-encoding", "content-length", "cookie", "host"}
    }
    headers["cookie"] = cookie

    return _fire(cap["method"], url, headers, body, label)


def minimal(cookie: str, body: dict, label: str) -> bool:
    """完全手工构造的最小请求:不复用任何抓包头,不带任何 query 参数。"""
    return _fire(
        "POST",
        f"https://douhot.douyin.com{QUERY_API}",
        {
            "user-agent": _UA,
            "content-type": "application/json",
            "referer": URL,
            "cookie": cookie,
        },
        json.dumps(body, separators=(",", ":")),
        label,
    )


def _fire(method: str, url: str, headers: dict, body: str | None, label: str) -> bool:
    try:
        resp = requests.request(method, url, headers=headers, data=body, timeout=20)
    except Exception as exc:  # noqa: BLE001
        print(f"  {label:<24} 请求异常 {type(exc).__name__}: {str(exc)[:100]}")
        return False

    verdict = summarize(resp.text)
    ok = "word_list=" in verdict and not verdict.endswith(("word_list=0", "word_list=None"))
    print(f"  {label:<24} HTTP {resp.status_code} | {verdict}")
    return ok


def main() -> None:
    cookie = load_cookie()
    if "--replay" in sys.argv and CAPTURE_FILE.exists():
        cap = json.loads(CAPTURE_FILE.read_text(encoding="utf-8"))
        print(f"→ 复用抓包结果 {CAPTURE_FILE}")
    else:
        cap = capture(cookie)

    split = urlsplit(cap["url"])
    keys = [k for k, _ in parse_qsl(split.query, keep_blank_values=True)]
    print(f"\n接口:{cap['method']} {split.path}")
    print(f"查询参数({len(keys)}):{keys}")
    print(f"请求体:{(cap.get('post_data') or '(无)')[:200]}")
    biz = [k for k in keys if k not in FINGERPRINT_KEYS]
    print(f"业务参数:{biz}\n")

    print("=== 复放结果 ===")
    base_body = json.loads(cap.get("post_data") or "{}")
    results = {
        "A 原样复放": replay(cap, cookie, "A 原样复放", set()),
        "B 去签名参数": replay(cap, cookie, "B 去签名参数", {"a_bogus", "X-Bogus", "_signature"}),
        "C 再去 msToken": replay(cap, cookie, "C 再去 msToken", {"a_bogus", "X-Bogus", "_signature", "msToken"}),
        "D 仅业务参数": replay(cap, cookie, "D 仅业务参数", FINGERPRINT_KEYS),
        "E 改 body 换页": replay(cap, cookie, "E 改 body 换页", FINGERPRINT_KEYS, body_override={"page_num": 2}),
        "F 最小手工请求": minimal(cookie, base_body, "F 最小手工请求"),
        "G 最小+改窗口": minimal(cookie, {**base_body, "date_window": 168}, "G 最小+改窗口"),
    }

    print("\n=== 结论 ===")
    if results["F 最小手工请求"] and results["G 最小+改窗口"]:
        print("✓ 可直连:纯 requests + Cookie 即可,且换参数照样返回真实数据 → 去浏览器化改造成立")
    elif results["D 仅业务参数"]:
        print("~ 大体可直连:剥掉签名参数能通,但最小手工请求失败,需比对缺哪个头")
    elif results["B 去签名参数"]:
        print("~ 半直连:不需要签名参数,但仍依赖抓包头/msToken 等浏览器产物")
    elif results["A 原样复放"]:
        print("✗ 必须签名:原样复放可用,去掉签名参数即失败 → 需实现 a_bogus")
    else:
        print("? 复放通道本身不成立(Cookie 失效/风控/TLS 指纹),先排查再下结论")


if __name__ == "__main__":
    main()
