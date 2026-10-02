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


class _FlakyPage:
    """前两次抛"上下文被跳转销毁",第三次成功 —— 复刻实测的竞态。"""

    def __init__(self, payload: str, fail_times: int = 2):
        self._payload, self._left = payload, fail_times
        self.waited = 0

    def evaluate(self, js, arg=None):
        if self._left > 0:
            self._left -= 1
            raise RuntimeError("Execution context was destroyed, most likely because of a navigation")
        return self._payload

    def wait_for_timeout(self, ms):
        self.waited += ms


def test_search_retries_when_page_navigates_under_it() -> None:
    """⚠️ **页面在 `goto` 之后又自己跳了一次**时,`evaluate` 会报
    "Execution context was destroyed ... navigation"(实测 23:45 那轮定时采集踩到,
    手跑却没事 —— 典型竞态)。**重试**即可,别当"闲鱼挂了"。"""
    payload = json.dumps({"data": {"resultList": [
        {"item": {"main": {"exContent": {"title": "剪映会员", "itemId": "1"}}}}]}})
    page = _FlakyPage(payload, fail_times=2)
    c = xb.XianyuBrowserClient()
    c._pg = page
    items = c.search("剪映会员")
    assert len(items) == 1
    assert page.waited == 3000, "两次重试各等 1500ms"


def test_search_gives_up_after_three_attempts() -> None:
    """连续 3 次都被跳转打断 → 明确报错(带上原因),不静默当 0 条。"""
    c = xb.XianyuBrowserClient()
    c._pg = _FlakyPage("{}", fail_times=99)
    with pytest.raises(XianyuError) as ei:
        c.search("剪映会员")
    assert "重试 3 次" in str(ei.value)


def test_detail_uses_in_page_api() -> None:
    """**行情(想要数/收藏/出单/浏览量)只有详情接口有** —— 浏览器路径也必须能取。

    2026-10-03 发现:深采此前**还在用纯协议客户端**,而搜索早换了浏览器路 →
    行情一直卡在"被挤爆"那条路上。这条用例钉住"详情也走页面内调用"。
    """
    payload = json.dumps({"data": {"itemDO": {"wantCnt": 12, "collectCnt": 3, "soldCnt": 1}}})
    c = _client(payload)
    obj = c.detail("123")
    assert obj["data"]["itemDO"]["wantCnt"] == 12
    api, data = c._pg.calls[0]
    assert api == xb.DETAIL_API and data == {"itemId": "123"}


def test_thread_bound_client_keeps_all_work_on_one_thread(monkeypatch) -> None:
    """⚠️ Playwright 的 sync 对象**只能在创建它的线程里用**,而调度器是多线程的
    (`ThreadPoolExecutor(24)`,FastAPI 同步端点另有一套线程池)。

    实测 2026-10-03 00:50 那轮采集报 `Cannot switch to a different thread` —— 前两轮恰好
    复用同一线程没事,**第三轮换了线程就炸**(这种"偶发、像网络抖动"的失败最难查)。
    这里用假客户端钉住:`_ThreadBoundClient` 无论被哪个线程调用,真正干活的客户端
    **始终建在同一个线程**里。
    """
    import threading

    seen: list[int] = []

    class _FakeClient:
        def __init__(self, settings=None):
            seen.append(threading.get_ident())          # 记录"客户端建在哪个线程"

        def search(self, keyword, page=1, rows=30):
            seen.append(threading.get_ident())          # 干活也必须在同一线程
            return [{"title": keyword, "item_id": "1"}]

        def detail(self, item_id):
            seen.append(threading.get_ident())
            return {"itemId": item_id}

        def close(self):
            seen.append(threading.get_ident())

    monkeypatch.setattr(xb, "XianyuBrowserClient", _FakeClient)
    c = xb._ThreadBoundClient()
    out: list = []
    threads = [threading.Thread(target=lambda: out.append(c.search("剪映会员"))) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(out) == 6
    assert len(set(seen)) == 1, f"客户端被建在了多个线程上:{set(seen)}"
    c.close()


def test_get_client_returns_thread_bound_proxy() -> None:
    """`get_client()` 必须给**线程绑定**的代理(否则跨线程调用会炸)。"""
    xb.close_client()
    assert isinstance(xb.get_client(), xb._ThreadBoundClient)
    xb.close_client()
