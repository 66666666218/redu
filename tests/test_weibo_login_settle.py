"""「验活通过 ≠ 这枚 cookie 能用」—— 登录后要**复验一次**(2026-10-30)。不联网、不开浏览器。

## 守的是什么

2026-10-30 实测:微博扫码登录 12:35 成功、12:37 `m.weibo.cn/api/config` 回 `login=true`
并写进了库,12:39 同一枚 `SUB` 就被服务端**主动注销**(响应头 `SUB=deleted`)。
只验一次就返回的流程,写进库的是一枚**几十秒后就死**的凭据,
而下一次失败告警又会让人去重扫 —— 现象是「怎么扫都没用」。

⇒ `_wait_login` 在验活通过后必须**再等 `settle` 秒复验一次**,两次都 true 才算成功。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _mod():
    spec = importlib.util.spec_from_file_location(
        "scripts_weibo_login_settle", ROOT / "scripts" / "weibo_login.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_复验要等一会儿再问服务端(monkeypatch):
    m = _mod()
    slept: list[float] = []
    monkeypatch.setattr(m.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(m, "_verify", lambda ck: (True, "服务端确认已登录"))

    ok, why = m._verify_settled({"SUB": "x"}, 30)

    assert (ok, why) == (True, "服务端确认已登录")
    assert slept == [30], "必须先等 settle 秒再问 —— 立刻问等于没复验"


def test_复验失败要如实返回(monkeypatch):
    """登录后被风控销会话时,复验要回 False(写库那一步据此拒写)。"""
    m = _mod()
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    monkeypatch.setattr(m, "_verify", lambda ck: (False, "服务端说未登录(cookie 可能是旧的)"))

    ok, _why = m._verify_settled({"SUB": "x"}, 30)
    assert ok is False


@pytest.mark.parametrize("settle", [0, -5])
def test_关掉复验时不许睡负数(monkeypatch, settle):
    m = _mod()
    slept: list[float] = []
    monkeypatch.setattr(m.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(m, "_verify", lambda ck: (True, "ok"))

    m._verify_settled({"SUB": "x"}, settle)
    assert slept == [0], "`--settle 0` 也照样问一次,只是不睡"


def test_默认会复验(monkeypatch):
    """默认值必须是"复验",别让以后有人把默认值改成 0 悄悄退回旧行为。"""
    m = _mod()
    import inspect

    assert inspect.signature(m._wait_login).parameters["settle_s"].default >= 10


# ---------------------------------------------------------------- 入库只留核心项

def test_入库只留核心项():
    """**别把档案里全部 cookie 写进库**(2026-10-30)。

    把 38 项串成一条 `Cookie:` 头之后,`m.weibo.cn` 回
    `400 Request Header Or Cookie Too Large` —— 连 `/api/config` 都进不去,
    看起来像"又被限了",其实是**我们自己把请求头撑爆了**。
    """
    m = _mod()
    ck = {"SUB": "a", "SUBP": "b", "SRT": "c", "ALF": "d",
          "M_WEIBOCN_PARAMS": "e", "mweibo_short_token": "f", "LC": "g"}

    kept = m._select_for_store(ck)

    assert set(kept) == {"SUB", "SUBP", "SRT", "ALF"}
    assert all(k in m._CORE_COOKIES for k in kept)


def test_一项核心都没有时不许写空():
    """微博改了 cookie 名字也不能把登录态写空 —— 宁可大一点。"""
    m = _mod()
    ck = {"FOO": "1", "BAR": "2"}
    assert m._select_for_store(ck) == ck


def test_同名cookie以weibo_cn域为准():
    """`.weibo.cn` 那份才是能给 `m.weibo.cn` 用的;`.weibo.com` / passport 的值可能不同。"""
    m = _mod()
    jar = [
        {"name": "SUB", "value": "com", "domain": ".weibo.com"},
        {"name": "SUB", "value": "cn", "domain": ".weibo.cn"},
        {"name": "SUB", "value": "passport", "domain": ".passport.weibo.com"},
    ]
    #: 无论列表顺序如何,`.weibo.cn` 都要赢
    assert m._merge_cookies(jar)["SUB"] == "cn"
    assert m._merge_cookies(list(reversed(jar)))["SUB"] == "cn"
