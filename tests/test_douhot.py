"""抖音热点宝直连采集单测(不联网)。

覆盖两层:
- `douhot_client`:响应封装拆解(code=0/8/其他)、翻页累积、null 列表兜底;
- `douhot`:各榜单字段解析、空标题过滤、可选榜单失败降级为空列表。
"""
import pytest
import requests

from config.settings import Settings
from app.services import douhot
from app.services import douhot_client
from app.services.douhot_client import DouhotAuthError, DouhotClient, DouhotError


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _FakeSession:
    """按调用次数依次返回预置响应,并记录请求体,用于验证翻页参数。"""

    def __init__(self, payloads: list[dict]) -> None:
        self._payloads = payloads
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, str, bytes | None]] = []

    def mount(self, prefix: str, adapter: object) -> None:
        return None

    def request(self, method: str, url: str, **kwargs: object) -> _FakeResponse:
        self.calls.append((method, url, kwargs.get("data")))  # type: ignore[arg-type]
        idx = len(self.calls) - 1
        # 预置响应用完即视为"翻到底"(空 data),避免翻页逻辑在测试里无限取到同一页
        return _FakeResponse(self._payloads[idx] if idx < len(self._payloads) else _ok({}))


def _client(payloads: list[dict]) -> DouhotClient:
    client = DouhotClient("sessionid=x")
    client.session = _FakeSession(payloads)  # type: ignore[assignment]
    return client


def _ok(data: object) -> dict:
    return {"code": 0, "data": data}


# ---- 客户端:响应封装 ----
def test_auth_error_on_code_8() -> None:
    client = _client([{"code": 8, "data": "用户未登录"}])
    with pytest.raises(DouhotAuthError):
        client.hot_words()


def test_error_on_other_code() -> None:
    client = _client([{"code": 500, "data": None}])
    with pytest.raises(DouhotError):
        client.hot_words()


def test_empty_cookie_rejected() -> None:
    with pytest.raises(DouhotAuthError):
        DouhotClient("   ")


def test_null_list_returns_empty() -> None:
    """无订阅时服务端返回 subscribe_list=null,不应炸。"""
    assert _client([_ok({"subscribe_list": None})]).subscribe() == []


def test_paging_accumulates_until_limit() -> None:
    """单页硬顶 24,limit=30 时应翻第 2 页并截断到 30 条。"""
    page = _ok({"word_list": [{"title": f"w{i}", "score": i} for i in range(24)]})
    client = _client([page, page])
    words = client.hot_words(limit=30)
    assert len(words) == 30
    assert len(client.session.calls) == 2  # type: ignore[attr-defined]
    assert b'"page_num":2' in client.session.calls[1][2]  # type: ignore[index]


def test_paging_stops_on_empty_page() -> None:
    client = _client([_ok({"word_list": [{"title": "a", "score": 1}]}), _ok({"word_list": []})])
    assert len(client.hot_words(limit=100)) == 1


# ---- 业务层:字段解析 ----
def test_parse_word_trend_delta() -> None:
    parsed = douhot._parse_word(
        {"title": " 词 ", "score": 10, "trends": [{"value": 3}, {"value": 8}], "query_day": "2026-09-01"}
    )
    assert parsed["title"] == "词"
    assert parsed["trend_len"] == 2
    assert parsed["latest_value"] == 8
    assert parsed["trend_delta"] == 5


def test_parse_word_without_trends() -> None:
    parsed = douhot._parse_word({"title": "词"})
    assert parsed["trend_len"] == 0 and parsed["trend_delta"] == 0 and parsed["score"] == 0


def _patch_client(monkeypatch: pytest.MonkeyPatch, payloads: list[dict]) -> None:
    monkeypatch.setattr(douhot, "DouhotClient", lambda cookie, settings=None: _client(payloads))


