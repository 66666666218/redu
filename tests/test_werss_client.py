"""WeRSS 客户端单测(全 mock,零外呼):认证头/路径、翻页与截断、错误分类、订阅归组。

合同取自 rachelos/we-mp-rss 的 main 源码(见 werss_client.py 顶部注释),这里把它**钉住**:
上游是别人维护的开源服务,接口改动的概率远大于微信读书,没有这几条测试就只能上线才发现。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest

from app.services import werss_client
from app.services.reader_platform_client import PlatformError
from app.services.werss_client import WerssAuthError, WerssClient, WerssError

BASE = "https://werss.test"


class _Resp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _ok(data, code=0):
    return _Resp(200, {"code": code, "message": "success", "data": data})


def _client(monkeypatch, responses):
    """注入假传输:responses 按调用顺序返回 _Resp;返回 (client, calls)。"""
    calls: list[dict] = []
    queue = list(responses)

    def fake_request(method, url, params=None, timeout=None, headers=None, **kw):
        calls.append({"method": method, "url": url, "params": params, "headers": headers})
        if not queue:
            raise AssertionError("WeRSS 客户端调用次数超出预期")
        return queue.pop(0)

    monkeypatch.setattr(werss_client.requests, "request", fake_request)
    return WerssClient(BASE, "WKabcdef", "SKghijkl", min_gap=0.0), calls


def test_articles_request_shape_and_normalization(monkeypatch) -> None:
    """path/分页参数/认证头都得按上游合同来;缺标题或缺链接的条目一律丢掉。"""
    payload = {"list": [
        {"id": "a1", "mp_id": "MP_WXS_9", "title": " 文章一 ", "url": "https://mp.weixin.qq.com/s/a1",
         "description": "摘要一", "publish_time": 1760000000},
        {"id": "a2", "title": "没有链接", "url": ""},
        {"id": "a3", "title": "", "url": "https://mp.weixin.qq.com/s/a3"},
        "不是对象",
    ], "total": 4}
    client, calls = _client(monkeypatch, [_ok(payload)])
    items = client.mp_articles("MP_WXS_9", page=3, limit=20)
    assert calls[0]["method"] == "GET"
    assert calls[0]["url"] == f"{BASE}/api/v1/wx/articles"
    assert calls[0]["params"] == {"mp_id": "MP_WXS_9", "limit": "20", "offset": "40"}
    assert calls[0]["headers"]["Authorization"] == "AK-SK WKabcdef:SKghijkl"
    assert items == [{"id": "a1", "title": "文章一", "url": "https://mp.weixin.qq.com/s/a1",
                      "summary": "摘要一", "publish_at_raw": 1760000000}]


def test_articles_limit_clamped_to_upstream_max(monkeypatch) -> None:
    """上游 `limit` 有 le=100 的校验,递 500 会被整个请求拒掉 → 必须自己夹住。"""
    client, calls = _client(monkeypatch, [_ok({"list": []}), _ok({"list": []})])
    client.mp_articles("M", page=1, limit=500)
    assert calls[0]["params"]["limit"] == "100" and calls[0]["params"]["offset"] == "0"
    client.mp_articles("M", page=1, limit=0)   # 下限也要夹住,不能让上游报 ge=1
    assert calls[1]["params"]["limit"] == "1"


def test_feeds_single_page_maps_and_drops_nameless(monkeypatch) -> None:
    """一页订阅 → [{id, mp_name}];没有 id 的行丢掉(回填时没法用)。"""
    page = _ok({"list": [{"id": "MP_WXS_0", "mp_name": "号零"}, {"id": "", "mp_name": "无 id"},
                         {"id": "MP_WXS_x", "mp_name": " 号尾 "}]})
    client, calls = _client(monkeypatch, [page])
    feeds = client.list_feeds(kw="号", limit=50, offset=50)
    assert calls[0]["url"] == f"{BASE}/api/v1/wx/mps"
    assert calls[0]["params"] == {"limit": "50", "offset": "50", "kw": "号"}
    assert feeds == [{"id": "MP_WXS_0", "mp_name": "号零"}, {"id": "MP_WXS_x", "mp_name": "号尾"}]


def test_auth_and_business_failures_are_classified(monkeypatch) -> None:
    """401/403 → 认证错;`code!=0` → 业务错;个别错误分支挂在 201 上,所以只认 code。"""
    client, _ = _client(monkeypatch, [_Resp(401, text=""), _Resp(403, text="")])
    with pytest.raises(WerssAuthError):
        client.list_feeds()
    with pytest.raises(WerssAuthError):
        client.list_feeds()

    client, _ = _client(monkeypatch, [_Resp(201, {"code": 50001, "message": "获取文章列表失败"})])
    with pytest.raises(WerssError, match="获取文章列表失败"):
        client.mp_articles("M")

    client, _ = _client(monkeypatch, [_ok(None, code=0)])   # data=None → 空列表,不是崩
    assert client.mp_articles("M") == []


def test_transport_and_json_failures_raise_werss_error(monkeypatch) -> None:
    import requests

    def boom(*a, **kw):
        raise requests.RequestException("connection refused")

    monkeypatch.setattr(werss_client.requests, "request", boom)
    client = WerssClient(BASE, "WK", "SK", min_gap=0.0)
    with pytest.raises(WerssError, match="connection refused"):
        client.list_feeds()

    client, _ = _client(monkeypatch, [_Resp(200, None, text="<html>登录页</html>"), _Resp(500, None, text="x")])
    with pytest.raises(WerssError, match="非 JSON"):
        client.list_feeds()
    with pytest.raises(WerssError, match="HTTP 500"):
        client.list_feeds()


def test_werss_errors_degrade_the_listen_round(monkeypatch) -> None:
    """监听 ⓪ 分支只 `except PlatformError`——WeRSS 的异常必须是它的子类。

    否则 WeRSS 一挂不是"降级到微信读书",而是整轮监听抛异常。
    """
    assert issubclass(WerssError, PlatformError)
    assert issubclass(WerssAuthError, PlatformError)
    client = WerssClient("", "WK", "SK")            # 地址没配也不该在构造期炸
    with pytest.raises(WerssError, match="未配置"):
        client.list_feeds()


def test_resolve_mp_is_explicitly_unsupported(monkeypatch) -> None:
    """WeRSS 没有"文章链接→公众号"的解析接口:要一句人话,不是 AttributeError。"""
    client, calls = _client(monkeypatch, [])
    with pytest.raises(WerssError, match="不支持按文章链接解析"):
        client.resolve_mp("https://mp.weixin.qq.com/s/x")
    assert calls == []


def test_refresh_mp_swallows_throttle(monkeypatch) -> None:
    """手动刷新是锦上添花:被节流/上游失败只返回 False,不能把监听轮带崩。"""
    client, calls = _client(monkeypatch, [_ok({"started": True}),
                                          _Resp(200, {"code": 40402, "message": "操作过于频繁"})])
    assert client.refresh_mp("MP_WXS_9") is True
    assert client.refresh_mp("MP_WXS_9") is False
    assert calls[0]["method"] == "POST" and calls[0]["url"].endswith("/api/v1/wx/mps/update/MP_WXS_9")
