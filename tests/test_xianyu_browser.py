"""闲鱼**浏览器路径**单测(2026-10-02)。

纯协议被"哎哟喂,被挤爆啦"挡住,而"页面内调用"正常 —— 这层就是把请求
交给闲鱼自己的 JS 去发。这里只测**解析与错误映射**(不真开浏览器;真开的那条路
靠 `tools/xianyu_browser_probe.py` 现场验)。

⚠️ **2026-10-08 订正**:此前这里写"被**账号级**限流"——**已证伪**(匿名请求同样被挤爆)。
详见 `app/services/xianyu_browser.py`。
"""
import json

import pytest

from app.services import xianyu_browser as xb
from app.services.xianyu import XianyuError, XianyuVerify


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


# ---------------------------------------------------------------- 页面驱动搜索(2026-10-03)

class _PageDrivenPage:
    """页面驱动路:跳到搜索页后,`evaluate` 读回钩子截获的 `__xy_cap`。

    ⚠️ 2026-11-18 起**首选路是网络层**(`page.on("response")`)—— 这个替身必须
    同时扮演"事件发射器":`goto` 时把预设的响应打给监听器。少了 `on`/`remove_listener`,
    测试会以 `AttributeError` 假红,红的却是**替身太弱**而不是被测代码有问题。
    """

    def __init__(self, caps: list, direct_payload: str = "",
                 responses: list | None = None):
        self._caps = caps
        self._direct = direct_payload
        self._responses = responses or []      #: (url, body) —— goto 时逐个投递
        self._handlers: dict = {}
        self.goto_url = ""
        self.waited = 0

    # ---- playwright 事件接口 ----
    def on(self, event, fn):
        self._handlers.setdefault(event, []).append(fn)

    def remove_listener(self, event, fn):
        if fn in self._handlers.get(event, []):
            self._handlers[event].remove(fn)

    def goto(self, url, **kw):
        self.goto_url = url
        for u, body in self._responses:
            for fn in list(self._handlers.get("response", [])):
                fn(_FakeResponse(u, body))

    def evaluate(self, js, arg=None):
        if "__xy_cap" in js:
            return json.dumps(self._caps)
        if not self._direct:
            raise AssertionError("不该走到直连那条路")
        return self._direct

    def wait_for_timeout(self, ms):
        self.waited += ms


class _FakeResponse:
    """够用即可:监听器只读 `.url` 与 `.text()`。"""

    def __init__(self, url: str, body: str):
        self.url, self._body = url, body

    def text(self) -> str:
        return self._body


def _cap_payload(title: str, item_id: str) -> dict:
    return {"api": "mtop.taobao.idlemtopsearch.pc.search",
            "data": {"resultList": [{"item": {"main": {"exContent":
                {"title": title, "itemId": item_id}}}}]}}


def test_search_prefers_page_driven_and_captures_results() -> None:
    """⚠️ **让页面自己去搜**(2026-10-03 实测的修复)。

    同一浏览器同一登录态下,我们自己调 `idlemtopsearch.pc.search` 会被回
    `TIMEOUT::接口超时`,而**页面自己的 JS 发同样的搜索完全正常**
    (同一时刻 `idlehome.feed`/`user.page.nav` 都正常 → 不是环境被封、不是登录失效,
    **是这一个接口被单独限了**)。所以改成截获页面自己发出的搜索响应。
    """
    c = xb.XianyuBrowserClient()
    pg = _PageDrivenPage([_cap_payload("剪映会员秒发", "111")])
    c._pg = pg
    items = c.search("剪映会员")
    assert len(items) == 1 and items[0]["title"] == "剪映会员秒发"
    assert "/search?q=" in pg.goto_url and "%E5%89%AA%E6%98%A0" in pg.goto_url   # 词进了 URL


def test_search_captures_at_network_layer() -> None:
    """★★ **2026-11-18 实测定案:截获点必须在网络层**。

    现场用 `page.on("response")` 旁听看到搜索请求 **9/9 都发了出去、状态 200**,
    而 `window.__xy_cap` **一条都没截到**(它截到的 8 条全是 gaia/loginuser)
    ⇒ 页面已不再经过 `window.lib.mtop.request` 发搜索。

    只留钩子那条路时,首选路**静默返回 0**、再落直连必拿 `TIMEOUT::接口超时` ——
    一次「钩子失效」被伪装成「接口被限」。这条用例钉住:钩子全空、只有网络层有回包,
    也必须解析出商品。
    """
    body = json.dumps(_cap_payload("网盘资料合集", "999"))
    c = xb.XianyuBrowserClient()
    c._pg = _PageDrivenPage([], responses=[(
        "https://h5api.m.goofish.com/h5/mtop.taobao.idlemtopsearch.pc.search/1.0/", body)])
    items = c.search("网盘资料")
    assert len(items) == 1 and items[0]["title"] == "网盘资料合集"


