"""小红书**浏览器登录**单测(2026-10-12)。

## 它盯的是什么:一个**会静默骗人**的坑
`web_session` 的 `04` 前缀只说明"**曾经**真登录过",**不说明它现在还活着**。

实测(2026-10-12):一枚已被服务端判 `-100 登录已过期` 的死会话,**前缀仍然是 `04`**。
于是 `is_real_session()`(只认前缀)当场返回 True ⇒ 登录脚本 **26 秒就报告"登录成功"**,
其实只是把那枚死会话又归档了一遍 —— 而且越归档越差(cookie 15 项被覆盖成 12 项、
`webSsk` 也没了),人却以为"这轮修好了"。

⇒ 判据必须是 **"会话真的换过"**:
   · 早退(档案已登好)⇒ 前缀 `04` **且** `webSsk` 还在;
   · 等待重登 ⇒ `web_session` 的**值变了**,或 `webSsk` **从无到有**。

## 另一个"说准是哪种坏"的点
超时报错要区分 **"没登录"** 和 **"没重新登录"**:
前者是去登录,后者是**先退出当前账号再登一次** —— 修法完全不同。

⚠️ 全部用例用**假 Playwright**,不开浏览器、不碰网络。
"""
from __future__ import annotations

import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest  # noqa: E402

from app.services import xhs_browser_login as L  # noqa: E402

SSK_REAL = "rkiV8+4u4qXzpaMy"          #: 假装一枚真的 webSsk
EDGE_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36 Edg/154.0.0.0")
HINTS_RAW = {"brands": [{"brand": "Microsoft Edge", "version": "154"},
                        {"brand": "Chromium", "version": "154"}],
             "mobile": False, "platform": "Windows"}

#: 一枚**看起来**像真登录(前缀 04)、实际已死的会话
STALE = {"web_session": "040069deadbeef", "a1": "a" * 52, "webId": "w" * 32,
         "id_token": "t", "gid": "g", "websectiga": "s", "sec_poison_id": "p",
         "xsecappid": "xhs-pc-web", "acw_tc": "c"}
#: 一枚**新签发**的健康会话
FRESH = dict(STALE, web_session="040069fresh001")
GUEST = dict(STALE, web_session="030069guest0000")


class _FakePage:
    def __init__(self, ctx: "_FakeCtx") -> None:
        self.ctx = ctx

    def goto(self, *a, **k) -> None:
        return None

    def evaluate(self, js: str, *a):
        if "userAgentData" in js:
            return self.ctx.hints_raw
        if "localStorage" in js:
            #: ⚠️ `webSsk` 用**自己的**计数器推进,不能跟 cookie 的索引耦合 ——
            #: 两者调用次数不一样(循环里每次都读 cookie,但只在判定成功时才读 ssk)。
            ss = self.ctx.ssks
            got = ss[min(self.ctx.ssk_i, len(ss) - 1)]
            self.ctx.ssk_i += 1
            return got
        if "userAgent" in js:
            return self.ctx.ua
        return ""


class _FakeCtx:
    """假浏览器上下文。每次 `cookies()` 推进一格(模拟"人在慢慢登录")。"""

    def __init__(self, cookie_states, ssks, ua=EDGE_UA, hints_raw=HINTS_RAW) -> None:
        self.cookie_states = cookie_states
        self.ssks = ssks
        self.ua = ua
        self.hints_raw = hints_raw
        self.i = 0
        self.ssk_i = 0
        self.closed = False
        self._page: _FakePage | None = None

    @property
    def pages(self):
        return []

    def new_page(self) -> _FakePage:
        if self._page is None:
            self._page = _FakePage(self)
        return self._page

    def cookies(self, url=None) -> list[dict]:
        st = self.cookie_states[min(self.i, len(self.cookie_states) - 1)]
        self.i += 1
        return [{"name": k, "value": v} for k, v in st.items()]

    def close(self) -> None:
        self.closed = True


def _patch(monkeypatch, ctx) -> None:
    """把 `wait_for_login` 依赖的浏览器/网络/睡眠全换成假的。"""
    class _Chromium:
        @staticmethod
        def launch_persistent_context(**kw):
            return ctx

    class _P:
        chromium = _Chromium()

    class _Sync:
        def __enter__(self):
            return _P()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("playwright.sync_api.sync_playwright", _Sync)
    monkeypatch.setattr(L, "_probe_egress", lambda c: "1.2.3.4")
    monkeypatch.setattr(L.time, "sleep", lambda s: None)   #: 别真睡,只跳过等待


class TestIsRealSession:
    def test_04_算真登录_03_不算(self) -> None:
        assert L.is_real_session({"web_session": "040069x"})
        assert not L.is_real_session({"web_session": "030069x"})
        assert not L.is_real_session({})

    def test_前缀只是必要不充分_它认不出死会话(self) -> None:
        """★ 这条就是整个文件的由头:**死会话也是 `04`**,所以不能只靠它下结论。"""
        assert L.is_real_session(STALE) is True


