"""夸克分享统计单测(2026-10-03 补 —— 此前**没有测试**)。

**为什么值得测**:这个模块的错误分类是**今天反复立的那条规矩的样板** ——
网络异常 / 401 登录态失效 / 非 200 / 非 JSON / 业务码非 0,**各抛各的异常**,
调用方(API、`backfill_suggestions`)能据此区分"该让用户重贴 Cookie"还是"就是网络抖了"。
分类退化成一个笼统的 Exception,下游就只能猜 —— 这类回归注释拦不住,得靠测试。

(它的字段全量回填功能本身因夸克不提供链接级转存数而**不可用** —— 那是定案的,
但**解析与错误分类**这部分依然值得钉住。)
"""
import pytest

from app.services import quark_share_stats as qs


class _Resp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def _patch_post(monkeypatch, resp=None, exc=None):
    def _post(*a, **k):
        if exc is not None:
            raise exc
        return resp

    monkeypatch.setattr(qs.requests, "post", _post)


# ---------------------------------------------------------------- 错误分类(核心)

def test_network_error_maps_to_error(monkeypatch) -> None:
    _patch_post(monkeypatch, exc=qs.requests.RequestException("boom"))
    with pytest.raises(qs.QuarkShareStatsError) as ei:
        qs.fetch_share_page("ck")
    assert "网络异常" in str(ei.value)


def test_401_maps_to_auth_error_so_user_knows_to_repaste(monkeypatch) -> None:
    """⚠️ **401 要单独分类**:它意味着"**让用户去重贴 Cookie**",与"网络抖了"的处置完全不同。"""
    _patch_post(monkeypatch, resp=_Resp(status_code=401))
    with pytest.raises(qs.QuarkShareStatsAuthError) as ei:
        qs.fetch_share_page("ck")
    assert "Cookie" in str(ei.value)


def test_non_200_maps_to_error(monkeypatch) -> None:
    _patch_post(monkeypatch, resp=_Resp(status_code=500))
    with pytest.raises(qs.QuarkShareStatsError):
        qs.fetch_share_page("ck")


def test_non_json_maps_to_error(monkeypatch) -> None:
    _patch_post(monkeypatch, resp=_Resp(payload=None, text="<html>"))
    with pytest.raises(qs.QuarkShareStatsError) as ei:
        qs.fetch_share_page("ck")
    assert "非 JSON" in str(ei.value)


def test_business_code_nonzero_maps_to_error(monkeypatch) -> None:
    """HTTP 200 但业务码非 0(如风控)→ 同样要报错,别当"没有分享"。"""
    _patch_post(monkeypatch, resp=_Resp(payload={"code": 1234, "message": "风控"}))
    with pytest.raises(qs.QuarkShareStatsError) as ei:
        qs.fetch_share_page("ck")
    assert "风控" in str(ei.value)


def test_success_returns_list_and_metadata(monkeypatch) -> None:
    _patch_post(monkeypatch, resp=_Resp(payload={"code": 0, "data": {
        "list": [{"share_id": "s1"}], "metadata": {"_total": 1}}}))
    items, meta = qs.fetch_share_page("ck")
    assert items == [{"share_id": "s1"}] and meta["_total"] == 1


# ---------------------------------------------------------------- 其他

def test_ms_to_dt_tolerates_bad_input() -> None:
    """毫秒时间戳转换要扛住各种脏值(接口给 null/字符串/越界都可能)。"""
    assert qs._ms_to_dt(None) is None
    assert qs._ms_to_dt(0) is None
    assert qs._ms_to_dt("not-a-number") is None
    assert qs._ms_to_dt(10 ** 20) is None              # 越界 → OverflowError 也要接住
    assert qs._ms_to_dt(1700000000000) is not None


def test_collect_all_paginates_and_stops_at_total(monkeypatch) -> None:
    """翻页:凑够 `metadata._total` 就停(别多翻一页)。"""
    pages = {1: ([{"share_id": "a"}], {"_total": 2}),
             2: ([{"share_id": "b"}], {"_total": 2})}
    monkeypatch.setattr(qs, "fetch_share_page",
                        lambda ck, page, *a, **k: pages.get(page, ([], {"_total": 2})))
    assert [x["share_id"] for x in qs.collect_all("ck")] == ["a", "b"]


def test_row_from_item_truncates_and_defaults() -> None:
    """字段映射:超列宽要截断(`title` 是 String(255)),缺字段给默认值而不是 None。"""
    row = qs._row_from_item(7, {"share_id": "s", "title": "字" * 400})
    assert row["user_id"] == 7 and len(row["title"]) == 255
    assert row["save_pv"] == 0 and row["click_pv"] == 0 and row["status"] == 0
