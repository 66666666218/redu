"""实测:闲鱼 mtop 搜索用纯协议能否避开滑块(get/user-validate)。

背景:
- 现状 `XianyuClient` 用纯 `requests`(Python urllib3 TLS 指纹,极像机器人),
  现场实测触发 `FAIL_SYS_USER_VALIDATE`(人机验证/滑块),且退避重试无效。
- 主流闲鱼采集器(goofish_spider / cn-scraper-mcp)改用 `curl_cffi` **模拟 Chrome
  TLS/HTTP2 指纹**,冒充浏览器从协议层发出,以降低被风控标记的概率。

本脚本用**同一个 Cookie、同一份 mtop 签名**,分别用:
  A) `requests.Session`(现状)
  B) `curl_cffi` impersonate='chrome'(伪浏览器指纹)
各发一次搜索,打印 ret 码,对比是否 A 触发滑块而 B 通过。

用法: python scripts/probe_xianyu_curl.py
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from curl_cffi import requests as curl_requests

COOKIE_FILE = Path("data/goofish_cookie.txt")
H5_BASE = "https://h5api.m.goofish.com/h5"
API = "mtop.taobao.idlemtopsearch.pc.search"
APP_KEY = "34839810"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "en,zh-CN;q=0.9,zh;q=0.8",
    "sec-ch-ua": '"Chromium";v="147", "Not.A/Brand";v="8", "Google Chrome";v="147"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Origin": "https://www.goofish.com",
    "Referer": "https://www.goofish.com/",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
    "priority": "u=1, i",
    "Content-Type": "application/x-www-form-urlencoded",
}
PAYLOAD = {
    "pageNumber": 1, "keyword": "ps教程", "fromFilter": False, "rowsPerPage": 30,
    "sortValue": "", "sortField": "", "customDistance": "", "gps": "", "propValueStr": {},
    "customGps": "", "searchReqFromPage": "pcSearch", "extraFilterValue": "{}", "userPositionJson": "{}",
}


def load_cookie() -> str:
    return COOKIE_FILE.read_text(encoding="utf-8").strip()


def token(cookie: str) -> str:
    m = re.search(r"_m_h5_tk=([0-9a-f]+)_", cookie)
    return m.group(1) if m else ""


def sign(t: str, tok: str, data: str) -> str:
    return hashlib.md5(f"{tok}&{t}&{APP_KEY}&{data}".encode()).hexdigest()


def build_params() -> dict:
    data = json.dumps(PAYLOAD, ensure_ascii=False, separators=(",", ":"))
    t = str(int(time.time() * 1000))
    return {
        "params": {
            "jsv": "2.7.2", "appKey": APP_KEY, "t": t, "sign": sign(t, tok, data),
            "v": "1.0", "type": "originaljson", "accountSite": "xianyu", "dataType": "json",
            "timeout": "20000", "api": API, "sessionOption": "AutoLoginOnly",
        },
        "data": {"data": data},
    }


def seed_requests_session(cookie: str):
    """A) 现状:纯 requests.Session。"""
    import requests

    s = requests.Session()
    s.headers.update({"User-Agent": UA, **HEADERS})
    from requests.cookies import RequestsCookieJar, create_cookie

    jar = RequestsCookieJar()
    for pair in cookie.split("; "):
        if "=" in pair:
            name, _, val = pair.partition("=")
            jar.set_cookie(create_cookie(name.strip(), val.strip(), domain=".goofish.com", path="/"))
    s.cookies = jar
    return s


def seed_curl_session(cookie: str):
    """B) 伪 Chrome 指纹:curl_cffi.Session(impersonate='chrome')。"""
    s = curl_requests.Session(impersonate="chrome")
    s.headers.update({"User-Agent": UA, **HEADERS})
    for pair in cookie.split("; "):
        if "=" in pair:
            name, _, val = pair.partition("=")
            s.cookies.set(name.strip(), val.strip(), domain=".goofish.com", path="/")
    return s


def call(label: str, session, is_curl: bool) -> None:
    p = build_params()
    url = f"{H5_BASE}/{API}/1.0/"
    try:
        if is_curl:
            resp = session.post(url, params=p["params"], data=p["data"], timeout=20)
        else:
            resp = session.post(url, params=p["params"], data=p["data"], timeout=20)
        ret = resp.json().get("ret", [""])[0]
        setc = resp.headers.get("set-cookie", "") or ""
        new_tok = bool(re.search(r"_m_h5_tk=([0-9a-f]{32})_", setc))
        print(f"  {label:<22} HTTP{resp.status_code} ret={ret}  下发新token={new_tok}")
    except Exception as exc:  # noqa: BLE001
        print(f"  {label:<22} 异常 {type(exc).__name__}: {exc}")


def main() -> None:
    cookie = load_cookie()
    global tok
    tok = token(cookie)
    print(f"cookie 内 _m_h5_tk 长度 = {len(tok)} ({'有' if tok else '空'})")
    print("=== 第 1 次:A requests(现状) vs B curl_cffi ===")
    call("A requests(现状)", seed_requests_session(cookie), False)
    call("B curl_cffi@chrome", seed_curl_session(cookie), True)
    print("\n=== 第 2 次(再验,排除偶发):A vs B ===")
    call("A2 requests", seed_requests_session(cookie), False)
    call("B2 curl_cffi", seed_curl_session(cookie), True)
    print("\n=== 判据 ===")
    print("  若 A 触发 USER_VALIDATE 而 B 通过 → curl_cffi 伪指纹能绕开滑块,值得集成")
    print("  若两者都 USER_VALIDATE → 指纹不是主因,账号/IP 已被标记,需换清洁出口 IP")


if __name__ == "__main__":
    main()