def test_network_layer_ignores_punish_and_non_mtop_bodies() -> None:
    """⚠️ **判据不许用 `"success" in body`**(2026-11-18 栽过):滑块 punish 端点回的是
    `{"code":0,"dt":"success","ec":200,...}`,不含 `api`/`ret` —— 按子串判会把
    **被拦**读成**恢复**。只认 `api` 字段等于搜索接口的 mtop 回包。
    """
    punish = json.dumps({"code": 0, "dt": "success", "ec": 200,
                         "result": {"code": 0, "sig": "from bx"}, "success": True})
    other = json.dumps({"api": "mtop.taobao.idlehome.feed", "ret": ["SUCCESS::调用成功"],
                        "data": {"resultList": [{"item": {"main": {"exContent":
                            {"title": "不该被当成搜索结果", "itemId": "0"}}}}]}})
    c = xb.XianyuBrowserClient()
    c._pg = _PageDrivenPage([], direct_payload=json.dumps({"data": {"resultList": []}}),
                            responses=[
                                ("https://h5api.m.goofish.com/_____tmd_____/punish?x=1", punish),
                                ("https://h5api.m.goofish.com/h5/mtop.taobao.idlehome.feed/1.0/", other),
                            ])
    #: 两条都该被忽略 ⇒ 页面路 0 条 → 落到直连 → 直连也是空 → 明确报错
    with pytest.raises(XianyuError) as ei:
        c.search("ps教程")
    assert "未解析到商品" in str(ei.value)


def test_page_risk_control_raises_verify_instead_of_empty() -> None:
    """★★ **滑块不许伪装成 TIMEOUT**(2026-11-18)。

    网络层看到页面自己的搜索被回 `FAIL_SYS_USER_VALIDATE` / `RGV587_ERROR` 时,
    必须抛 `XianyuVerify` —— `collect_hot` 靠它**立刻停轮**并上报"需人工过滑块"。
    若吞成空结果再落 `_search_direct`,拿回的是 `TIMEOUT::接口超时`,
    真因从"被风控"退化成"接口超时",值班的人会去查网络而不是去拖滑块。

    ⚠️ **回包里没有 `api` 字段** —— 这是现场抓到的真实形状,不是简化。第一版判据
    要求 `obj["api"] == API` 才往下看,于是**真的风控回包被当成 other 扔掉**,
    监测脚本读数显示 `RGV587=0`、整条风控判定形同虚设。
    """
    risk = json.dumps({"ret": ["FAIL_SYS_USER_VALIDATE",
                               "RGV587_ERROR::SM::哎哟喂,被挤爆啦,请稍后重试"],
                       "data": {"url": "https://h5api.m.goofish.com/_____tmd_____/punish"}})
    c = xb.XianyuBrowserClient()
    c._pg = _PageDrivenPage([], direct_payload="不该走到这里", responses=[(
        "https://h5api.m.goofish.com/h5/mtop.taobao.idlemtopsearch.pc.search/1.0/", risk)])
    with pytest.raises(xb.XianyuVerify) as ei:
        c.search("网盘资源")
    assert "滑块" in str(ei.value) and "RGV587" in str(ei.value)


def test_session_expired_on_search_still_means_slider() -> None:
    """★★ **`SESSION_EXPIRED` 不等于「登录没了」**(2026-11-18 栽过,两次)。

    现场同批读数互相矛盾:搜索接口回 `RGV587 被挤爆啦`,而
    `loginuser.get` / `user.page.nav` **也**回 `FAIL_SYS_SESSION_EXPIRED::Session过期`。
    我据此判成「登录态失效、拖滑块没用」,让用户白等一轮 ——
    而**同一时刻**页面右上角有用户名、`redpoint.query` 回 `SUCCESS total=14`,
    滑块弹窗也好好地挂在 `baxia-dialog-content` iframe 里。

    ⇒ 搜索侧见到 `SESSION_EXPIRED` **照旧归滑块**(去拖),真过期由
    `scripts/xianyu_login.py` 用「多个接口交叉验证」去判,别在这里一刀切。
    """
    expired = json.dumps({"ret": ["FAIL_SYS_SESSION_EXPIRED::Session过期"], "data": {}})
    c = xb.XianyuBrowserClient()
    c._pg = _PageDrivenPage([], direct_payload="不该走到这里", responses=[(
        "https://h5api.m.goofish.com/h5/mtop.taobao.idlemtopsearch.pc.search/1.0/", expired)])
    #: 归滑块(去拖),**不是** `XianyuCookieExpired`(去重新登录);
    #: 也**不许**静默变成"这次没商品" —— 那正是本仓最忌讳的静默失败。
    with pytest.raises(xb.XianyuVerify) as ei:
        c.search("网盘资源")
    assert "滑块" in str(ei.value), f"应归滑块:{ei.value}"


