"""抖音/微博/知乎**凭据验活**测试(2026-10-08)。全部离线。

## 为什么这三家非加不可
它们的 Cookie 都是**人工粘进「Cookie 管理」页**的,而体检此前**连它们这一行都没有**
(那行注释还写着"抖音走匿名或档案,不在这张表里" —— **早过期了**)。
⇒ **凭据在库里,体检却不看它:死了没人知道。**

## 这里守三条
1. **2483(请先登录)要判红** —— 那是抖音唯一确定的"凭据死了"信号;
2. **网络抖动不许报成"凭据失效"** —— 两者的修法完全不同(等人重粘 vs 等一会儿);
3. **"零结果"不许判绿** —— 抖音限流时回的形状与"真没结果"**完全一样**(实测过),
   拿它当"活着"就是假绿。
"""
from __future__ import annotations

import pytest

from app.services import chain_health as ch


class _Boom(Exception):
    def __init__(self, msg, kind="api"):
        super().__init__(msg)
        self.kind = kind


@pytest.fixture
def db():
    """探针里用到 db 的只有抖音(读凭据),给它个占位就行。"""
    return object()


# ---------------------------------------------------------------------------
# 判据
# ---------------------------------------------------------------------------


def test_抖音_2483要判红(db, monkeypatch):
    from app.services import douyin_protocol_source as dps

    def _boom(*a, **k):
        raise _Boom("抖音要求先登录(2483)", kind="need_login")

    monkeypatch.setattr(dps, "search", _boom)
    alive, why = ch._cred_alive("douyin", "ck", db)
    assert alive is False and "2483" in why or "登录" in why


def test_抖音_有结果判绿(db, monkeypatch):
    from app.services import douyin_protocol_source as dps

    monkeypatch.setattr(dps, "search", lambda *a, **k: [{"url": "u"}] * 13)
    alive, why = ch._cred_alive("douyin", "ck", db)
    assert alive is True and "13" in why


def test_抖音_零结果既不判绿也不判红(db, monkeypatch):
    """★★ **这条是核心**:抖音限流时回 `status_code=0 + data:[]`,
    与"真没结果"**形状完全一样** ⇒ 拿它当"活着"就是假绿。
    但"2483 没出现"本身有信息,所以**如实说清证明了什么**。"""
    from app.services import douyin_protocol_source as dps

    monkeypatch.setattr(dps, "search", lambda *a, **k: [])
    alive, why = ch._cred_alive("douyin", "ck", db)
    assert alive is None, "零结果不该被判成绿"
    assert "2483" in why and "不作判定" in why, f"要把证明与没证明说清:{why}"


def test_微博_有结果判绿(monkeypatch):
    from app.services import weibo_search

    monkeypatch.setattr(weibo_search, "search", lambda *a, **k: [{}] * 15)
    alive, why = ch._cred_alive("weibo", "ck", None)
    assert alive is True and "15" in why


def test_知乎_有结果判绿(monkeypatch):
    from app.services import cross_accounts

    monkeypatch.setattr(cross_accounts, "_search_zhihu", lambda ck, kw, limit=5: [{}] * 4)
    alive, why = ch._cred_alive("zhihu", "ck", None)
    assert alive is True and "4" in why


def test_网络抖动不许报成凭据失效(db, monkeypatch):
    """★ 两者的修法完全不同:一个要人重新粘 Cookie,一个等一会儿就好。"""
    from app.services import douyin_protocol_source as dps

    def _net(*a, **k):
        raise _Boom("请求失败:ConnectionError", kind="network")

    monkeypatch.setattr(dps, "search", _net)
    alive, _ = ch._cred_alive("douyin", "ck", db)
    assert alive is None, "网络问题不能报成'凭据失效'"


# ---------------------------------------------------------------------------
# 接线守卫
# ---------------------------------------------------------------------------


def test_体检必须包含抖音和微博这两行():
    """★ **函数写得再好,没接上去等于没做** —— 这三家此前正是"库里凭据在、体检不看它"。"""
    import inspect

    src = inspect.getsource(ch.check_credentials)
    for plat in ('"douyin"', '"weibo"', '"zhihu"'):
        assert plat in src, f"check_credentials 里没有 {plat} 这一行"


