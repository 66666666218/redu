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
