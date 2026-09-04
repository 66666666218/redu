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


def test_collect_hot_raises_verify_when_all_keywords_blocked(settings: Settings) -> None:
    """全部关键词都被人机验证挡住时,collect_hot 应上抛 XianyuVerify。

    否则上层会把它当"搜索成功 0 条"且继续,掩盖"账号/出口被标记"的真实故障。
    """
    from app.services.xianyu import XianyuVerify

    class _VerifyClient:
        def search(self, keyword: str) -> list[dict]:
            raise XianyuVerify(f"闲鱼人机验证(滑块),keyword={keyword}")

    settings.xianyu_keywords = "kw1,kw2"
    settings.xianyu_top_n = 10
    with pytest.raises(XianyuVerify):
        collect_hot(settings, client=_VerifyClient())


def test_collect_hot_partial_verify_keeps_remaining(settings: Settings) -> None:
    """仅部分关键词被验证时,已成功的关键词数据仍应保留,不整体抛错。"""
    from app.services.xianyu import XianyuVerify

    class _HalfBlockClient:
        def search(self, keyword: str) -> list[dict]:
            if keyword == "bad":
                raise XianyuVerify("闲鱼人机验证(滑块)")
            return [_item(1, "A"), _item(2, "B")]

    settings.xianyu_keywords = "good,bad"
    settings.xianyu_top_n = 10
    out = collect_hot(settings, client=_HalfBlockClient())
    assert len(out) == 2  # good 关键词的数据仍在


def test_post_backs_off_on_rate_limit_then_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """真限流码应退避重试(不连环猛打),仍失败抛 XianyuRateLimit。"""
    from app.services import xianyu
    from app.services.xianyu import XianyuClient, XianyuRateLimit

    calls = {"n": 0}
    monkeypatch.setattr(xianyu.time, "sleep", lambda s: None)  # 退避不真等

    class _Resp:
        def json(self): return {"ret": ["FAIL_SYS_RATE_LIMIT::请求过多"]}

    client = XianyuClient("tracknick=x; ")
    monkeypatch.setattr(client.session, "post", lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1), _Resp())[1])

    with pytest.raises(XianyuRateLimit):
        client.search("ps教程", rows=5)
    # 1 次初始 + 3 次退避重试 = 4 次请求
    assert calls["n"] == 4


def test_post_verify_raises_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    """人机验证(FAIL_SYS_USER_VALIDATE)不是限流:立即抛 XianyuVerify,不退避(实测重试无效)。

    这正是与旧行为的差异——旧代码把它当限流退避 3 次×30/90/180s(白等 5 分钟)。
    """
    from app.services import xianyu
    from app.services.xianyu import XianyuClient, XianyuVerify

    calls = {"n": 0}
    monkeypatch.setattr(xianyu.time, "sleep", lambda s: None)

    class _Resp:
        def json(self): return {"ret": ["FAIL_SYS_USER_VALIDATE::需要验证"]}

    client = XianyuClient("tracknick=x; ")
    monkeypatch.setattr(client.session, "post", lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1), _Resp())[1])

    with pytest.raises(XianyuVerify):
        client._post("mtop.taobao.idlemtopsearch.pc.search", {"keyword": "k"})
    assert calls["n"] == 1  # 只发 1 次请求,绝不退避


def test_post_returns_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """正常 SUCCESS 响应应直接返回,不触发退避。"""
    from app.services import xianyu
    from app.services.xianyu import XianyuClient

    class _Resp:
        def json(self): return {"ret": ["SUCCESS::调用成功"], "data": {"biz": {"list": []}}}

    client = XianyuClient("tracknick=x; ")
    monkeypatch.setattr(client.session, "post", lambda *a, **k: _Resp())
    assert client._post("mtop.taobao.idlemtopsearch.pc.search", {"keyword": "k"})["ret"][0].startswith("SUCCESS")
