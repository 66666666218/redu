"""闲鱼热榜采集:抽取/价格/去重排名单测(纯逻辑,无网络)。"""
import pytest

from config.settings import Settings
from app.services.xianyu import _extract_items, collect_hot, item_price


def _item(iid: int, title: str, price: object = None) -> dict:
    return {
        "itemId": str(iid),
        "title": title,
        "price": price if price is not None else [{"text": "¥"}, {"text": "1"}],
        "userNickName": "卖家甲",
        "picUrl": f"p{iid}.png",
    }


class FakeClient:
    def __init__(self, per_kw: dict[str, list[dict]]) -> None:
        self.per_kw = per_kw

    def search(self, keyword: str) -> list[dict]:
        return self.per_kw[keyword]


def test_extract_items_from_nested() -> None:
    obj = {"data": {"resultInfo": {"items": [_item(1, "A"), _item(2, "B")]}}}
    items = _extract_items(obj)
    assert len(items) == 2
    assert items[0]["title"] == "A"


def test_price_normalizes_repeated_yen() -> None:
    assert item_price({"price": [{"text": "¥"}, {"text": "¥"}, {"text": "1.5"}]}) == "¥1.5"


def test_collect_hot_dedupe_and_rank(settings: Settings) -> None:
    fake = FakeClient({"kw1": [_item(1, "A"), _item(2, "B")], "kw2": [_item(1, "A")]})
    settings.xianyu_keywords = "kw1,kw2"
    settings.xianyu_top_n = 10
    out = collect_hot(settings, client=fake)
    assert len(out) == 2
    assert out[0]["item_id"] == "1"
    assert out[0]["hit_keywords"] == 2
    assert out[0]["best_rank"] == 1
    assert out[1]["item_id"] == "2"


def test_collect_hot_respects_top_n(settings: Settings) -> None:
    fake = FakeClient({"kw": [_item(i, f"商品{i}") for i in range(50)]})
    settings.xianyu_keywords = "kw"
    settings.xianyu_top_n = 3
    out = collect_hot(settings, client=fake)
    assert len(out) == 3


def test_post_backs_off_on_rate_limit_then_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """限流/风控码应退避重试(不连环猛打),仍失败抛 XianyuRateLimit。"""
    from app.services import xianyu
    from app.services.xianyu import XianyuClient, XianyuRateLimit

    calls = {"n": 0}
    monkeypatch.setattr(xianyu.time, "sleep", lambda s: None)  # 退避不真等

    class _Resp:
        def json(self): return {"ret": ["FAIL_SYS_USER_VALIDATE::需要验证"]}

    client = XianyuClient("tracknick=x; ")
    monkeypatch.setattr(client.session, "post", lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1), _Resp())[1])

    with pytest.raises(XianyuRateLimit):
        client.search("ps教程", rows=5)
    # 1 次初始 + 3 次退避重试 = 4 次请求
    assert calls["n"] == 4


def test_post_returns_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """正常 SUCCESS 响应应直接返回,不触发退避。"""
    from app.services import xianyu
    from app.services.xianyu import XianyuClient

    class _Resp:
        def json(self): return {"ret": ["SUCCESS::调用成功"], "data": {"biz": {"list": []}}}

    client = XianyuClient("tracknick=x; ")
    monkeypatch.setattr(client.session, "post", lambda *a, **k: _Resp())
    assert client._post("mtop.taobao.idlemtopsearch.pc.search", {"keyword": "k"})["ret"][0].startswith("SUCCESS")
