"""微信读书 **App 侧**客户端单测(2026-10-05)。

⚠️ **全部不联网** —— App 接口是**唯一还能拿到精确阅读数**的路
(网页 `/web/mp/articles` 已被账号级拦截 `-2041`),所以它的解析与错误分支必须钉死:
错了就是"阅读数又变 0",而且**看不出是解析坏了还是真没数据**(本仓的老毛病)。

响应形状取自 2026-10-05 mitmproxy 的真实抓包:
`{"reviews":[{"reviewId":..., "review":{"mpInfo":{"title":..., "readNum":27, ...}}}]}`
"""
from __future__ import annotations

import pytest

from app.services.weread_app_client import (
    WereadAppAuthError, WereadAppClient, WereadAppError)


class _Resp:
    def __init__(self, payload, status=200):
        self._p, self.status_code = payload, status

    def json(self):
        if isinstance(self._p, Exception):
            raise ValueError("not json")
        return self._p


def _patch(monkeypatch, payload):
    import app.services.weread_app_client as m

    monkeypatch.setattr(m, "requests", _FakeRequests(payload), raising=False)


class _FakeRequests:
    """替身只提供 `get` —— 模块内是 `import requests` 局部引用,所以这样替得进去。"""

    def __init__(self, payload):
        self.payload = payload
        self.calls: list[dict] = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append({"url": url, "headers": headers or {}, "params": params or {}})
        return _Resp(self.payload)


def _payload(reviews):
    return {"reviews": [{"reviewId": "x", "review": {"mpInfo": mp}} for mp in reviews]}


# ---------------- 正常路径 ----------------

def test_能解析出精确阅读数(monkeypatch):
    _patch(monkeypatch, _payload([
        {"title": "甲", "readNum": 27, "likeNum": 3, "time": 1791164421, "originalId": "o1"},
        {"title": "乙", "readNum": 1151, "likeNum": 8, "time": 1791164400, "originalId": "o2"},
    ]))
    arts = WereadAppClient("tok", "439862397").articles("MP_WXS_1")
    assert [a["title"] for a in arts] == ["甲", "乙"]
    assert [a["read_num"] for a in arts] == [27, 1151]
    assert arts[0]["like_num"] == 3
    assert arts[0]["original_id"] == "o1"


def test_鉴权走_accessToken_头而不是_Authorization(monkeypatch):
    """**这条是抓包换来的事实**:App 用的是自定义头 `accessToken` + `vid`。

    ⚠️ 我一开始猜的是 `Authorization: Bearer`,服务端回 `-2010/-2012` ——
    没有这条断言,以后有人"顺手改回标准头"就会把整条路改死,而且**看起来像登录过期**。
    """
    import app.services.weread_app_client as m

    fake = _FakeRequests(_payload([]))
    monkeypatch.setattr(m, "requests", fake, raising=False)
    WereadAppClient("tok123", "999").articles("MP_WXS_1")
    h = fake.calls[0]["headers"]
    assert h["accessToken"] == "tok123"
    assert h["vid"] == "999"
    assert "Authorization" not in h


def test_缺标题的条目要跳过(monkeypatch):
    _patch(monkeypatch, _payload([{"readNum": 5}, {"title": "有标题", "readNum": 7}]))
    arts = WereadAppClient("t", "1").articles("MP_WXS_1")
    assert [a["title"] for a in arts] == ["有标题"]


def test_读数为空时给_0_而不是_None(monkeypatch):
    _patch(monkeypatch, _payload([{"title": "甲"}]))
    assert WereadAppClient("t", "1").articles("MP_WXS_1")[0]["read_num"] == 0


# ---------------- 错误分支 ----------------

def test_登录态错误要单独成一类(monkeypatch):
    """`-2010/-2012` 的处置是"**去重新取 token**",与网络抖动完全不同 ⇒ 必须能区分。"""
    for code in (-2010, -2012):
        _patch(monkeypatch, {"errcode": code, "errmsg": "x"})
        with pytest.raises(WereadAppAuthError):
            WereadAppClient("t", "1").articles("MP_WXS_1")


def test_其它业务码抛普通错误(monkeypatch):
    _patch(monkeypatch, {"errcode": -2041, "errmsg": "被拦"})
    with pytest.raises(WereadAppError) as ei:
        WereadAppClient("t", "1").articles("MP_WXS_1")
    assert not isinstance(ei.value, WereadAppAuthError)


def test_非_JSON_要抛错而不是当空(monkeypatch):
    """⚠️ 返回空列表会被上层读成"这个号今天没发文" —— 又一个假成功。"""
    _patch(monkeypatch, ValueError("x"))
    with pytest.raises(WereadAppError):
        WereadAppClient("t", "1").articles("MP_WXS_1")


