"""微信读书 **App 凭据定时续期**测试(2026-10-08)。全部离线。

## 为什么值得单独一个文件
App 凭据原来**没有任何定时续期**,只靠监听轮里"用到才自救"——而那条自救路径挂在
**App 兜底**上(只在「网页列表没拿到 且 该号在本轮轮转窗口内」才走得到)。
事实是(查 logger 名查出来的,不是猜):`app.services.weread_app_token` 的日志
**自 2026-10-06 起一次都没出现过**,而 token 早在 10-07 15:55 就失效了
⇒ 阅读数从 10-07 15:59 之后**一篇都没拿到**,卡片「阅读数」全是 `—`。

这个文件守两件事:
1. `weread_refresh_tick` **确实调了** App 续期(接线守卫 —— 光有函数没接上等于没做);
2. App 续期**失败不许把整轮续期带崩、也不许占用"网页续期失败"那面旗子**
   (两者严重程度不同:网页挂了**断源**,App 挂了只是**阅读数**退化)。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db import models  # noqa: F401
from app.db.models import User

# ⚠️ **必须先经门面(`wechat_monitor`)导入** —— `_source` 与它是互相 import 的,
# 直接 `from app.services.wechat import _source` 会撞 `ImportError: partially initialized
# module`(本仓反复踩的那个循环导入)。
from app.services import wechat_monitor  # noqa: F401
from app.services.wechat import _source as src


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(User(id=1, username="u", password_hash="x", enabled=True))
    db.commit()
    yield db
    db.close()


@pytest.fixture
def wat(monkeypatch):
    """把 `weread_app_token` 换成假件,并可指定它的行为。"""
    from app.services import weread_app_token

    state = {"has": True, "out": {"ok": True, "accessToken": "t", "vid": "1"},
             "raise": None, "calls": 0}

    def _load(_s, _u):
        return {"accessToken": "old", "vid": "1"} if state["has"] else None

    def _refresh(_s, _u, **kw):
        state["calls"] += 1
        if state["raise"]:
            raise state["raise"]
        return dict(state["out"])

    monkeypatch.setattr(weread_app_token, "load", _load)
    monkeypatch.setattr(weread_app_token, "refresh_with_wake", _refresh)
    return state


# ---------------------------------------------------------------------------
# _renew_weread_app 本身
# ---------------------------------------------------------------------------


def test_续期成功返回_True(session, wat):
    assert src._renew_weread_app(session, 1) is True
    assert wat["calls"] == 1


def test_没配_App_凭据是正常分支_不调用也不报错(session, wat):
    """⚠️ 没配过 App 凭据**不是失败** —— 不能把它记成错误(那会变成每天的假告警)。"""
    wat["has"] = False
    assert src._renew_weread_app(session, 1) is False
    assert wat["calls"] == 0, "没凭据就不该去唤醒 App"


def test_模拟器没开_返回_False_但不抛(session, wat):
    """★ 模拟器没开是**常态**(那台机器不是一直开着)⇒ 只记日志,别把整轮续期带崩。"""
    wat["raise"] = RuntimeError("没有已连接的模拟器")
    assert src._renew_weread_app(session, 1) is False


def test_唤醒后仍无效_返回_False(session, wat):
    wat["out"] = {"ok": False, "reason": "-2012 登录超时"}
    assert src._renew_weread_app(session, 1) is False


def test_App_续期失败不推飞书(session, wat, monkeypatch):
    """★ 后果的**量级不同**:网页挂了**断源**,App 挂了只是**阅读数**退化。
    给它推飞书只会变成噪音(而噪音最终会被无视 —— 本仓反复讲过的教训)。"""
    from app.services import alert_service

    pushed: list = []
    monkeypatch.setattr(alert_service, "notify_incident",
                        lambda *a, **k: pushed.append((a, k)) or False)
    wat["out"] = {"ok": False, "reason": "模拟器没开"}
    src._renew_weread_app(session, 1)
    assert pushed == [], f"App 续期失败不该推飞书,实际推了 {pushed}"


# ---------------------------------------------------------------------------
# 接线守卫:光有函数、没接上 = 没做
# ---------------------------------------------------------------------------


def test_定时续期作业必须同时续_App(monkeypatch, session):
    """★★ **这条才是重点**:`weread_refresh_tick` 原来**只管网页版** ——
    函数写得再好,没接上去,阅读数照样会像 10-07 那样整片消失。"""
    from app.services import wechat_monitor

    called: list[int] = []
    monkeypatch.setattr(wechat_monitor, "refresh_weread_cookie",
                        lambda db, uid, **kw: {"status": "success"})
    monkeypatch.setattr(src, "_renew_weread_app",
                        lambda db, uid: called.append(uid) or True)
    monkeypatch.setattr("app.db.get_session_local", lambda: (lambda: session))
    src.weread_refresh_tick()
    assert called == [1], f"续期作业没有续 App 凭据(实际调用 {called})"


# ---------------------------------------------------------------------------
# 体检里那一行(★ 让"静默退化"变得看得见)
# ---------------------------------------------------------------------------


def _patch_app_blob(monkeypatch, blob):
    from app.services import weread_app_token as wat

    monkeypatch.setattr(wat, "load", lambda _s, _u: blob)


def _row():
    from app.services import chain_health as ch

    return ch._weread_app_row()


def test_体检_没配_App_凭据要黄(monkeypatch, session):
    """★ 没配是**黄**不是绿:阅读数会一直是 `—`,而报告必须说出来。"""
    _patch_app_blob(monkeypatch, None)
    monkeypatch.setattr("app.db.get_session_local", lambda: (lambda: session))
    r = _row()
    assert r["level"] == "🟡" and "未配置" in r["detail"]


def test_体检_刚续过是绿(monkeypatch, session):
    from datetime import datetime

    _patch_app_blob(monkeypatch, {"accessToken": "t", "vid": "1",
                                  "pulled_at": datetime.now().isoformat(timespec="seconds")})
    monkeypatch.setattr("app.db.get_session_local", lambda: (lambda: session))
    assert _row()["level"] == "🟢"


def test_体检_超过两个周期要黄_超过四个要红(monkeypatch, session):
    """★ 这就是 10-07 那次事故的形状:凭据早就断了两天,而报告一直是绿的。"""
    from datetime import datetime, timedelta

    monkeypatch.setattr("app.db.get_session_local", lambda: (lambda: session))
    for hours, want in ((13, "🟡"), (30, "🔴")):        # 周期 6h ⇒ 2 个=12h、4 个=24h
        _patch_app_blob(monkeypatch, {
            "accessToken": "t", "vid": "1",
            "pulled_at": (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")})
        r = _row()
        assert r["level"] == want, f"{hours}h 前续过应当是 {want},实际 {r['level']}:{r['detail']}"


def test_体检_pulled_at_坏掉要黄而不是崩(monkeypatch, session):
    _patch_app_blob(monkeypatch, {"accessToken": "t", "vid": "1", "pulled_at": "不是时间"})
    monkeypatch.setattr("app.db.get_session_local", lambda: (lambda: session))
    r = _row()
    assert r["level"] == "🟡" and "pulled_at" in r["detail"]