def test_shade_and_activate_subpaths_are_not_mistaken_for_search() -> None:
    """同一条 URL 前缀下还有 `…pc.search.shade` / `…item.search.activate`(激活页),
    **它们不是搜索结果**;`api` 字段存在时用来交叉校验,把这两个排除掉。

    反例意义:若只看 URL 前缀,`activate` 回包里也有 `cardList` 商品形状,
    会被误当成搜索结果写进库(=假数据)。
    """
    activate = json.dumps({"api": "mtop.taobao.idlemtopsearch.pc.item.search.activate",
                           "ret": ["SUCCESS::调用成功"],
                           "data": {"cardList": [{"cardData": {"item": {"main": {"exContent":
                               {"title": "激活页里的假商品", "itemId": "0"}}}}}]}})
    c = xb.XianyuBrowserClient()
    c._pg = _PageDrivenPage([], direct_payload=json.dumps({"data": {"resultList": []}}),
                            responses=[(
                                "https://h5api.m.goofish.com/h5/"
                                "mtop.taobao.idlemtopsearch.pc.item.search.activate/1.0/",
                                activate)])
    with pytest.raises(XianyuError) as ei:
        c.search("ps教程")
    assert "未解析到商品" in str(ei.value)


def test_search_falls_back_to_direct_when_page_yields_nothing() -> None:
    """页面路拿不到东西(钩子失效/站点改版) → **退回直连**,别整条链判死。"""
    direct = json.dumps({"data": {"resultList": [
        {"item": {"main": {"exContent": {"title": "直连拿到的", "itemId": "222"}}}}]}})
    c = xb.XianyuBrowserClient()
    c._pg = _PageDrivenPage([], direct_payload=direct)
    items = c.search("ps教程")
    assert len(items) == 1 and items[0]["title"] == "直连拿到的"


def test_search_raises_when_both_paths_fail() -> None:
    """两条路都空 → 明确报错(不能静默当 0 条)。"""
    c = xb.XianyuBrowserClient()
    c._pg = _PageDrivenPage([], direct_payload=json.dumps({"data": {"resultList": []}}))
    with pytest.raises(XianyuError) as ei:
        c.search("ps教程")
    assert "未解析到商品" in str(ei.value)


def test_hook_is_injected_before_navigation() -> None:
    """钩子必须走 `add_init_script`(每次页面加载都先执行)。

    ⚠️ 实测:用 `page.evaluate` 装钩子会被**跳转清掉**,截获恒为 0。
    """
    src = (xb.__file__ and open(xb.__file__, encoding="utf-8").read()) or ""
    assert "add_init_script(_HOOK_INIT)" in src
    assert "__xy_patched" in src          # 防重复包(同一页可能加载多次)


# ------------------------- 浏览器"没了"的自愈 + 闲置自动关(2026-10-04)

def test_looks_closed_only_matches_browser_gone() -> None:
    """只认"浏览器没了" —— **别的错照旧冒泡**:风控/限流靠重试只会撞得更狠。"""
    assert xb._looks_closed(RuntimeError("Target page, context or browser has been closed"))
    assert xb._looks_closed(RuntimeError("Browser has been closed"))
    assert not xb._looks_closed(XianyuError("RGV587_ERROR::哎哟喂,被挤爆啦"))
    assert not xb._looks_closed(RuntimeError("TIMEOUT::接口超时"))


def test_launch_failure_releases_playwright_driver(monkeypatch) -> None:
    """★★ **2026-11-23 事故**:启动失败后必须把 `_pw` 清掉。

    现场:远程容器**没装 Playwright 浏览器** ⇒ `launch_persistent_context` 报
    `Executable doesn't exist`。而 `_pw` 已经 start 过、没被清 ⇒ **下一轮**
    `sync_playwright().start()` 报 `It looks like you are using Playwright Sync API
    inside the asyncio loop` —— 一句和真实原因**毫无关系**的话,把排障方向带偏
    (我据此去查过调度器的线程模型)。

    钉的是**性质**:第二次尝试时 `_pw is None`(即会重新 start,而不是复用死驱动);
    并且**真实原因原样冒泡**(不能被替换成 asyncio 那句)。
    """
    calls: list[str] = []

    class _FakeChromium:
        def launch_persistent_context(self, **kw):
            calls.append(kw.get("channel") or "chromium")
            raise RuntimeError("Executable doesn't exist at /root/.cache/ms-playwright/x")

    class _FakePW:
        chromium = _FakeChromium()

        def start(self):
            return self

        def stop(self):
            calls.append("stopped")

    class _FakeSync:
        def start(self):
            return _FakePW()

    import playwright.sync_api as sync_api

    monkeypatch.setattr(sync_api, "sync_playwright", lambda: _FakeSync())
    c = xb.XianyuBrowserClient()
    with pytest.raises(RuntimeError, match="Executable doesn't exist"):
        c._ensure_page()
    assert calls[-1] == "stopped", calls            # 驱动被收尾
    assert c._pw is None, "启动失败必须清掉 _pw,否则下一轮会报 asyncio loop 的假原因"
    assert c._pg is None and c._ctx is None

    # 第二轮:能重新 start(如果 _pw 没清,真实 Playwright 在这一步就抛 asyncio loop 错)
    with pytest.raises(RuntimeError, match="Executable doesn't exist"):
        c._ensure_page()
    assert calls.count("stopped") == 2