def test_真正的空列表是合法结果(monkeypatch):
    """反向:服务端说"没文章"(errcode 缺省 + reviews 空)时**不是错误**,返回 []。"""
    _patch(monkeypatch, {"reviews": []})
    assert WereadAppClient("t", "1").articles("MP_WXS_1") == []


def test_缺凭据时构造就报_不是等请求失败():
    with pytest.raises(WereadAppAuthError):
        WereadAppClient("", "1")
    with pytest.raises(WereadAppAuthError):
        WereadAppClient("tok", "")


# ---------------- 兜底接线 ----------------

def test_weread_app_client_读不到凭据时返回_None_而不报错():
    """没配 App 凭据是**正常分支**(兜底跳过),不是失败 —— 不能因此停掉整轮监听。"""
    import app.services.wechat_monitor as wm
    from app.db.models import Base

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    try:
        assert wm._weread_app_client(db, 1) is None
    finally:
        db.close()


def test_凭据坏掉时要能说出来_不静默当没配(monkeypatch, caplog):
    """⚠️ 静默当成"没配"会让"token 坏了"表现成"阅读数又是 0" —— 本仓最怕的那种。"""
    import app.services.wechat_monitor as wm
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db.models import Base

    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    try:
        import app.services.cookie_store as cs

        monkeypatch.setattr(cs, "get_cookie", lambda *a, **k: "{ 不是合法 JSON")
        assert wm._weread_app_client(db, 1) is None
        assert any("App 侧凭据不可用" in r.getMessage() for r in caplog.records), \
            "凭据坏掉必须留下 WARNING —— 静默当'没配'会让 token 失效表现成'阅读数又是 0'"
    finally:
        db.close()


# ---------------- 自愈:token 轮换后自动重取 ----------------

def test_登录态失效会自愈并重试一次(monkeypatch):
    """⚠️ **这条是这条链能长期活着的前提**:`accessToken` 随 App 会话轮换,
    轮换后接口回 `-2010/-2012`、阅读数又断。靠人记得跑脚本是**靠不住**的
    (本仓已经吃过一次:断了一周没人知道)。
    """
    import app.services.weread_app_client as m

    calls: list[str] = []

    class FakeReq:
        def __init__(self):
            self.n = 0

        def get(self, url, headers=None, params=None, timeout=None):
            self.n += 1
            calls.append(headers["accessToken"])
            if self.n == 1:
                return _Resp({"errcode": -2012, "errmsg": "登录超时"})
            return _Resp(_payload([{"title": "自愈后的文", "readNum": 99}]))

    monkeypatch.setattr(m, "requests", FakeReq(), raising=False)
    cli = WereadAppClient("old_token", "1",
                          on_auth_error=lambda: ("new_token", "1"))
    arts = cli.articles("MP_WXS_1")
    assert cli.refreshed is True, "要留下'确实自愈过'的痕迹,否则事后查不出来"
    assert calls == ["old_token", "new_token"], "必须用**新** token 重试"
    assert arts[0]["read_num"] == 99


def test_重取不到时不许假装重试过(monkeypatch):
    """反向:回调返回 None(模拟器没开是**常态**)⇒ 照实抛,不吞。

    吞掉就会表现成"阅读数又是 0" —— 又一个假成功。
    """
    import app.services.weread_app_client as m

    class FakeReq:
        def get(self, *a, **k):
            return _Resp({"errcode": -2010, "errmsg": "用户不存在"})

    monkeypatch.setattr(m, "requests", FakeReq(), raising=False)
    cli = WereadAppClient("t", "1", on_auth_error=lambda: None)
    with pytest.raises(WereadAppAuthError):
        cli.articles("MP_WXS_1")
    assert cli.refreshed is False


def test_只重试一次_重取后还失败就不再试(monkeypatch):
    """取到新 token 仍失败 ⇒ 抛出去。**不能死循环**(每次重取都要开模拟器,代价很高)。"""
    import app.services.weread_app_client as m

    n = {"calls": 0}

    class FakeReq:
        def get(self, *a, **k):
            n["calls"] += 1
            return _Resp({"errcode": -2012, "errmsg": "登录超时"})

    monkeypatch.setattr(m, "requests", FakeReq(), raising=False)
    cli = WereadAppClient("t", "1", on_auth_error=lambda: ("t2", "1"))
    with pytest.raises(WereadAppAuthError):
        cli.articles("MP_WXS_1")
    assert n["calls"] == 2, "原始 1 次 + 重试 1 次,不该更多"
