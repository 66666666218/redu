"""迅雷 captcha 续期单测(2026-10-02)。

这是**整条转存链的命门**:captcha 十几分钟就废,废了盘写操作全停;它靠"借网页版铸"
自愈。之前只有 `xunlei_transfer` 那一侧的重试被测过,**续期本身没有覆盖** —— 补上。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import json
import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db import models  # noqa: F401
from app.db.models import User
from app.services import cookie_store, xunlei_captcha as xc, xunlei_transfer as xt


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    db.add(User(id=1, username="u1", email="u1@b.c", password_hash="x", enabled=True))
    db.commit()
    yield db
    db.close()


@pytest.fixture(autouse=True)
def _wire(session, monkeypatch):
    """把 db 与"已配凭据"接好,并清掉冷却。"""
    monkeypatch.setattr("app.db.get_session_local", lambda: (lambda: session))
    monkeypatch.setattr(xc, "_last_minted", 0.0)
    monkeypatch.setattr(xt, "_credentials",
                        lambda settings=None: {"access_token": "old", "refresh_token": "r0"})
    # ⚠️ **必须 mock 掉"续期后立刻验一次刷新"那步**(2026-10-05 加的功能):
    # 它**会真发请求**到 auth.xunlei.com —— 不 mock 的话本文件的测试会变成联网测试
    # (实测单条从 ~0.1s 涨到 1.82s,而且依赖外网)。本仓的纪律是**测试离线**。
    monkeypatch.setattr(xt, "_refresh_access_token",
                        lambda rt, cid="": "A-VERIFIED-OK")


def _capture_written(monkeypatch) -> dict:
    box: dict = {}
    monkeypatch.setattr(cookie_store, "set_cookie",
                        lambda db, uid, plat, val: box.update(uid=uid, plat=plat,
                                                              val=json.loads(val)))
    return box


def test_refresh_mints_and_persists_all_three(monkeypatch) -> None:
    """铸成功后要**把三件套一起写回**(token + device_id + client_id)——

    ⚠️ 只存 token 是不行的:captcha 与 `device_id`/`client_id` **三者绑定**,
    换了 device 就 `captcha_invalid`(2026-10-02 实测踩过)。
    """
    monkeypatch.setattr(xc, "_mint_via_web", lambda url: {
        "token": "CK0-NEW", "device_id": "DEV1", "client_id": "CID1"})
    box = _capture_written(monkeypatch)

    assert xc.refresh(force=True) is True
    assert box["plat"] == "xunlei" and box["uid"] == 1
    assert box["val"]["captcha_token"] == "CK0-NEW"
    assert box["val"]["device_id"] == "DEV1" and box["val"]["client_id"] == "CID1"
    assert box["val"]["refresh_token"] == "r0"           # 原有凭据不能被覆盖掉


def test_refresh_keeps_rotated_tokens(monkeypatch) -> None:
    """网页若**自己刷新过** token(会轮换 refresh_token),必须一并读回写库 ——

    不回写 = 跑一次废一次(2026-10-02 就是这么把 refresh_token 用废的)。
    """
    monkeypatch.setattr(xc, "_mint_via_web", lambda url: {
        "token": "CK", "device_id": "D", "client_id": "C",
        "access_token": "A-NEW", "refresh_token": "R-NEW"})
    box = _capture_written(monkeypatch)

    assert xc.refresh(force=True) is True
    assert box["val"]["access_token"] == "A-NEW"
    assert box["val"]["refresh_token"] == "R-NEW"        # ← 轮换后的新的


def test_refresh_respects_cooldown(monkeypatch) -> None:
    """冷却期内**不再开浏览器** —— 连环失败时否则会把浏览器开成串。"""
    called: list[str] = []
    monkeypatch.setattr(xc, "_mint_via_web", lambda url: called.append(url) or {})
    monkeypatch.setattr(xc, "_last_minted", time.time())
    assert xc.refresh() is False and called == []
    xc.refresh(force=True)
    assert called == [xc._FALLBACK_SHARE]      # 库里没有群分享 → 用兜底链


def test_refresh_failure_is_reported_not_raised(monkeypatch) -> None:
    """铸失败(页面被要求登录等)→ 返回 False 并记下原因,**不抛**。"""
    monkeypatch.setattr(xc, "_mint_via_web", lambda url: {})
    assert xc.refresh(force=True) is False
    assert "没铸到" in xc.status()["last_error"]


def test_pick_share_url_prefers_existing_group_share(session, monkeypatch) -> None:
    """触发页优先用**库里已有的群分享链**(现成 20+ 条),没有才回落常量。"""
    from app.db.models import XunleiGroupShare

    session.add(XunleiGroupShare(user_id=1, group_id="g", share_id="s", title="t",
                                 origin_url="https://pan.xunlei.com/s/LIB?pwd=1"))
    session.commit()
    assert xc._pick_share_url() == "https://pan.xunlei.com/s/LIB?pwd=1"


# ------------------------------------------------ 2026-10-05:每天要重扫的根因

def test_expires_at_必须是页面那种_iso_串() -> None:
    """★ **"每天得重扫一次"的根因就在这个格式上**。

    `xunlei_captcha` 借网页铸 captcha 时,要把一个"新鲜的" `expires_at` 注入页面,
    好让网页**认为 token 还没过期、不要去兑换** —— 那次兑换**会轮换 refresh_token**。

    ⚠️ 原来注入的是 **整数 epoch**(`int(_jwt_exp(...))`),而页面用的是 ISO 串
    (实测库里读回的是 `2026-10-05T07:22:15.275Z`)。**格式对不上 ⇒ 页面照样去兑 ⇒
    refresh_token 被换掉而我们接不住 ⇒ 直连刷新 `invalid_grant` ⇒ 只能重扫。**
    实测时间线:15:20:41 captcha 续期 → **15:30 就 invalid_grant**;
    access_token 寿命实测 12 小时 ⇒ **一天一扫**。
    """
    import re

    s = xt.iso_expires_at(1759700000)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", s), \
        f"必须是页面那种 ISO-8601 + 毫秒 + Z,实际是 {s!r}"
    assert not s.isdigit(), "**不能是整数 epoch** —— 那正是被修掉的那个 bug"


def test_expires_at_按秒数也算得出来() -> None:
    """`_refresh_access_token` 写回时用的是 `expires_in`(秒),走同一条格式化。"""
    import re
    import time

    s = xt.iso_expires_at(seconds_from_now=43200)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", s)
    assert time.time() + 43000 < time.mktime(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")) + 8 * 3600


def test_注入给页面的_expires_at_是_iso_不是整数(monkeypatch) -> None:
    """★ **端到端钉住注入值**(不是在测那个工具函数,而是"注入的确实是它")。"""
    import re

    seen: dict = {}

    class _FakeReq:
        """⚠️ 要**真的触发回调**:`_mint_via_web` 里有个
        `for _ in range(25): if minted.get("token"): break; time.sleep(1)` ——
        不喂它一个 captcha,这条测试就白等 **25 秒**(实测,而本仓纪律是测试要快且离线)。
        """
        url = "https://pan.xunlei.com/drive/v1/files"
        headers = {"x-captcha-token": "CK", "x-device-id": "D", "x-client-id": "C"}

    class _FakePage:
        def __init__(self):
            self._cb = None

        def on(self, ev, cb):
            self._cb = cb

        def goto(self, *a, **k):
            if self._cb:
                self._cb(_FakeReq())      # 一进页面就给 captcha,别让上面那个循环空转

        def evaluate(self, *a, **k):
            return {}

    class _FakeCtx:
        def new_page(self): return _FakePage()

    class _FakeBrowser:
        def new_context(self, **k): return _FakeCtx()
        def close(self): pass

    class _Chromium:
        def launch(self, **k): return _FakeBrowser()

    class _FakePW:
        def __init__(self):
            self.chromium = _Chromium()      # ⚠️ 是**属性**不是方法:
                                             # 代码里写的是 `p.chromium.launch(...)`

    class _CM:
        def __enter__(self): return _FakePW()
        def __exit__(self, *a): return False

    import sys as _s
    import types as _t

    fake = _t.ModuleType("playwright.sync_api")
    fake.sync_playwright = lambda: _CM()
    monkeypatch.setitem(_s.modules, "playwright.sync_api", fake)

    monkeypatch.setattr(xt, "_credentials",
                        lambda settings=None: {"access_token": "a", "refresh_token": "r0",
                                               "client_id": "C", "device_id": "D"})
    monkeypatch.setattr(xt, "_access_token", lambda cred: "a")
    monkeypatch.setattr(xt, "_jwt_exp", lambda t: 1759700000)
    monkeypatch.setattr(xt, "_jwt_sub", lambda t: "1")
    # ⚠️ **`_pick_share_url` 也要 mock**:它会去打迅雷接口挑一条分享链 ——
    #    不 mock 的话这条测试从 0.04s 变成 25s(实测),而且依赖外网。
    #    本仓的纪律是**测试离线**。
    monkeypatch.setattr(xc, "_pick_share_url", lambda: "https://pan.xunlei.com/s/mock")
    # 抓注入值
    orig_eval = _FakePage.evaluate

    def _eval(self, js, arg=None):
        if isinstance(arg, list) and len(arg) == 2 and arg[0] in (
                "access_token", "refresh_token", "expires_at"):
            seen[arg[0]] = arg[1]
        return orig_eval(self, js, arg)

    monkeypatch.setattr(_FakePage, "evaluate", _eval)

    xc._mint_via_web("https://pan.xunlei.com/s/x")
    assert "expires_at" in seen, f"没注入 expires_at:{seen}"
    assert not str(seen["expires_at"]).isdigit(), (
        f"注入的是整数 epoch({seen['expires_at']!r})—— 页面要的是 ISO 串,"
        "这正是'每天要重扫'的根因")
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z",
                        str(seen["expires_at"]))


def test_续期后刷新失败要当场告警(monkeypatch) -> None:
    """★ **别等 12 小时**:网页兑换可能已经把 refresh_token 换掉而新值没落盘 ——
    那种情况下这枚凭据**已经死了**,但要等 access_token 到期才暴露,那时只能重扫。

    所以续期后**立刻**用一次刷新把状态钉死;失败就**当场告警**。
    """
    monkeypatch.setattr(xc, "_mint_via_web", lambda url: {
        "token": "CK", "device_id": "D", "client_id": "C"})
    _capture_written(monkeypatch)

    def _boom(rt, cid=""):
        raise RuntimeError("迅雷刷新 token 失败:{'error': 'invalid_grant'}")

    monkeypatch.setattr(xt, "_refresh_access_token", _boom)

    sent: list = []
    import app.services.alert_service as als
    monkeypatch.setattr(als, "notify_incident",
                        lambda *a, **k: sent.append(a[3]) or True)

    assert xc.refresh(force=True) is True          # 续期本身仍算成功(它确实铸到了)
    assert sent, "刷新验证失败却**没有告警** —— 那就会拖到 12 小时后才发现"
    assert any("重新扫码" in str(t) for t in sent), sent


def test_续期后刷新成功就不告警(monkeypatch) -> None:
    """反向:验证通过时**不该**打扰人(告警疲劳会让真警报被忽略)。"""
    monkeypatch.setattr(xc, "_mint_via_web", lambda url: {
        "token": "CK", "device_id": "D", "client_id": "C"})
    _capture_written(monkeypatch)
    sent: list = []
    import app.services.alert_service as als
    monkeypatch.setattr(als, "notify_incident", lambda *a, **k: sent.append(a[3]) or True)

    assert xc.refresh(force=True) is True
    assert not sent
