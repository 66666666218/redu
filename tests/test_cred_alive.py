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


# ---------------------------------------------------------------------------
# 小红书:按失败类型分流(★ 拿真账号换来的分寸)
# ---------------------------------------------------------------------------


def _xhs_row(monkeypatch, exc, reheal_ok=True, retry_rows=None):
    from app.services import xhs_protocol_source as xp

    calls = {"probe": 0, "reheal": 0}

    def _search(*a, **k):
        calls["probe"] += 1
        if calls["probe"] == 1:
            raise exc
        return retry_rows if retry_rows is not None else []

    monkeypatch.setattr(xp, "search", _search)
    monkeypatch.setattr(ch, "_xhs_reheal",
                        lambda db: (calls.__setitem__("reheal", calls["reheal"] + 1)
                                    or "已从档案自动重导"))
    row = ch._xhs_protocol_row(object())
    return row, calls


def test_小红书_账号被限制时_绝对不许去重导(monkeypatch):
    """★★ **这是拿真账号换来的那条分寸**:`-104 账号被限制` 时**重导没有任何用**
    (凭据能认证,是账号没权限),而**反复重登/重试只会延长封锁**。
    把"被踢"和"被限制"混成一句"小红书挂了",下一个人就会去重登 —— 那正是最不该做的。"""
    class _R(_Boom):
        pass

    row, calls = _xhs_row(monkeypatch, _R("-104 账号被限制", kind="restricted"))
    assert calls["reheal"] == 0, "**账号级限制时不该去重导凭据**"
    assert row["level"] == "🔴"
    assert "重导凭据没有任何用" in row["detail"] and "延长封锁" in row["detail"]


def test_小红书_被踢下线时要自动重导并复验(monkeypatch):
    row, calls = _xhs_row(monkeypatch, _Boom("-101 无登录信息", kind="need_login"),
                          retry_rows=[{"url": "u"}] * 13)
    assert calls["reheal"] == 1, "被踢是凭据问题,该重导"
    assert row["level"] == "🟡" and "已自动接回" in row["detail"]


def test_小红书_重导后仍失败就报红(monkeypatch):
    row, calls = _xhs_row(monkeypatch, _Boom("-101 无登录信息", kind="need_login"),
                          retry_rows=[])
    assert calls["reheal"] == 1 and row["level"] == "🔴"
    assert "复验仍零结果" in row["detail"]


# ---------------------------------------------------------------------------
# 快手:专属体检行(它的失败以前埋在「名字型热度」那行里,看不见)
# ---------------------------------------------------------------------------


class _Rec:
    def __init__(self, status, detail):
        from datetime import datetime
        self.status, self.detail, self.started_at = status, detail, datetime.now()


class _RecDB:
    def __init__(self, rec):
        self._rec = rec

    def scalars(self, *a, **k):
        rec = self._rec

        class _R:
            def first(self_inner):
                return rec
        return _R()


def test_快手失败要单列报红并说要扫码():
    """★ 以前它的失败只出现在「名字型热度」那行的 `失败:kuaishou` 里 —— 一眼扫不见。"""
    row = ch._kuaishou_row(_RecDB(_Rec("success", "平台3 命中16 失败:kuaishou")))
    assert row["level"] == "🔴"
    assert "扫码重登" in row["detail"] and "cdp_ks_user_data_dir" in row["detail"]


def test_快手成功要报绿():
    row = ch._kuaishou_row(_RecDB(_Rec("success", "平台2 命中15")))
    assert row["level"] == "🟢" and "平台2 命中15" in row["detail"]


def test_快手整轮失败比如撞锁_要报黄而不是红():
    """⚠️ **区分"快手自己挂了"与"这一轮整体没跑成"**:后者的修法完全不同(不是去重登)。"""
    row = ch._kuaishou_row(_RecDB(_Rec("failed", "sqlite3.OperationalError: database is locked\n[SQL: INSERT…")))
    assert row["level"] == "🟡"
    assert "SQL:" not in row["detail"], "带 SQL 的整段异常会把报告撑爆,只该取第一行"


def test_快手没跑过要报黄():
    row = ch._kuaishou_row(_RecDB(None))
    assert row["level"] == "🟡" and "没有跑过" in row["detail"]


def test_抖音要求过验证_判红并给对修法(monkeypatch):
    """★ 2026-10-08 新增这一档。在此之前它落进"零结果,不作判定"那支 ⇒
    报告上既不红也不黄,而实际上**整条链是断的**(实测连空 8 小时)。

    ⚠️ 修法必须是「**人在浏览器里过一次验证**」,不能是「重导 cookie」——
    重导只是复制 cookie,**过不了验证**。给错的修法 = 让下一个人把同样无效的动作
    再试一遍(本仓最恨的"假装有自动化")。
    """
    from app.services import chain_health as ch
    from app.services import douyin_protocol_source as dps

    def _boom(*a, **k):
        raise dps.DouyinProtocolError(
            "抖音要求**过验证**(search_nil_info.search_nil_type=verify_check)",
            kind="verify", needs_human=True)

    monkeypatch.setattr(dps, "search", _boom)
    alive, why = ch._cred_alive("douyin", "ck", None)
    assert alive is False, "要求过验证是**断源**,不能报成「不作判定」"
    assert "过验证" in why and "重导 cookie" in why and "解决不了" in why
