"""抖音热点宝·内容词趋势监控(见 doc/dev.md §5.9)。

douhot.douyin.com 的 `hot_word/query_list` 接口需要抖音 `a_bogus`/`msToken` 签名,
裸 requests 调不动(`url doesn't match`);但用 Playwright 驱动真实浏览器时签名由浏览器合法生成。
故本服务**用无头 Chromium 打开热点趋势页,拦截该接口响应**,拿到内容词与热度时间序列(明文 JSON,
非加密)。读取的是用户已授权(douhot Cookie)的数据。

用途:`run_douhot_trend()` 采集内容词趋势快照并入库,供"监控内容词趋势/判涨"使用。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from config.settings import Settings
from app.utils import get_logger, retry

logger = get_logger(__name__)

URL = "https://douhot.douyin.com/square/trend?active_tab=hotword_all"
QUERY_API = "/douhot/v1/dashboard/hot_word/query_list"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36 Edg/147.0.0.0"
)


class DouhotError(Exception):
    """内容词趋势获取/解析失败。"""



def fetch_list(
    cookie: str,
    url: str,
    fragment: str,
    title_keys: tuple[str, ...],
    score_keys: tuple[str, ...],
    click_label: str | None = None,
) -> list[dict]:
    """通用:驱动浏览器打开 url,拦截含 fragment 的响应,取 [{title, score}]。

    - 若 `click_label` 给定时,先点击该子Tab(如 视频榜/话题榜)再抓对应榜单接口。
    - 解析失败返回空(不抛错)。供 内容词/搜索/视频/话题/订阅 使用。
    """
    from playwright.sync_api import sync_playwright

    respons: list[object] = []

    def on_response(response) -> None:  # type: ignore[no-untyped-def]
        try:
            if fragment in response.url and "json" in (response.headers.get("content-type") or ""):
                respons.append(response)
        except Exception:  # noqa: BLE001
            pass

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent=_UA, viewport={"width": 1400, "height": 950})
            context.add_cookies(_parse_cookies(cookie))
            page = context.new_page()
            page.on("response", on_response)
            page.goto(url, timeout=60_000)
            page.wait_for_timeout(4500)
            if click_label:
                try:
                    page.get_by_text(click_label, exact=True).first.click(timeout=8_000)
                except Exception:  # noqa: BLE001
                    pass
            page.wait_for_timeout(5_000)
            words: list[dict] = []
            for resp in respons:
                try:
                    obj = resp.json()  # type: ignore[attr-defined]
                except Exception:  # noqa: BLE001
                    continue
                words = _extract_keywords(obj, title_keys, score_keys)
                if words:
                    break
            browser.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("热点榜单 %s 加载失败:%s", fragment, exc)
        return []
    return words


def _extract_keywords(obj: object, title_keys: tuple[str, ...], score_keys: tuple[str, ...]) -> list[dict]:
    """递归找含 title/key_word + score 的条目列表,返回 [{title, score}]。"""
    found: list[dict] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            title = next((node[k] for k in title_keys if k in node and node[k]), None)
            # 并非所有 dict 都是条目,只有同时含 title 与 score 才算
            if title and any(k in node for k in score_keys):
                score = next((node[k] for k in score_keys if k in node), 0)
                found.append({"title": str(title).strip(), "score": score})
                return
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(obj)
    return found


def fetch_search_words(cookie: str) -> list[dict]:
    """搜索榜(hot_search/query_list):key_word + search_score。"""
    return fetch_list(cookie, URL, "/hot_search/query_list", ("key_word", "title"), ("search_score", "score"))


def fetch_video_words(cookie: str) -> list[dict]:
    """视频榜(聚合榜-子Tab):material/video_billboard,item_title + play_cnt。"""
    return fetch_list(
        cookie,
        "https://douhot.douyin.com/square/hotspot?active_tab=hotspot_all",
        "/material/video_billboard",
        ("item_title", "title"),
        ("play_cnt", "score"),
        click_label="视频榜",
    )


def fetch_topic_words(cookie: str) -> list[dict]:
    """话题榜(聚合榜-子Tab):material/challenge_billboard,challenge_name + score。"""
    return fetch_list(
        cookie,
        "https://douhot.douyin.com/square/hotspot?active_tab=hotspot_all",
        "/material/challenge_billboard",
        ("challenge_name", "title"),
        ("score", "play_cnt"),
        click_label="话题榜",
    )


def fetch_subscribe_words(cookie: str) -> list[dict]:
    """我的订阅/榜单聚合(subscribe/query_list)。"""
    return fetch_list(cookie, "https://douhot.douyin.com/square/hotspot?active_tab=hotspot_all", "/subscribe/query_list", ("title", "key_word"), ("score", "search_score"))


def _parse_cookies(raw: str) -> list[dict]:
    out: list[dict] = []
    for pair in raw.split("; "):
        if "=" not in pair:
            continue
        name, _, val = pair.partition("=")
        out.append({"name": name.strip(), "value": val.strip(), "domain": ".douyin.com", "path": "/"})
    return out


def _parse_word(w: dict) -> dict:
    """把接口返回的内容词卡片整理为一条趋势记录。"""
    trends = w.get("trends") or []
    latest = trends[-1]["value"] if trends else 0
    first = trends[0]["value"] if trends else 0
    return {
        "title": str(w.get("title", "")).strip(),
        "score": w.get("score") or 0,          # 飙升指数
        "rising_ratio": w.get("rising_ratio") or 0,  # 平台飙升倍率
        "rising_speed": w.get("rising_speed") or "",
        "trend_len": len(trends),
        "latest_value": latest,
        "trend_delta": latest - first,          # 自身热度序列近端-远端
        "query_day": w.get("query_day") or "",
    }


@retry(attempts=2, base_delay=1.0, exceptions=(DouhotError, Exception))
def fetch_content_words(cookie: str) -> list[dict]:
    """用无头浏览器打开热点趋势页,拦截内容词接口,返回趋势记录列表。

    直接接收 Cookie 字符串(便于多用户各自注入)。签名由浏览器合法生成。
    """
    from playwright.sync_api import sync_playwright

    responses: list[object] = []

    def on_response(response) -> None:  # type: ignore[no-untyped-def]
        try:
            if QUERY_API in response.url and "json" in (response.headers.get("content-type") or ""):
                responses.append(response)
        except Exception:  # noqa: BLE001
            pass

    words: list[dict] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent=_UA, viewport={"width": 1400, "height": 900})
            context.add_cookies(_parse_cookies(cookie))
            page = context.new_page()
            page.on("response", on_response)
            page.goto(URL, timeout=60_000)
            page.wait_for_timeout(9_000)
            # 必须在浏览器/事件循环存活时解析 response(退出后 json() 会抛 Event loop closed)。
            for resp in responses:
                try:
                    obj = resp.json()  # type: ignore[attr-defined]
                except Exception:  # noqa: BLE001
                    continue
                wl = obj.get("data", {}).get("word_list", []) if isinstance(obj, dict) else []
                if wl:
                    words = [_parse_word(w) for w in wl]
                    break
            browser.close()
    except Exception as exc:  # noqa: BLE001
        raise DouhotError(f"热点趋势页加载失败:{exc}") from exc

    if not words:
        raise DouhotError("未从热点响应中解析到内容词")
    logger.info("抖音热点·内容词采集完成,共 %s 个", len(words))
    return words
