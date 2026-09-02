"""抖音热点宝直连采集单测(不联网)。

覆盖两层:
- `douhot_client`:响应封装拆解(code=0/8/其他)、翻页累积、null 列表兜底;
- `douhot`:各榜单字段解析、空标题过滤、可选榜单失败降级为空列表。
"""
import pytest

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
