"""一次只读探测:夸克「我的分享」接口是否带每条链的转存/浏览数据(拉新回补可行性)。

纪律:只读 GET、不打印 Cookie、连续失败即停(不加深风控)。
用法: python scripts/probe_quark_share_stats.py --cookie-file data/quark_cookie.tmp.txt
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import requests  # noqa: E402

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36")
BASE = "https://drive-pc.quark.cn/1/clouddrive"
COMMON = {"pr": "ucpro", "fr": "pc"}


def probe(cookie: str, path: str, params: dict) -> None:
    r = requests.get(BASE + path, params={**COMMON, **params},
                     headers={"User-Agent": UA, "Accept": "application/json, text/plain, */*",
                              "Cookie": cookie, "Origin": "https://pan.quark.cn",
                              "Referer": "https://pan.quark.cn/"}, timeout=20)
    print(f"\n== GET {path} -> HTTP {r.status_code}")
    try:
        data = r.json()
    except ValueError:
        print("非 JSON:", r.text[:200])
        return
    print("顶层键:", sorted(data.keys()))
    print("code =", data.get("code"), "message =", str(data.get("message") or "")[:60])
    d = data.get("data") or {}
    if isinstance(d, dict):
        print("data 键:", sorted(d.keys()))
        items = d.get("list") or d.get("records") or d.get("shares") or []
        if items:
            fields: dict[str, int] = {}
            for it in items[:20]:
                for k in it.keys():
                    fields[k] = fields.get(k, 0) + 1
            print(f"条目数 {len(items)},字段覆盖率(前 20 条):")
            for k, n in sorted(fields.items(), key=lambda x: -x[1]):
                print(f"   {k}: {n}/{len(items)}")
            first = items[0]
            print("首条样例(截断):")
            print(json.dumps({k: (str(v)[:40] if isinstance(v, str) else v)
                              for k, v in first.items()}, ensure_ascii=False, indent=1)[:1200])
        else:
            print("data 无列表:", json.dumps(d, ensure_ascii=False)[:300])
    if data.get("code") not in (0, 200, None):
        print("!! 业务错误,停止后续探测")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cookie-file", required=True)
    args = ap.parse_args()
    cookie = open(args.cookie_file, encoding="utf-8").read().strip()
    if not cookie:
        print("Cookie 文件为空")
        return 1

    probe(cookie, "/share/mypage", {"_page": 1, "_size": 20})
    probe(cookie, "/share/mypage/statistics", {"_page": 1, "_size": 20})
    probe(cookie, "/share", {"_page": 1, "_size": 20})
    return 0


if __name__ == "__main__":
    sys.exit(main())
