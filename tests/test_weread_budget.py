"""微信读书**额度**的单一事实来源 + 跨路径共享熔断(2026-10-05)。

⚠️ 这个文件的重点不是"函数对不对",而是**"以后新写的路绕不过去"**:
本仓反复出现的不是"某个 bug 没修",而是**同一个坑在 A 链修了、B 链没修**
(原话:"同一个坑在两条链上,只修了一条")。额度就是最典型的那个坑 ——
判据住在 `_listen.py` 里,同步链嫌重不 import,于是它**从来没判过额度**。

所以最后一条测试是**源码级守卫**:凡是调用微信读书列表的地方,必须经统一入口。
"""
from __future__ import annotations

import os
import re

import pytest

from app.services import weread_budget as wb


# ---------------- 判据 ----------------

@pytest.mark.parametrize("code", ["-2014", "-2041", "-10100"])
def test_额度类码要认出(code):
    assert wb.is_quota_error(RuntimeError(f"微信读书接口返回错误({code}):x"))


@pytest.mark.parametrize("code", ["-2012", "-2010"])
def test_登录态码是另一类(code):
    """⚠️ **必须分得开**:额度类要"冷静一会儿",登录态要"去重新取凭据/续期" ——
    处置完全相反。合成一类就会出现"token 过期了却在那儿等冷静期"这种荒唐事。
    """
    assert wb.is_auth_error(RuntimeError(f"({code}):x"))
    assert not wb.is_quota_error(RuntimeError(f"({code}):x"))


def test_普通错误两边都不算():
    exc = RuntimeError("ConnectionError: 网络不通")
    assert not wb.is_quota_error(exc) and not wb.is_auth_error(exc)


# ---------------- 共享熔断 ----------------

@pytest.fixture()
def db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db.models import Base

    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()


def test_被挡之后连请求都不发(db):
    """★ 这是"抢额度"的核心修法:**熔断中根本不发请求**。

    以前各条路各写熔断,监听合了闸**同步照打** —— 发出去的每一次失败
    都会把风控**打得更深**(本仓原话:"再问也不给,还会把风控加深")。
    """
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("错误(-2041)")

    with pytest.raises(RuntimeError):     # 第一次**发出去**了,被服务端拒
        wb.call(db, 1, boom, what="t")
    assert calls == [1], "第一次该真发出去"

    def should_not_run():
        calls.append("不该发生")
        return "x"
    with pytest.raises(wb.Blocked):
        wb.call(db, 1, should_not_run, what="t")
    assert calls == [1], "熔断期内**不允许再把请求发出去**"


def test_被挡要记全路径共享的状态(db):
    with pytest.raises(RuntimeError):
        wb.call(db, 1, lambda: (_ for _ in ()).throw(RuntimeError("(-2041)")), what="t")
    assert wb.blocked_until(db, 1) is not None
    assert wb.snapshot(db, 1)["web_list"]["blocked"] is True


def test_额度错照实抛_不吞成空(db):
    """★ 母题:吞掉就变成"这次没有数据",而真相是"被挡了" —— 假成功。"""
    with pytest.raises(RuntimeError):
        wb.call(db, 1, lambda: (_ for _ in ()).throw(RuntimeError("(-2041)")), what="t")


def test_登录态错不记额度熔断(db):
    """token 过期 ≠ 额度被挡;**等冷静期等不出 token**,处置完全不同。"""
    with pytest.raises(RuntimeError):
        wb.call(db, 1, lambda: (_ for _ in ()).throw(RuntimeError("(-2012)")), what="t")
    assert wb.blocked_until(db, 1) is None, "登录态问题不该触发额度熔断"


