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


def _load_cookie(path: str) -> str:
    return Path(path).read_text(encoding="utf-8").strip()


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
def fetch_content_words(cookie_file: str) -> list[dict]:
    """用无头浏览器打开热点趋势页,拦截内容词接口,返回趋势记录列表。"""
    from playwright.sync_api import sync_playwright

    cookie = _load_cookie(cookie_file)
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


def _detect_rising(settings: Settings, repo: object, words: list[dict], notifier: object) -> tuple[list[dict], list[str]]:
    """跨轮判涨:按词取历史飙升指数序列,双条件(环比涨幅>阈值 且 斜率>0)命中则告警(带冷却去重)。"""
    from app.models import Alert
    from app.services.trend_analyzer import compute_growth, compute_slope

    rising: list[dict] = []
    for w in words:
        series = repo.douhot_score_series(w["title"], limit=10)  # type: ignore[attr-defined]
        values = [s["score"] for s in series]
        if len(values) < 2:
            continue
        growth = compute_growth(values)
        slope = compute_slope(values)
        if growth is None or slope is None:
            continue
        if growth > settings.growth_threshold and slope > 0:
            item = dict(w)
            item["growth"] = growth
            item["slope"] = slope
            rising.append(item)
    rising.sort(key=lambda r: r["growth"], reverse=True)

    alerted: list[str] = []
    for r in rising[: settings.douhot_alert_max]:
        if repo.douhot_alerted_recent(r["title"], settings.douhot_alert_cooldown_hours):  # type: ignore[attr-defined]
            continue
        alert = Alert(
            keyword=r["title"],
            reason=f"抖音内容词飙升指数环比 {r['growth']:.0%}/斜率 {r['slope']:.0f}",
        )
        notifier.notify(alert)  # type: ignore[attr-defined]
        repo.record_douhot_alert(r["title"])  # type: ignore[attr-defined]
        alerted.append(r["title"])
    return rising, alerted


def run_douhot_trend(
    settings: Settings | None = None,
    repo: object | None = None,
    notifier: object | None = None,
) -> dict:
    """采集一次内容词趋势快照:抓取 → 入库 → 跨轮判涨 → 告警 → 返回。"""
    from config.settings import get_settings
    from app.services.notifier import get_notifier
    from app.storage import ArchiveRepository

    settings = settings or get_settings()
    repo = repo or ArchiveRepository(settings.data_dir)
    notifier = notifier or get_notifier(settings)
    words = fetch_content_words(settings.douhot_cookie_file)
    run_id = datetime.now().strftime("%Y%m%d%H%M%S")
    repo.save_douhot_words(run_id, words)  # type: ignore[attr-defined]
    top = sorted(words, key=lambda w: w["score"], reverse=True)[: settings.douhot_top_n]
    rising, alerted = _detect_rising(settings, repo, top, notifier)
    logger.info(
        "抖音热词趋势完成 run=%s 条数=%s 判涨=%s 告警=%s", run_id, len(words), len(rising), len(alerted)
    )
    return {"run_id": run_id, "count": len(words), "items": top, "rising": rising, "rising_count": len(rising)}


if __name__ == "__main__":
    import json

    from app.utils import setup_logging

    setup_logging()
    outcome = run_douhot_trend()
    print("抖音内容词趋势采集:", outcome["count"], "条")
    for it in outcome["items"][:15]:
        print(f"  飙升{it['score']/1e4:>9.1f}万  ratio={it['rising_ratio']}  {it['title'][:24]}")
