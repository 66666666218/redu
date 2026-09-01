"""抖音热点宝·榜单采集与解析(见 doc/dev.md §5.9)。

数据由 `app/services/douhot_client.py` **直连**拉取(纯 requests + 登录 Cookie,
不需要 a_bogus/X-Bogus 签名,也不需要无头浏览器);本模块只做**字段解析**——
把各榜单的原始条目统一成 `{"title", "score", ...}`,供入库与"判涨/关注词快照"使用。

历史:早期用 Playwright 打开热点页拦截接口响应,单次采集 ~15s 且吃内存,子Tab 点击
还常因页面改版失效。实测接口不校验签名后改为直连,采集降到亚秒级,并能翻页取满
`DOUHOT_TOP_N` 条(浏览器方案受首屏渲染限制只能拿到 24 条)。

对外接口(供 `app/services/tenant.py` 调用):
- `fetch_content_words` 内容词榜(飙升词,含热度时间序列)
- `fetch_search_words` / `fetch_video_words` / `fetch_topic_words` / `fetch_subscribe_words`
"""
from __future__ import annotations

from config.settings import Settings
from app.services.douhot_client import DouhotAuthError, DouhotClient, DouhotError
from app.utils import get_logger

logger = get_logger(__name__)

__all__ = [
    "DouhotError",
    "DouhotAuthError",
    "fetch_content_words",
    "fetch_search_words",
    "fetch_video_words",
    "fetch_topic_words",
    "fetch_subscribe_words",
]

DEFAULT_TOP_N = 24  # 未传 settings 时的默认条数(与原浏览器方案首屏一致)


def _top_n(settings: Settings | None) -> int:
    """取本次采集条数:优先 `DOUHOT_TOP_N`,并做合理范围保护。"""
    n = getattr(settings, "douhot_top_n", DEFAULT_TOP_N) or DEFAULT_TOP_N
    return max(1, min(int(n), 200))


def _entry(title: object, score: object) -> dict:
    """统一条目格式;标题为空的条目由调用方过滤。"""
    return {"title": str(title or "").strip(), "score": score or 0}


def _pick(item: dict, keys: tuple[str, ...]) -> object:
    """按优先级取第一个非空字段(各榜单字段名不一致)。"""
    return next((item[k] for k in keys if item.get(k)), None)


def _parse_word(w: dict) -> dict:
    """把内容词卡片整理为一条趋势记录(字段对应 `DouhotWord` 模型列)。"""
    trends = w.get("trends") or []
    latest = trends[-1]["value"] if trends else 0
    first = trends[0]["value"] if trends else 0
    return {
        "title": str(w.get("title", "")).strip(),
        "score": w.get("score") or 0,                # 飙升指数
        "rising_ratio": w.get("rising_ratio") or 0,  # 平台飙升倍率
        "rising_speed": w.get("rising_speed") or "",
        "trend_len": len(trends),
        "latest_value": latest,
        "trend_delta": latest - first,               # 自身热度序列近端-远端
        "query_day": w.get("query_day") or "",
    }


def fetch_content_words(cookie: str, settings: Settings | None = None) -> list[dict]:
    """内容词榜:返回趋势记录列表(无数据时抛 `DouhotError`,以便记为采集失败)。"""
    raw = DouhotClient(cookie, settings).hot_words(limit=_top_n(settings))
    words = [_parse_word(w) for w in raw if str(w.get("title", "")).strip()]
    if not words:
        raise DouhotError("热点宝未返回内容词(Cookie 可能失效或接口改版)")
    logger.info("抖音热点·内容词采集完成,共 %s 个", len(words))
    return words


def _fetch_ranked(
    cookie: str,
    settings: Settings | None,
    method_name: str,
    title_keys: tuple[str, ...],
    score_keys: tuple[str, ...],
    label: str,
) -> list[dict]:
    """通用榜单拉取+解析:失败不抛错、返回空列表(这些榜单是可选的补充数据)。"""
    try:
        raw = getattr(DouhotClient(cookie, settings), method_name)(limit=_top_n(settings))
    except DouhotError as exc:
        logger.warning("热点宝%s拉取失败:%s", label, exc)
        return []
    items = [_entry(_pick(it, title_keys), _pick(it, score_keys)) for it in raw]
    return [it for it in items if it["title"]]


def fetch_search_words(cookie: str, settings: Settings | None = None) -> list[dict]:
    """搜索榜:key_word + search_score。"""
    return _fetch_ranked(cookie, settings, "hot_search", ("key_word", "title"), ("search_score", "score"), "搜索榜")


def fetch_video_words(cookie: str, settings: Settings | None = None) -> list[dict]:
    """视频榜:item_title + play_cnt(无标题的视频条目会被过滤掉)。"""
    return _fetch_ranked(cookie, settings, "video_billboard", ("item_title",), ("play_cnt", "score"), "视频榜")


def fetch_topic_words(cookie: str, settings: Settings | None = None) -> list[dict]:
    """话题榜:challenge_name + score。"""
    return _fetch_ranked(
        cookie, settings, "challenge_billboard", ("challenge_name", "title"), ("score", "play_cnt"), "话题榜"
    )


def fetch_subscribe_words(cookie: str, settings: Settings | None = None) -> list[dict]:
    """我的订阅:字段随订阅类型而异,按优先级兜底取标题/分值。"""
    try:
        raw = DouhotClient(cookie, settings).subscribe()
    except DouhotError as exc:
        logger.warning("热点宝订阅拉取失败:%s", exc)
        return []
    items = [
        _entry(_pick(it, ("title", "key_word", "challenge_name", "word")), _pick(it, ("score", "search_score", "play_cnt")))
        for it in raw
    ]
    return [it for it in items if it["title"]]
