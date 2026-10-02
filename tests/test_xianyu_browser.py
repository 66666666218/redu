"""闲鱼**浏览器路径**单测(2026-10-02)。

纯协议被"哎哟喂,被挤爆啦"**账号级**限流,而"页面内调用"正常 —— 这层就是把请求
交给闲鱼自己的 JS 去发。这里只测**解析与错误映射**(不真开浏览器;真开的那条路
靠 `tools/xianyu_browser_probe.py` 现场验)。
"""
import json

import pytest

from app.services import xianyu_browser as xb
from app.services.xianyu import XianyuError


class _FakePage:
    """假的页面:把预设的返回/异常当成 `evaluate` 的结果吐出来。"""

    def __init__(self, payload: str):
        self._payload = payload
        self.calls: list[list] = []

    def evaluate(self, js, arg=None):
        self.calls.append(arg)
        return self._payload


def _client(payload: str) -> xb.XianyuBrowserClient:
    c = xb.XianyuBrowserClient()
    c._pg = _FakePage(payload)              # 跳过真开浏览器
    return c


def test_search_parses_items_and_passes_payload() -> None:
    """页面内调用返回的商品要能解析出来(与协议路同一个 `_extract_items`)。"""
    obj = {"api": "x", "data": {"resultList": [
        {"item": {"main": {"exContent": {"title": "PS教程全套", "itemId": "111"}}}},
        {"item": {"main": {"exContent": {"title": "剪映会员", "itemId": "222"}}}}]}}
    c = _client(json.dumps(obj))
    items = c.search("ps教程", rows=20)
    assert len(items) == 2 and items[0]["title"] == "PS教程全套"
    from app.services.xianyu import API

    api, payload = c._pg.calls[0]
    assert api == API                     # 搜的就是闲鱼搜索那个 mtop 接口
    assert payload["keyword"] == "ps教程" and payload["rowsPerPage"] == 20


def test_search_maps_mtop_business_error_to_xianyu_error() -> None:
    """mtop 自己回的业务错误(如 TIMEOUT/错误请求类型)要包成 XianyuError,**别静默当 0 条**。"""
    c = _client(json.dumps({"__err": '{"ret":["TIMEOUT::接口超时"],"retJson":-1}'}))
    with pytest.raises(XianyuError) as ei:
        c.search("ps教程")
    assert "TIMEOUT" in str(ei.value)


def test_search_maps_risk_control_error() -> None:
    """页面内**也**被风控时,错误文案要能认出来(交给上层冷却/告警,别当成普通失败)。"""
    c = _client(json.dumps({"__err": 'RGV587_ERROR::SM::哎哟喂,被挤爆啦'}))
    with pytest.raises(XianyuError) as ei:
        c.search("ps教程")
    assert "风控" in str(ei.value)


def test_search_raises_when_no_items() -> None:
    """返回里没有商品 → 明确报错(不能当"0 条"记成功,那会让热点假消失)。"""
    c = _client(json.dumps({"data": {"resultList": []}}))
    with pytest.raises(XianyuError) as ei:
        c.search("ps教程")
    assert "未解析到商品" in str(ei.value)


def test_shared_client_is_reused() -> None:
    """进程内**复用**同一个客户端:开一次浏览器 10~20 秒,一轮多词别反复开关。"""
    xb.close_client()
    a = xb.get_client()
    b = xb.get_client()
    assert a is b
    xb.close_client()
    assert xb.get_client() is not a
    xb.close_client()