class TestStaleSessionIsNotSuccess:
    """★ 档案里躺着一枚死掉的 `04` ⇒ **绝不能**当成"已登录"交差。"""

    def test_有一枚死_04_时不会早退_而是等人重登(self, monkeypatch, tmp_path) -> None:
        ctx = _FakeCtx([STALE, STALE], ["", ""])        #: 会话一直没换
        _patch(monkeypatch, ctx)
        res = L.wait_for_login(str(tmp_path), timeout_s=0.3, poll_s=0)
        assert res.ok is False
        #: ★ 报错要**说准**:是"没重新登录",不是"没登录"
        assert "没被换掉" in res.reason
        assert "退出当前账号" in res.reason

    def test_死_04_加上没_ssk_时也不早退(self, monkeypatch, tmp_path) -> None:
        """`04` 还在但 `webSsk` 没了 ⇒ 多半已被服务端作废 ⇒ 同样不能算成功。"""
        ctx = _FakeCtx([STALE, STALE], ["", ""])
        _patch(monkeypatch, ctx)
        assert L.wait_for_login(str(tmp_path), timeout_s=0.3, poll_s=0).ok is False

    def test_会话真的换过之后才算成功(self, monkeypatch, tmp_path) -> None:
        ctx = _FakeCtx([STALE, STALE, FRESH], ["", SSK_REAL])
        _patch(monkeypatch, ctx)
        res = L.wait_for_login(str(tmp_path), timeout_s=1.0, poll_s=0)
        assert res.ok is True
        assert res.cookies["web_session"] == FRESH["web_session"]     #: 拿到的是**新**的
        assert res.ssk_b64 == SSK_REAL
        assert res.ua == EDGE_UA                                      #: 指纹也一起存了
        assert ctx.closed is True


class TestHealthySessionStillShortCircuits:
    """档案本来就是好的 ⇒ 不该逼人再扫一次码。"""

    def test_04_加_ssk_就直接早退(self, monkeypatch, tmp_path) -> None:
        ctx = _FakeCtx([FRESH], [SSK_REAL])
        _patch(monkeypatch, ctx)
        res = L.wait_for_login(str(tmp_path), timeout_s=0.3, poll_s=0)
        assert res.ok is True
        assert res.waited_s == 0.0                    #: 没等 ⇒ 说明走了早退分支
        assert res.cookies["web_session"] == FRESH["web_session"]

    def test_早退时也把指纹一起带出来(self, monkeypatch, tmp_path) -> None:
        ctx = _FakeCtx([FRESH], [SSK_REAL])
        _patch(monkeypatch, ctx)
        res = L.wait_for_login(str(tmp_path), timeout_s=0.3, poll_s=0)
        assert res.ua == EDGE_UA
        assert res.client_hints["sec-ch-ua-platform"] == '"Windows"'
        assert res.egress_ip == "1.2.3.4"


class TestGuestWaitsForLogin:
    def test_游客档会一直等到换成_04(self, monkeypatch, tmp_path) -> None:
        ctx = _FakeCtx([GUEST, GUEST, FRESH], [SSK_REAL])
        _patch(monkeypatch, ctx)
        res = L.wait_for_login(str(tmp_path), timeout_s=1.0, poll_s=0)
        assert res.ok is True
        assert res.cookies["web_session"].startswith("04")
        assert res.ssk_b64 == SSK_REAL

    def test_游客档一直没登就超时(self, monkeypatch, tmp_path) -> None:
        ctx = _FakeCtx([GUEST, GUEST], ["", ""])
        _patch(monkeypatch, ctx)
        res = L.wait_for_login(str(tmp_path), timeout_s=0.3, poll_s=0)
        assert res.ok is False
        assert "游客档" in res.reason


class TestFingerprintCapture:
    """指纹必须**从浏览器抓**,不是编的。"""

    def test_无头_UA_会被洗成正常_Chrome(self, monkeypatch, tmp_path) -> None:
        headless = EDGE_UA.replace("Chrome/154", "HeadlessChrome/154")
        ctx = _FakeCtx([FRESH], [SSK_REAL], ua=headless)
        _patch(monkeypatch, ctx)
        res = L.wait_for_login(str(tmp_path), timeout_s=0.3, poll_s=0)
        #: 存档里**绝不能**留 `HeadlessChrome` —— 那等于自报"我是自动化"
        assert "Headless" not in res.ua
        assert "Edg/154.0.0.0" in res.ua

    def test_抓不到提示头时宁缺不编(self, monkeypatch, tmp_path) -> None:
        ctx = _FakeCtx([FRESH], [SSK_REAL], hints_raw=None)
        _patch(monkeypatch, ctx)
        res = L.wait_for_login(str(tmp_path), timeout_s=0.3, poll_s=0)
        assert res.client_hints == {}
