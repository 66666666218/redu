"""探测:夸克 update_list 返回的 save_pv=0/click_pv=-1 是真实数据还是参数/权限门。

纪律:只读、不打印 Cookie、每个实验独立、业务报错即停该分支。
用法: python scripts/probe_quark_share_pv.py --cookie-file data/quark_cookie.tmp.txt
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import requests  # noqa: E402

PC_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36")
ANDROID_UA = ("Mozilla/5.0 (Linux; Android 14; M2012K11AC Build/UP1A.231005.007) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/130.0.0.0 "
              "Mobile Safari/537.36 Quark/7.9.4.670")
BASE = "https://drive-pc.quark.cn/1/clouddrive"

BASE_BODY = {"page_size": 3, "page": 1, "fetch_max_file_update_pos": 0,
             "fetch_total": 1, "fetch_update_files": 0, "needTotalNum": 0,
             "share_read_statues": [0]}


def stats_of(items: list) -> str:
    """摘出统计字段便于横向对比。"""
    if not items:
        return "(空列表)"
    it = items[0]
    return " ".join(f"{k}={it.get(k)}" for k in
                    ("click_pv", "save_pv", "download_pv", "visit_user_count"))


def call(cookie: str, method: str, path: str, body: dict | None = None,
         fr: str = "pc", ua: str = PC_UA, extra_params: dict | None = None):
    """单次请求,返回 (状态摘要, 原始json或None)。"""
    headers = {"User-Agent": ua, "Accept": "application/json, text/plain, */*",
               "Cookie": cookie, "Origin": "https://pan.quark.cn",
               "Referer": "https://pan.quark.cn/"}
    params = {"pr": "ucpro", "fr": fr, **(extra_params or {})}
    try:
        if method == "POST":
            r = requests.post(BASE + path, params=params, headers=headers,
                              json=body or {}, timeout=20)
        else:
            r = requests.get(BASE + path, params=params, headers=headers, timeout=20)
    except requests.RequestException as e:
        return f"网络错误: {e}", None
    if r.status_code != 200:
        return f"HTTP {r.status_code}", None
    try:
        d = r.json()
    except ValueError:
        return f"非JSON: {r.text[:80]}", None
    lst = (d.get("data") or {}).get("list") if isinstance(d.get("data"), dict) else None
    meta = (d.get("data") or {}).get("metadata") if isinstance(d.get("data"), dict) else None
    summary = (f"code={d.get('code')} msg={str(d.get('message'))[:40]!r} "
               f"n={len(lst) if lst else 0} [{stats_of(lst or [])}]")
    if meta:
        summary += f" meta_total={meta.get('_total')}"
    return summary, d


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cookie-file", required=True)
    args = ap.parse_args()
    cookie = open(args.cookie_file, encoding="utf-8").read().strip()
    if not cookie:
        print("Cookie 文件为空")
        return 1

    tests = [
        ("A0 基线(pc+默认body)", "POST", "/share/update_list", BASE_BODY, "pc", PC_UA, None),
        ("A1 fr=android+安卓UA", "POST", "/share/update_list", BASE_BODY, "android", ANDROID_UA, None),
        ("A2 空 statues[]", "POST", "/share/update_list",
         {**BASE_BODY, "share_read_statues": []}, "pc", PC_UA, None),
        ("A3 未知参数容忍度", "POST", "/share/update_list",
         {**BASE_BODY, "fetch_click_pv": 1, "fetch_statistics": 1}, "pc", PC_UA, None),
        ("B1 POST /share/list", "POST", "/share/list",
         {"page": 1, "page_size": 3}, "pc", PC_UA, None),
        ("B2 POST /share/page", "POST", "/share/page",
         {"page": 1, "page_size": 3}, "pc", PC_UA, None),
        ("B3 GET /share/list", "GET", "/share/list", None, "pc", PC_UA,
         {"_page": 1, "_size": 3}),
    ]
    for name, method, path, body, fr, ua, extra in tests:
        summary, _ = call(cookie, method, path, body, fr, ua, extra)
        print(f"{name:<24} {path:<22} {summary}")
        time.sleep(1.2)  # 轻微限速,不加深风控
    return 0


if __name__ == "__main__":
    sys.exit(main())
