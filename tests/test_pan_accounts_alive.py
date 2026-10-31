"""网盘账号池:**百度死号不能被"最空"排到第一**(2026-10-31)。不联网。

## 背景(当天实测)

`baidupan#2` / `#3` 的 cookie 早已失效(`/api/quota` 与 `/api/list` 都回 `errno=-6`),
但它们"最空"(各 ~5TiB) ⇒ 在池里**永远排第一** ⇒ `pan_retention` 的百度分支连续多轮
「列目录失败(errno=-6 /)」,而**主号明明是好的**(能列目录,只是超额 43 倍)。

⇒ 取号前先探活,死的跳过;全死则原样返回(否则会把"cookie 过期"误诊成"没配账号")。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest

from app.services import pan_accounts as pa


class _Resp:
    def __init__(self, errno):
        self._errno = errno

    def json(self):
        return {"errno": self._errno}


@pytest.fixture(autouse=True)
def _clear_cache():
    pa._alive_cache.clear()
    yield
    pa._alive_cache.clear()


def test_errno0算活着(monkeypatch):
    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp(0))
    assert pa._baidu_alive("ck1") is True


def test_errno负六算死了(monkeypatch):
    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp(-6))
    assert pa._baidu_alive("ck2") is False


def test_探活失败不判死(monkeypatch):
    """网络抖动不能把好号踢出池子 —— 「查不到」不是「死的证据」。"""
    def boom(*a, **k):
        raise RuntimeError("timeout")

    monkeypatch.setattr("requests.get", boom)
    assert pa._baidu_alive("ck3") is True


def test_探活有缓存(monkeypatch):
    calls = {"n": 0}

    def fake(*a, **k):
        calls["n"] += 1
        return _Resp(0)

    monkeypatch.setattr("requests.get", fake)
    pa._baidu_alive("ck4")
    pa._baidu_alive("ck4")
    assert calls["n"] == 1


def _fake_pool(monkeypatch, cookies):
    """把 `ordered`/`get_cookie` 换成固定池子。"""
    class _R:
        def __init__(self, label, key):
            self.label, self.cookie_key, self.user_id = label, key, 1

    rows = [_R(lb, lb) for lb, _ in cookies]
    monkeypatch.setattr(pa, "ordered", lambda session, platform, user_id=1: rows)
    monkeypatch.setattr("app.services.cookie_store.get_cookie",
                        lambda session, uid, key: dict(cookies).get(key, ""))


def test_死号被跳过_活号留下(monkeypatch):
    _fake_pool(monkeypatch, [("#2", "dead"), ("main", "alive")])
    monkeypatch.setattr(pa, "_baidu_alive", lambda ck: ck == "alive")

    out = pa.cookies_for(None, "baidupan")

    assert [lb for lb, _ in out] == ["main"]


def test_全死就原样返回(monkeypatch):
    """全死时不能回空 —— 那会把「cookie 过期」误诊成「没配账号」。"""
    _fake_pool(monkeypatch, [("#2", "dead1"), ("#3", "dead2")])
    monkeypatch.setattr(pa, "_baidu_alive", lambda ck: False)

    out = pa.cookies_for(None, "baidupan")

    assert len(out) == 2


def test_非百度不探活(monkeypatch):
    _fake_pool(monkeypatch, [("q2", "cq")])
    monkeypatch.setattr(pa, "_baidu_alive",
                        lambda ck: pytest.fail("夸克不该走百度探活"))

    assert pa.cookies_for(None, "quark") == [("q2", "cq")]