def test_抖音失效要自动重导并复验(monkeypatch):
    """抖音是这三家里**唯一**能自动接回的(它有浏览器档案)。"""
    from app.services import cookie_store

    monkeypatch.setattr(cookie_store, "get_cookie", lambda *a, **k: "ck")
    # ⚠️ **按平台计数**,不是全局计数 —— `check_credentials` 是**按固定顺序**遍历的
    # (weread/zhihu/xunlei/…),全局计数器会被知乎那一次吃掉,于是抖音第一次就拿到 True。
    calls = {"douyin": 0}

    def _alive(plat, ck, db):
        if plat != "douyin":
            return True, "非本次目标"
        calls["douyin"] += 1
        return (False, "need_login") if calls["douyin"] == 1 else (True, "搜到 13 条")

    monkeypatch.setattr(ch, "_cred_alive", _alive)
    monkeypatch.setattr(ch, "_douyin_reheal", lambda db: "已从浏览器档案自动重导")
    row = [r for r in ch.check_credentials(_FakeDB()) if r["name"].startswith("抖音")][0]
    assert "自动重导" in row["detail"] and "复验通过" in row["detail"]
    assert row["level"] == "🟡", "接回来了就该降级为黄(不挡路了,但值得知道)"


def test_微博失效不许假装有自动重导(monkeypatch):
    """★ 微博的 Cookie 是**人粘的**,没有可导的来源 —— 别给它假装一条不存在的自动化。"""
    from app.services import cookie_store

    monkeypatch.setattr(cookie_store, "get_cookie", lambda *a, **k: "ck")
    monkeypatch.setattr(ch, "_cred_alive", lambda p, c, d: (False, "need_login"))
    row = [r for r in ch.check_credentials(_FakeDB()) if r["name"].startswith("微博")][0]
    assert row["level"] == "🔴"
    assert "人工重新复制" in row["detail"]


class _FakeDB:
    """`check_credentials` 只用它跑几个 SELECT —— 给最小替身即可。"""

    def scalars(self, *a, **k):
        class _R:
            def all(self_inner):
                from app.db.models import UserCookie
                from datetime import datetime
                return [UserCookie(user_id=1, platform=p, cookie="x",
                                   updated_at=datetime.now())
                        for p in ("douyin", "weibo", "zhihu")]
        return _R()


# ---------------------------------------------------------------------------
# 从档案导凭据(体检自动重导走的就是它)
# ---------------------------------------------------------------------------


def test_导出缺_sessionid_要说明而不是静默成功(monkeypatch):
    """★ **"导了个空壳还以为修好了"是最坏的形状** —— 缺登录态核心必须写进返回值。"""
    from app.services import douyin_cookie_export as dce

    monkeypatch.setattr(dce, "read_profile_cookies", lambda p=None: {"ttwid": "x", "a": "1"})
    monkeypatch.setattr("app.services.cookie_store.set_cookie", lambda *a, **k: None)
    msg = dce.export_from_profile(object(), 1)
    assert "缺" in msg and "sessionid" in msg, f"缺核心项要说出来:{msg}"


def test_导出齐全时报齐全(monkeypatch):
    """反面对照:登录态核心齐了要说「齐全」,别一律带着告警文案(那会变成噪音)。"""
    from app.services import douyin_cookie_export as dce

    monkeypatch.setattr(dce, "read_profile_cookies",
                        lambda p=None: {"sessionid": "s", "sessionid_ss": "s2", "ttwid": "t"})
    monkeypatch.setattr("app.services.cookie_store.set_cookie", lambda *a, **k: None)
    msg = dce.export_from_profile(object(), 1)
    assert "登录态核心齐全" in msg and "缺" not in msg, msg


def test_导出档案里没有凭据要抛(monkeypatch):
    """**不返回空字典** —— 那会伪装成"档案没登录",而真相可能是浏览器没起来。"""
    from app.services import douyin_cookie_export as dce

    monkeypatch.setattr(dce, "read_profile_cookies", lambda p=None: {})
    with pytest.raises(RuntimeError):
        dce.export_from_profile(object(), 1)


def test_体检里自动重导失败不能把体检带崩(monkeypatch):
    """★ 体检是每天唯一一次全局照面 —— 它自己崩了,当天所有信号一起消失。"""
    from app.services import cookie_store, douyin_cookie_export as dce

    monkeypatch.setattr(cookie_store, "get_cookie", lambda *a, **k: "ck")
    monkeypatch.setattr(ch, "_cred_alive", lambda p, c, d: (False, "need_login"))

    def _boom(*a, **k):
        raise RuntimeError("Edge 起不来")

    monkeypatch.setattr(dce, "export_from_profile", _boom)
    row = [r for r in ch.check_credentials(_FakeDB()) if r["name"].startswith("抖音")][0]
    assert row["level"] == "🔴"
    assert "自动重导失败" in row["detail"] and "RuntimeError" in row["detail"]