def test_idle_close_seconds_default_and_off_switch() -> None:
    """默认 15 分钟;**配 ≤0 = 不关**(退回旧行为,给不想被关的人留后路)。"""

    class _Default:
        pass

    class _Off:
        xianyu_browser_idle_close_sec = 0

    class _Custom:
        xianyu_browser_idle_close_sec = 60

    assert xb._idle_close_seconds(_Default()) == 900
    assert xb._idle_close_seconds(_Off()) == 0
    assert xb._idle_close_seconds(_Custom()) == 60


def test_window_closed_by_hand_is_rebuilt_transparently(monkeypatch) -> None:
    """⚠️ 用户看到桌面那个 `about:blank` 窗口会想**手动关** —— 而此前关了**不会重开**:
    `_ThreadBoundClient` 见 `_client` 非空就直接用、`_ensure_page` 见 `_pg` 非空也不重建,
    闲鱼链会**一路失败到重启为止**。现在必须**丢掉坏实例、重建一次**。
    """
    built: list[int] = []

    class _FakeClient:
        def __init__(self, settings=None):
            built.append(1)

        def search(self, keyword, page=1, rows=30):
            if len(built) == 1:                 # 第一个实例 = 已经被手工关掉的那个
                raise RuntimeError("Target page, context or browser has been closed")
            return [{"title": keyword, "item_id": "1"}]

        def close(self):
            pass

    monkeypatch.setattr(xb, "XianyuBrowserClient", _FakeClient)
    monkeypatch.setattr(xb, "_arm_idle_close", lambda *a, **k: None)   # 别真排定时器
    c = xb._ThreadBoundClient()

    assert c.search("剪映会员") == [{"title": "剪映会员", "item_id": "1"}]
    assert len(built) == 2, "被关掉的浏览器没有被重建"
    c.close()


def test_closed_browser_still_fails_after_one_rebuild(monkeypatch) -> None:
    """重建后仍失败 → **照旧抛**,不无限重试(否则一条链就卡死在这里)。"""

    class _AlwaysClosed:
        def __init__(self, settings=None):
            pass

        def search(self, keyword, page=1, rows=30):
            raise RuntimeError("Target page, context or browser has been closed")

        def close(self):
            pass

    monkeypatch.setattr(xb, "XianyuBrowserClient", _AlwaysClosed)
    monkeypatch.setattr(xb, "_arm_idle_close", lambda *a, **k: None)
    c = xb._ThreadBoundClient()

    with pytest.raises(RuntimeError, match="has been closed"):
        c.search("剪映会员")
    c.close()


def test_close_if_idle_only_fires_when_really_idle(monkeypatch) -> None:
    """⚠️ 闲置判定必须**在专属线程里复查一次**:定时器到点时可能刚好有新的一轮进来
    (`max_workers=1` 意味着这个任务排在它后面),那时就不该关 —— 否则会把正在跑的一轮掐断。
    """
    closed: list[int] = []

    class _FakeClient:
        def __init__(self, settings=None):
            pass

        def search(self, keyword, page=1, rows=30):
            return []

        def close(self):
            closed.append(1)

    monkeypatch.setattr(xb, "XianyuBrowserClient", _FakeClient)
    monkeypatch.setattr(xb, "_arm_idle_close", lambda *a, **k: None)
    c = xb._ThreadBoundClient()
    c.search("x")                            # 先把客户端建起来

    assert c.close_if_idle(3600) is False, "刚用过就关 —— 会把正在跑的一轮掐断"
    assert closed == []
    assert c.close_if_idle(0) is True, "真闲置了就该关"
    assert closed == [1]

    c.search("x")                            # 关掉之后还能自动重开
    c.close()
