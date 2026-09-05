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
    "fetch_keyword_heat",
    "fetch_list_keyword_heat",
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


def fetch_keyword_heat(cookie: str, keyword: str, settings: Settings | None = None) -> dict:
    """按关键词定向查内容词热度:返回 {score, rank, trend_len, latest_value, …}。

    与榜单采集不同:直接拿 keyword 去查接口,即使该词不在 top100 里也能取到
    它的飙升指数与趋势。查不到精确匹配时返回冷启动空值(score=0)。
    """
    raw = DouhotClient(cookie, settings).hot_word_keyword(keyword.strip())
    if not raw:
        return {"keyword": keyword.strip(), "score": 0, "trend_len": 0, "latest_value": 0, "rank_now": 0}
    # 优先精确匹配,否则取相关结果第一名
    hit = next((w for w in raw if str(w.get("title", "")).strip() == keyword.strip()), raw[0])
    return {
        "keyword": keyword.strip(),
        "score": hit.get("score") or 0,
        "trend_len": len(hit.get("trends") or []),
        "latest_value": (hit.get("trends") or [{}])[-1].get("value", 0) if hit.get("trends") else 0,
        "rank_now": next(
            (i + 1 for i, w in enumerate(raw) if str(w.get("title", "")).strip() == keyword.strip()), 0
        ),
        "title": str(hit.get("title", ""))
    }


# 各榜单定向查询的规格:client 方法 + 条目里"词"与"分"的字段优先级。
# 实测(scripts/probe_douhot_keyword.py)搜索/视频/话题榜的 query_list/body 同样
# 支持 keyword 过滤——这正是"监控词走定向查询而不是全榜默认数据"的关键。
_KEYWORD_SPEC: dict[str, tuple[str, tuple[str, ...], tuple[str, ...]]] = {
    "search": ("hot_search", ("key_word", "title"), ("search_score", "score")),
    "video": ("video_billboard", ("item_title", "title"), ("play_cnt", "score")),
    "topic": ("challenge_billboard", ("challenge_name", "title"), ("score", "play_cnt")),
}


def fetch_list_keyword_heat(cookie: str, list_type: str, keyword: str, settings: Settings | None = None) -> dict:
    """按关键词定向查**非内容词榜单**(搜索/视频/话题)的条目热度。

    与 `fetch_keyword_heat`(内容词)同一套语义:拿 keyword 过滤接口,榜外词也能取到
    专属数据;精确匹配优先,否则取相关结果第一,查不到返回冷启动零值。
    订阅榜(subscribe)无 keyword 参数,不支持定向查询,调用方走榜单内查找。
    """
    spec = _KEYWORD_SPEC.get(list_type)
    if spec is None:
        return {"keyword": keyword.strip(), "score": 0, "rank_now": 0}
    method_name, title_keys, score_keys = spec
    try:
        raw = getattr(DouhotClient(cookie, settings), method_name)(limit=50, keyword=keyword.strip())
    except DouhotError:
        logger.warning("热点宝%s定向查询失败(keyword=%s)", list_type, keyword)
        return {"keyword": keyword.strip(), "score": 0, "rank_now": 0}
    if not raw:
        return {"keyword": keyword.strip(), "score": 0, "rank_now": 0}
    hit = next((it for it in raw if str(_pick(it, title_keys) or "").strip() == keyword.strip()), raw[0])
    title = str(_pick(hit, title_keys) or "").strip()
    rank = next(
        (i + 1 for i, it in enumerate(raw) if str(_pick(it, title_keys) or "").strip() == keyword.strip()), 0
    )
    return {"keyword": keyword.strip(), "score": _pick(hit, score_keys) or 0, "rank_now": rank, "title": title}


def _trend_of(it: dict) -> tuple[float | None, str]:
    """从话题条目的 `trends` 每日热度序列算(窗口)增长与趋势标签。

    `trends` = [{date, value}, ...](近约 14 天每日热度)。增长 = (最新 - 最早) / 最早,
    这是**一次采集即可得出**的真实趋势——否则相邻采集只拿最新日值,变化极慢会恒 0%。
    无 trends/样本<2 返回 (None, None)。
    """
    t = it.get("trends") or []
    if not isinstance(t, list):
        return None, ""
    vals = [p.get("value") for p in t if isinstance(p, dict) and p.get("value") is not None]
    if len(vals) < 2 or not vals[0]:
        return None, ""
    growth = (vals[-1] - vals[0]) / vals[0]
    label = "上升期" if growth > 0.05 else ("回落期" if growth < -0.05 else "平稳")
    return growth, label


def fetch_keyword_items(cookie: str, list_type: str, keyword: str, settings: Settings | None = None,
                        limit: int = 50) -> list[dict]:
    """按关键词查某子榜的**条目列表**(榜外词也能查到),供榜 tab 按词搜索。

    与 `fetch_list_keyword_heat`(单条最优)不同,这里返回过滤后的**整表**:
    内容词走 `hot_word_keyword`,搜索/视频/话题走带 keyword 的 query_list(`limit` 条,
    话题榜可到 100+,搜索/视频受服务端上限约 50),订阅(subscribe)无 keyword 参数,不支持(返回空)。
    每条除 `title/score` 外,附带 `trend_growth`/`trend_label`(由 `trends` 每日序列算出,真实趋势)。
    """
    kw = keyword.strip()
    if not kw:
        return []
    try:
        client = DouhotClient(cookie, settings)
        if list_type == "word":
            raw = client.hot_word_keyword(kw)
            items = [_entry(_pick(it, ("title", "key_word", "challenge_name", "word")),
                            _pick(it, ("score", "search_score", "play_cnt"))) for it in raw]
        elif list_type in _KEYWORD_SPEC:
            method_name, title_keys, score_keys = _KEYWORD_SPEC[list_type]
            raw = getattr(client, method_name)(limit=limit, keyword=kw)
            items = []
            for it in raw:
                title = str(_pick(it, title_keys) or "").strip()
                if not title:
                    continue
                growth, label = _trend_of(it)
                items.append({"title": title, "score": _pick(it, score_keys) or 0,
                              "trend_growth": growth, "trend_label": label})
        else:
            return []  # subscribe 不支持 keyword
    except DouhotError as exc:
        logger.warning("热点宝%s按词搜索失败(keyword=%s):%s", list_type, kw, exc)
        return []
    return [it for it in items if it["title"]]


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
