"""过验证后**回写整套 Cookie**(2026-11-26):`tenant._persist_jar_after_autosolve`。

为什么单独测它:协议路用的那份 jar 和浏览器档案是**两份**。滑块是在浏览器里过去的,
种上的新 `sgcookie`/`tfstk` 不回写,协议路下一轮还是旧 jar、还是被挡 ——
**自愈只救了浏览器那条路,而协议路才是常态路**(实测 07:06~08:37 就是这样:
协议路 `SESSION_EXPIRED`、浏览器兜底一路 60~90 条)。

⚠️ 两个方向都要钉:
* 解过 ⇒ 回写,**且同一个 `solved_at` 只导一次**(否则每轮白读一次档案);
* 没解过 ⇒ **一次都不许导**(那是慢操作,而且会把好 jar 覆盖成浏览器档案的旧值);
* 导出来的 jar 不完整(缺 `unb`/`cookie2`)⇒ **不许覆盖**库里的好数据。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db import models  # noqa: F401
from app.db.models import User
from app.services import cookie_store
from app.services import tenant
from app.services import xianyu_verify

_GOOD_JAR = "unb=1; cookie2=c2; sgcookie=SG; tfstk=TF"
_BAD_JAR = "cna=abc; xlly_s=1"          # 缺 unb/cookie2 ⇒ 不完整


@pytest.fixture(autouse=True)
def _reset_state():
    xianyu_verify._LAST_SOLVED_AT = None
    tenant._JAR_PERSISTED_AT = None
    yield
    xianyu_verify._LAST_SOLVED_AT = None
    tenant._JAR_PERSISTED_AT = None


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(User(username="t", password_hash="x"))
    db.commit()
    cookie_store.set_cookie(db, 1, "goofish", "unb=OLD; cookie2=OLD")
    yield db
    db.close()


class _Client:
    def __init__(self, jar: str) -> None:
        self.jar, self.calls = jar, 0

    def export_cookie_jar(self) -> str:
        self.calls += 1
        return self.jar


def _settings():
    from config.settings import Settings

    return Settings(_env_file=None)


def _cookie(db) -> str:
    return cookie_store.get_cookie(db, 1, "goofish")


def test_writes_jar_after_solve(session) -> None:
    xianyu_verify.mark_solved()
    c = _Client(_GOOD_JAR)
    assert tenant._persist_jar_after_autosolve(session, 1, c) is True
    assert _cookie(session) == _GOOD_JAR


def test_skips_when_never_solved(session) -> None:
    """没解过就**一次都不碰**档案 —— 那是慢操作,还会把库里的好 jar 覆盖成档案旧值。"""
    c = _Client(_GOOD_JAR)
    assert tenant._persist_jar_after_autosolve(session, 1, c) is False
    assert c.calls == 0
    assert _cookie(session) == "unb=OLD; cookie2=OLD"


def test_same_solve_exports_only_once(session) -> None:
    xianyu_verify.mark_solved()
    c = _Client(_GOOD_JAR)
    assert tenant._persist_jar_after_autosolve(session, 1, c) is True
    assert tenant._persist_jar_after_autosolve(session, 1, c) is False
    assert c.calls == 1


def test_incomplete_jar_is_not_written(session) -> None:
    """缺 `unb`/`cookie2` = 读歪了/档案未登录 ⇒ **别覆盖**好数据。"""
    xianyu_verify.mark_solved()
    c = _Client(_BAD_JAR)
    assert tenant._persist_jar_after_autosolve(session, 1, c) is False
    assert _cookie(session) == "unb=OLD; cookie2=OLD"
    #: 没写成功就不该记「已回写」—— 下一次还有机会
    assert tenant._JAR_PERSISTED_AT is None


def test_client_without_exporter_is_tolerated(session) -> None:
    """协议路客户端没有 `export_cookie_jar` ⇒ 静默跳过,别 AttributeError 炸采集。"""
    xianyu_verify.mark_solved()

    class _Proto:
        def cookie_header(self) -> str:
            return ""

    assert tenant._persist_jar_after_autosolve(session, 1, _Proto()) is False
    assert _cookie(session) == "unb=OLD; cookie2=OLD"


def test_exporter_exception_is_swallowed(session) -> None:
    xianyu_verify.mark_solved()

    class _Boom:
        def export_cookie_jar(self) -> str:
            raise RuntimeError("浏览器没了")

    assert tenant._persist_jar_after_autosolve(session, 1, _Boom()) is False
    assert _cookie(session) == "unb=OLD; cookie2=OLD"


# ------------------------------------------------- 回落路径(2026-11-26 补)
def test_fallback_path_persists_jar(monkeypatch) -> None:
    """★ **浏览器兜底跑通了 ⇒ 顺手把那份 jar 导给协议路**。

    不能只靠「过完验证才回写」:实测 07:06~08:37 **连续 10 轮** `[浏览器兜底]`
    (每轮 60~90 条、档案一直是好的),协议路却每轮 `SESSION_EXPIRED`
    —— **兜底把问题兜住了,也把问题藏住了**。这条用例钉住"兜底成功也要同步 jar"。
    """
    from app.services import xianyu as xy

    boom = xy.XianyuCookieExpired("SESSION_EXPIRED")
    calls: list[str] = []

    def _collect(settings, client, start_offset=0, stats=None):
        calls.append(getattr(client, "kind", "?"))
        if calls[-1] == "协议":
            raise boom
        return [{"item_id": "i1", "title": "PS教程"}]

    monkeypatch.setattr(xy, "collect_hot", _collect)

    class _Proto:
        kind = "协议"

        def cookie_header(self) -> str:
            return ""

    class _Browser:
        kind = "浏览器"

        def export_cookie_jar(self) -> str:
            return _GOOD_JAR

    import app.services.xianyu_browser as xb
    monkeypatch.setattr(xb, "get_client", lambda s: _Browser())

    from app.services import tenant as tn

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(User(username="t", password_hash="x"))
    db.commit()
    cookie_store.set_cookie(db, 1, "goofish", "unb=OLD; cookie2=OLD")
    try:
        hot, client, source = tn._collect_hot_with_fallback(
            _settings(), _Proto(), "协议", browser_allowed=True, start_offset=0, stats={})
        assert source == "浏览器兜底" and hot
        #: 回落成功后由调用方(本用例模拟)导出 —— 真正的接线在 `run_xianyu` 里
        assert tn._persist_jar_after_fallback(db, 1, client) is True
        assert cookie_store.get_cookie(db, 1, "goofish") == _GOOD_JAR
    finally:
        db.close()


def test_incomplete_fallback_jar_not_written() -> None:
    """回落导出的 jar 若不完整,**照样不许覆盖**(与过验证那条路同一个闸)。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(User(username="t", password_hash="x"))
    db.commit()
    cookie_store.set_cookie(db, 1, "goofish", "unb=OLD; cookie2=OLD")
    try:
        assert tenant._persist_jar_after_fallback(db, 1, _Client(_BAD_JAR)) is False
        assert cookie_store.get_cookie(db, 1, "goofish") == "unb=OLD; cookie2=OLD"
    finally:
        db.close()