def test_两条通道分开记账(db):
    """★ 网页被拦不该连带停 App —— App 是另一套鉴权与配额,**网页被挡时正是它该顶上**。"""
    with pytest.raises(RuntimeError):
        wb.call(db, 1, lambda: (_ for _ in ()).throw(RuntimeError("(-2041)")),
                what="web", scope=wb.SCOPE_WEB_LIST)
    assert wb.blocked_until(db, 1, wb.SCOPE_WEB_LIST) is not None
    assert wb.blocked_until(db, 1, wb.SCOPE_APP_LIST) is None, "App 通道必须不受影响"
    # App 通道仍然可调
    assert wb.call(db, 1, lambda: "ok", scope=wb.SCOPE_APP_LIST) == "ok"


def test_续期成功要能解熔断(db):
    """新会话 = 新额度账。不解的话,"刚换到好会话却因为旧账被停半小时"。"""
    with pytest.raises(RuntimeError):
        wb.call(db, 1, lambda: (_ for _ in ()).throw(RuntimeError("(-2041)")), what="t")
    wb.clear(db, 1)
    assert wb.blocked_until(db, 1) is None
    assert wb.call(db, 1, lambda: "ok") == "ok"


# ---------------- 源码级守卫(防"同一个坑在另一条链上再踩一次") ----------------

def _wechat_pkg_files() -> list[str]:
    root = os.path.join("app", "services", "wechat")
    out = []
    for f in sorted(os.listdir(root)):
        if f.endswith(".py"):
            out.append(os.path.join(root, f))
    return out


def test_凡是打微信读书列表的地方都要经统一额度入口():
    """★ **防复发守卫**:这条测试是这次改造**真正的交付物**。

    背景:判据原来住在 `_listen.py`,同步链嫌重就没 import ⇒ 它**从来没判过额度**,
    监听被挡时它照打不误。**同一个坑在两条链上,只修了一条** —— 本仓的老毛病。

    规则:任何调用 `mp_articles(` 的微信读书侧模块,必须出现 `weread_budget.call(`。
    (排除:定义 `mp_articles` 自己的后端客户端 —— WeRSS/Wemp/读书平台,它们不是微信读书额度。)
    """
    bad = []
    for path in _wechat_pkg_files():
        src = open(path, encoding="utf-8", errors="replace").read()
        # **定义 `mp_articles` 的文件 = 后端或分发器**(WeRSS/Wemp/读书平台/多源分发),
        # 它们不是"微信读书额度"这条链;只有**调用**它的文件才该走统一入口。
        # ⚠️ 我第一版按文件名白名单排除,结果 `_source.py`(那里是 MultiSourceClient
        # 的定义)被误抓 —— **判据要落在"定义还是调用"这个事实上,不是文件名**。
        defines = re.search(r"def\s+mp_articles\s*\(", src) is not None
        if not defines and ".mp_articles(" in src and "weread_budget.call(" not in src:
            bad.append(path)
    assert not bad, (
        "这些地方在打微信读书列表却没经统一额度入口 —— 会脱离跨路径熔断,"
        f"重演「监听被挡、别的链照打」那次:{bad}"
    )


# ---------------- 半开探测(2026-10-14):冷静期内"有限地"试一发 ----------------
#
# 为什么加:冷静期是**按作用域全局**的(一次 -2041 ⇒ 该作用域 30 分钟不发请求),
# 而 App 列表恰好是**网页关窗时唯一能出精确读数**的那条 ⇒ 一次失败把整轮(实测 37 个号)
# 的读数全吃掉。这类挡又是**时限性**的(当天已证会自愈)⇒ 等满 30 分钟纯属白等。


@pytest.fixture
def session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db import models  # noqa: F401
    from app.db.database import Base

    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)()
    yield db
    db.close()


def _call(session, fn, **kw):
    return wb.call(session, 1, fn, what="app/book/articles", scope=wb.SCOPE_APP_LIST, **kw)


