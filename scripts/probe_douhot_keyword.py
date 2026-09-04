"""探测:hot_search 的 query_list 是否支持 `keyword` 定向过滤(榜外词也能查)。

背景:关键词监控修复——`record_watch_snaps` 目前只对 `list_type="word"` 走定向查询
(`hot_word_keyword`),选"搜索榜"等其它榜单输入关键词时,退化为在**全榜默认数据**里
找该词(榜外记 0),监控不到用户在热点宝官网输入关键词后看到的热度排行。

`hot_word/query_list` 请求体带 `"keyword": ""` 字段已是实证;hot_search 同为
query_list 型接口,极可能同样支持。本脚本按"最小直连"验证:
  A) baseline   —— 不带 keyword,拉默认搜索榜(对照组)
  B) +keyword   —— 取榜上第一名词作为 keyword 过滤,应只剩含该词的结果
  C) 榜外词     —— 用一个不在榜里的词过滤,看能否返回该词的定向数据

用法:
    python scripts/probe_douhot_keyword.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import requests

sys.stdout.reconfigure(encoding="utf-8")

COOKIE_FILE = Path("data/douhot_cookie.txt")
HOT_SEARCH_API = "/douhot/v1/dashboard/hot_search/query_list"
HOT_WORD_API = "/douhot/v1/dashboard/hot_word/query_list"
VIDEO_API = "/douhot/v1/material/video_billboard"
CHALLENGE_API = "/douhot/v1/material/challenge_billboard"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36 Edg/147.0.0.0"
)


def load_cookie() -> str:
    if not COOKIE_FILE.exists():
        sys.exit(f"缺少 Cookie 文件:{COOKIE_FILE}")
    return COOKIE_FILE.read_text(encoding="utf-8").strip()


def post(cookie: str, path: str, body: dict) -> dict:
    resp = requests.post(
        f"https://douhot.douyin.com{path}",
        headers={
            "user-agent": _UA,
            "content-type": "application/json",
            "referer": "https://douhot.douyin.com/square/trend?active_tab=hotword_all",
            "cookie": cookie,
        },
        data=json.dumps(body, ensure_ascii=False).encode(),
        timeout=20,
    )
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return {"_status": resp.status_code, "_text": resp.text[:120]}


def items_of(obj: dict, key: str) -> list[dict]:
    data = obj.get("data")
    lst = data.get(key) if isinstance(data, dict) else None
    return [it for it in (lst or []) if isinstance(it, dict)]


def show(label: str, obj: dict, key: str, word_field: str) -> list[dict]:
    items = items_of(obj, key)
    head = [str(it.get(word_field))[:14] for it in items[:6]]
    print(f"  {label:<26} code={obj.get('code')} n={len(items)} head={head}")
    return items


def main() -> None:
    cookie = load_cookie()
    base_body = {"page_num": 1, "page_size": 20, "sub_type": 3001, "date_window": 1}

    print("=== hot_search query_list ===")
    baseline = post(cookie, HOT_SEARCH_API, base_body)
    items = show("A 不带keyword(默认榜)", baseline, "search_list", "key_word")

    if items:
        kw = str(items[0].get("key_word") or items[0].get("title") or "")
        print(f"  (取榜首词做过滤:{kw!r})")
        hit = post(cookie, HOT_SEARCH_API, {**base_body, "keyword": kw})
        show(f"B +keyword={kw[:10]!r}", hit, "search_list", "key_word")

    print("\n=== hot_word query_list(对照,已知支持) ===")
    word_body = {"page_num": 1, "page_size": 24, "tab_type": 1, "keyword": "", "date_window": 24}
    words = show("C 内容词默认", post(cookie, HOT_WORD_API, word_body), "word_list", "title")
    if words:
        w = str(words[0].get("title"))
        show(f"D +keyword={w[:10]!r}", post(cookie, HOT_WORD_API, {**word_body, "keyword": w}), "word_list", "title")

    print("\n=== video / challenge billboard(未知,验证) ===")
    video_body = {"sub_type": 1001, "date_window": 24, "page": 1, "page_size": 10, "tag_version": "v2"}
    videos = show("E 视频榜默认", post(cookie, VIDEO_API, video_body), "objs", "item_title")
    if videos:
        v = str(videos[0].get("item_title"))
        show(f"F +keyword={v[:10]!r}", post(cookie, VIDEO_API, {**video_body, "keyword": v[:6]}), "objs", "item_title")
    chal_body = {"sub_type": 2001, "date_window": 24, "page": 1, "page_size": 10, "tag_version": "v2"}
    chals = show("G 话题榜默认", post(cookie, CHALLENGE_API, chal_body), "objs", "challenge_name")
    if chals:
        c = str(chals[0].get("challenge_name"))
        show(f"H +keyword={c[:10]!r}", post(cookie, CHALLENGE_API, {**chal_body, "keyword": c[:4]}), "objs", "challenge_name")

    print("\n=== 结论判据 ===")
    print("  过滤后只含该词/n 变小  → 对应榜单支持 keyword,可给该榜监控做定向查询")


if __name__ == "__main__":
    main()
