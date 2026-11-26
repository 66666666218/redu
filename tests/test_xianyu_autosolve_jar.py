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