def test_fetch_content_words_filters_empty_title(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_client(monkeypatch, [_ok({"word_list": [{"title": "有效", "score": 5}, {"title": "  "}]})])
    words = douhot.fetch_content_words("ck")
    assert [w["title"] for w in words] == ["有效"]


def test_fetch_content_words_raises_when_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_client(monkeypatch, [_ok({"word_list": []})])
    with pytest.raises(DouhotError):
        douhot.fetch_content_words("ck")


def test_fetch_search_words(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_client(monkeypatch, [_ok({"search_list": [{"key_word": "开学第一课", "search_score": 66}]})])
    assert douhot.fetch_search_words("ck") == [{"title": "开学第一课", "score": 66}]


def test_fetch_video_words_drops_untitled(monkeypatch: pytest.MonkeyPatch) -> None:
    """视频榜条目常有空 item_title,应过滤而不是留下空标题。"""
    _patch_client(
        monkeypatch,
        [_ok({"objs": [{"item_title": "", "play_cnt": 9}, {"item_title": "小狗", "play_cnt": 16}]})],
    )
    assert douhot.fetch_video_words("ck") == [{"title": "小狗", "score": 16}]


def test_fetch_topic_words(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_client(monkeypatch, [_ok({"objs": [{"challenge_name": "续火花", "score": 157}]})])
    assert douhot.fetch_topic_words("ck") == [{"title": "续火花", "score": 157}]


def test_optional_list_swallows_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """可选榜单失败不应中断整轮采集(内容词才是主数据)。"""
    _patch_client(monkeypatch, [{"code": 8, "data": "用户未登录"}])
    assert douhot.fetch_search_words("ck") == []
    assert douhot.fetch_subscribe_words("ck") == []


# ---- 条数配置 ----
def test_top_n_from_settings() -> None:
    assert douhot._top_n(Settings(_env_file=None, douhot_top_n=50)) == 50
    assert douhot._top_n(None) == douhot.DEFAULT_TOP_N
    assert douhot._top_n(Settings(_env_file=None, douhot_top_n=0)) == douhot.DEFAULT_TOP_N
    assert douhot._top_n(Settings(_env_file=None, douhot_top_n=9999)) == 200


def test_fetch_keyword_heat_prefers_exact_match(monkeypatch: pytest.MonkeyPatch) -> None:
    """按关键词定向查:优先精确匹配,否则取相关结果第一名;无结果返回冷启动。"""
    class _FakeHotWordKeyword:
        def __init__(self, result): self.result = result
        def __call__(self, keyword, **kw): return self.result

    class _FakeClient:
        def __init__(self, result): self.hot_word_keyword = _FakeHotWordKeyword(result)

    monkeypatch.setattr(
        douhot, "DouhotClient",
        lambda cookie, settings=None: _FakeClient([
            {"title": "世界杯", "score": 500, "trends": [{"value": 3}, {"value": 8}]},
            {"title": "世界杯竞猜", "score": 100, "trends": []},
        ]),
    )
    hit = douhot.fetch_keyword_heat("ck", "世界杯", Settings(_env_file=None))
    assert hit["score"] == 500 and hit["title"] == "世界杯" and hit["trend_len"] == 2 and hit["rank_now"] == 1

    # 无结果 → 冷启动零值
    monkeypatch.setattr(douhot, "DouhotClient", lambda cookie, settings=None: _FakeClient([]))
    miss = douhot.fetch_keyword_heat("ck", "不存在", Settings(_env_file=None))
    assert miss["score"] == 0 and miss["rank_now"] == 0 and miss["trend_len"] == 0


def _patch_list_keyword_client(monkeypatch: pytest.MonkeyPatch, methods: dict[str, list[dict]]) -> None:
    """打桩非内容词榜单的定向查询客户端(hot_search/video_billboard/challenge_billboard)。"""

    class _FakeMethod:
        def __init__(self, result): self.result = result
        def __call__(self, limit=20, keyword=""): return self.result

    class _FakeClient:
        def __init__(self, methods): self._methods = methods
        def __getattr__(self, name): return _FakeMethod(self._methods.get(name, []))

    monkeypatch.setattr(douhot, "DouhotClient", lambda cookie, settings=None: _FakeClient(methods))


def test_fetch_list_keyword_heat_search_exact_match(monkeypatch: pytest.MonkeyPatch) -> None:
    """搜索榜按 keyword 定向查询:命中精确词 → 取 score 与真实 rank。"""
    _patch_list_keyword_client(
        monkeypatch,
        {"hot_search": [{"key_word": "电视剧", "search_score": 10}, {"key_word": "早春晴朗", "search_score": 200}]},
    )
    hit = douhot.fetch_list_keyword_heat("ck", "search", "早春晴朗", Settings(_env_file=None))
    assert hit["score"] == 200 and hit["rank_now"] == 2 and hit["title"] == "早春晴朗"


def test_fetch_list_keyword_heat_topic_exact_match(monkeypatch: pytest.MonkeyPatch) -> None:
    """话题榜(challenge)字段为 challenge_name，精确命中取 score。"""
    _patch_list_keyword_client(
        monkeypatch,
        {"challenge_billboard": [{"challenge_name": "续火花", "score": 157}]},
    )
    hit = douhot.fetch_list_keyword_heat("ck", "topic", "续火花", Settings(_env_file=None))
    assert hit["score"] == 157 and hit["rank_now"] == 1 and hit["title"] == "续火花"


def test_fetch_list_keyword_heat_video_fuzzy_top(monkeypatch: pytest.MonkeyPatch) -> None:
    """视频榜为标题模糊/分词检索:无精确词命中时取顶级匹配视频的 play_cnt，rank 恒 0。"""
    _patch_list_keyword_client(
        monkeypatch,
        {"video_billboard": [
            {"item_title": "全网有和我一样的孩子吗", "play_cnt": 999},
            {"item_title": "孩子真棒", "play_cnt": 100},
        ]},
    )
    hit = douhot.fetch_list_keyword_heat("ck", "video", "孩子", Settings(_env_file=None))
    assert hit["score"] == 999 and hit["rank_now"] == 0 and hit["title"] == "全网有和我一样的孩子吗"


def test_fetch_list_keyword_heat_unsupported_type_cold(monkeypatch: pytest.MonkeyPatch) -> None:
    """订阅榜无 keyword 参数(不在 _KEYWORD_SPEC)→ 冷启动零值,不发起查询。"""
    _patch_list_keyword_client(monkeypatch, {})
    hit = douhot.fetch_list_keyword_heat("ck", "subscribe", "任意词", Settings(_env_file=None))
    assert hit["score"] == 0 and hit["rank_now"] == 0 and hit["keyword"] == "任意词"


def test_fetch_list_keyword_heat_empty_result_cold(monkeypatch: pytest.MonkeyPatch) -> None:
    """定向查询返回空 → 冷启动零值(不炸)。"""
    _patch_list_keyword_client(monkeypatch, {"hot_search": []})
    hit = douhot.fetch_list_keyword_heat("ck", "search", "不存在", Settings(_env_file=None))
    assert hit["score"] == 0 and hit["rank_now"] == 0


# ---- 榜 tab 按词搜索(返回过滤后的条目列表) ----
def test_fetch_keyword_items_topic(monkeypatch: pytest.MonkeyPatch) -> None:
    """话题榜按词搜索:返回 (title, score) 条目列表(含同词相关条目)。"""
    class _Fake:
        def challenge_billboard(self, limit=20, keyword=""):
            return [{"challenge_name": "续火花", "score": 157}, {"challenge_name": "续火花专用", "score": 46}]

    monkeypatch.setattr(douhot, "DouhotClient", lambda cookie, settings=None: _Fake())
    items = douhot.fetch_keyword_items("ck", "topic", "续火花", Settings(_env_file=None))
    assert items == [{"title": "续火花", "score": 157}, {"title": "续火花专用", "score": 46}]


def test_fetch_keyword_items_word(monkeypatch: pytest.MonkeyPatch) -> None:
    """内容词按词搜索:走 hot_word_keyword。"""
    class _Fake:
        def hot_word_keyword(self, keyword):
            return [{"title": "卢克", "score": 500}, {"title": "小卢克", "score": 8}]

    monkeypatch.setattr(douhot, "DouhotClient", lambda cookie, settings=None: _Fake())
    items = douhot.fetch_keyword_items("ck", "word", "卢克", Settings(_env_file=None))
    assert items[0] == {"title": "卢克", "score": 500}
    assert items[1] == {"title": "小卢克", "score": 8}


def test_fetch_keyword_items_empty_or_unsupported() -> None:
    """空关键词返回空;subscribe 不支持 keyword 搜索,返回空。"""
    assert douhot.fetch_keyword_items("ck", "topic", "   ") == []
    assert douhot.fetch_keyword_items("ck", "subscribe", "任意词") == []


def test_fetch_keyword_items_drops_empty_title(monkeypatch: pytest.MonkeyPatch) -> None:
    """过滤掉空标题条目(各榜偶有空白条目)。"""
    class _Fake:
        def hot_search(self, limit=20, keyword=""):
            return [{"key_word": "", "search_score": 9}, {"key_word": "有效词", "search_score": 5}]

    monkeypatch.setattr(douhot, "DouhotClient", lambda cookie, settings=None: _Fake())
    items = douhot.fetch_keyword_items("ck", "search", "有效词", Settings(_env_file=None))
    assert items == [{"title": "有效词", "score": 5}]


def test_douhot_honors_proxy_switch_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """DOUHOT_USE_PROXY 默认关闭时,即使配了代理池也不走(douhot 直连)。

    避免代理抖动把本可成功的采集拖垮;需要时再显式开启。
    """
    monkeypatch.setattr(douhot_client, "get_proxies", lambda settings: {"http": "http://p:1", "https": "http://p:1"})
    s = Settings(_env_file=None)
    client = DouhotClient("x", s)
    assert client.proxies is None  # douhot_use_proxy=false → 直连


def test_douhot_honors_proxy_switch_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """DOUHOT_USE_PROXY=true 时走代理池(绕过服务器 IP 被抖音风控的 502)。"""
    monkeypatch.setattr(douhot_client, "get_proxies", lambda settings: {"http": "http://p:1", "https": "http://p:1"})
    s = Settings(_env_file=None, douhot_use_proxy=True)
    client = DouhotClient("x", s)
    assert client.proxies == {"http": "http://p:1", "https": "http://p:1"}


def test_proxy_bad_node_retries_with_new_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """代理池里偶有死节点:首次连接失败应换新代理重试,而不是整次采集放弃。"""
    calls = {"get_proxies": 0, "request": 0}

    def fake_get_proxies(settings):
        calls["get_proxies"] += 1
        return {"http": f"http://p{calls['get_proxies']}:1", "https": f"http://p{calls['get_proxies']}:1"}

    class _BadThenGood:
        def request(self, method, url, **kw):
            calls["request"] += 1
            if calls["request"] == 1:
                raise requests.RequestException("死节点")
            # 一次给满 24 条,_paged 就到第 1 页为止
            return _FakeResponse({"code": 0, "data": {"word_list": [{"title": f"w{i}", "score": i} for i in range(24)]}})

    monkeypatch.setattr(douhot_client, "get_proxies", fake_get_proxies)
    c = DouhotClient("x", Settings(_env_file=None, douhot_use_proxy=True))  # 先打桩再构造,__init__ 即走代理
    c.session = _BadThenGood()  # type: ignore[assignment]
    words = c.hot_words(limit=24)
    assert calls["get_proxies"] == 2 and calls["request"] == 2
    assert len(words) == 24 and words[0]["title"] == "w0"