class TestHalfOpenProbe:
    def test_默认不探测_行为与从前逐字一致(self, session) -> None:
        """⚠️ 没显式要求探测的路径,**熔断中一律不发**(保护不能被顺手削弱)。"""
        wb.note_blocked(session, 1, why="x:-2041", scope=wb.SCOPE_APP_LIST)
        calls: list[int] = []
        with pytest.raises(wb.Blocked):
            _call(session, lambda: calls.append(1) or [])
        assert calls == [], "熔断中不该发请求"

    def test_要求探测且够久_真发一次_成功即解除熔断(self, session) -> None:
        """★ 核心:熔断期内允许探一发;**成功 ⇒ 立刻解除**(不必等满冷静期)。"""
        wb.note_blocked(session, 1, why="x:-2041", scope=wb.SCOPE_APP_LIST)
        assert wb.blocked_until(session, 1, wb.SCOPE_APP_LIST) is not None
        calls: list[int] = []
        out = _call(session, lambda: calls.append(1) or ["ok"], allow_probe=True)
        assert out == ["ok"] and calls == [1], "该探这一发"
        assert wb.blocked_until(session, 1, wb.SCOPE_APP_LIST) is None, "探测成功应解除熔断"

    def test_探测间隔内不再发(self, session) -> None:
        """⚠️ 频次由 `PROBE_MIN` 限着 —— 是"每 10 分钟一发",不是放开了打(风控会更深)。"""
        wb.note_blocked(session, 1, why="x:-2041", scope=wb.SCOPE_APP_LIST)
        calls: list[int] = []
        _call(session, lambda: calls.append(1) or [], allow_probe=True)      # 第一次探
        wb.note_blocked(session, 1, why="x:-2041", scope=wb.SCOPE_APP_LIST)  # 又被挡(重新熔断)
        with pytest.raises(wb.Blocked):
            _call(session, lambda: calls.append(2) or [], allow_probe=True)  # 间隔内 → 不发
        assert calls == [1], f"间隔内不该再探:{calls}"

    def test_探测仍被挡_熔断继续(self, session) -> None:
        """探测失败(仍 -2041)⇒ **熔断继续**,而且探测时间已经推进(不会变成连发)。"""
        wb.note_blocked(session, 1, why="x:-2041", scope=wb.SCOPE_APP_LIST)
        def _boom():
            raise RuntimeError("微信读书错误 code=-2041:列表预算耗尽")
        with pytest.raises(RuntimeError):
            _call(session, _boom, allow_probe=True)
        assert wb.blocked_until(session, 1, wb.SCOPE_APP_LIST) is not None, "仍被挡就该继续冷静"
        with pytest.raises(wb.Blocked):
            _call(session, lambda: [], allow_probe=True)   # 间隔内不再探

    def test_连续探测失败要指数退避_不能把熔断永久续上(self, session, monkeypatch) -> None:
        """★ 2026-10-19:App 路整天 `app=0 / app_blocked=48~88` 的直接原因之一。

        `PROBE_MIN=10` + `DEFAULT_BLOCK_MIN=30` ⇒ 每次探针失败都把熔断**重置成 30 分钟**,
        而 10 分钟后又能探 ⇒ **熔断永远不自然到期**。连续失败必须退避到 60/120/…
        """
        from datetime import datetime

        monkeypatch.setattr(wb, "_probe_due", lambda *a, **k: True)
        wb.note_blocked(session, 1, why="x:-2041", scope=wb.SCOPE_APP_LIST)

        def _boom():
            raise RuntimeError("微信读书错误 code=-2041:列表预算耗尽")

        with pytest.raises(RuntimeError):
            _call(session, _boom, allow_probe=True)
        first = wb.blocked_until(session, 1, wb.SCOPE_APP_LIST)
        first_min = (first - datetime.now()).total_seconds() / 60
        assert 25 <= first_min <= 35, f"第一次探测失败仍应是 30 分钟,实际 {first_min:.1f}"

        with pytest.raises(RuntimeError):
            _call(session, _boom, allow_probe=True)
        second = wb.blocked_until(session, 1, wb.SCOPE_APP_LIST)
        second_min = (second - datetime.now()).total_seconds() / 60
        assert second_min > 55, f"第二次探测失败应退避到 60 分钟,实际 {second_min:.1f}"
