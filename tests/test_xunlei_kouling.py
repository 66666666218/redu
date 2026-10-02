"""迅雷**口令解析**单测(2026-10-02):口令 → 分享链 / 群 / 非口令。

样本取自真实接口返回(`associate_search` 的 `birdkey`),不是编的。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db import models  # noqa: F401
from app.db.models import User, XunleiResource
from app.services import xunlei_kouling as kk


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    db.add(User(id=1, username="admin", email="a@b.c", password_hash="x", enabled=True))
    db.commit()
    yield db
    db.close()


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


@pytest.fixture(autouse=True)
def _fake_auth(monkeypatch):
    """不让单测碰真实凭据。"""
    monkeypatch.setattr(kk, "_headers",
                        lambda: ({"Authorization": "Bearer x"}, "wdi10.test"))


def _patch_search(monkeypatch, action_url: str, type_: str = "share_page") -> None:
    import requests

    def fake_get(url, **kw):
        assert "associate_search" in url, url
        assert kw["params"]["guid"] == "wdi10.test", "guid 是必填,必须带上"
        return _Resp({"birdkey": {"content": kw["params"]["keyword"],
                                  "action_url": action_url, "type": type_},
                      "data": {"count": 0, "list": []}})

    monkeypatch.setattr(requests, "get", fake_get)


# ---------------------------------------------------------------- 解析

def test_resolve_share_page_returns_share_url_with_passcode(monkeypatch) -> None:
    """实测样本:「玩车不求人」→ `pan.xunlei.com/s/xxx?pwd=gcsk&from=BHO/paste/kouling`。"""
    _patch_search(monkeypatch,
                  "https://pan.xunlei.com/s/VOtw0rXU99xNQ-XD-0vtBexoA1?&pwd=gcsk&from=BHO/paste/kouling")
    out = kk.resolve("玩车不求人")
    assert out["kind"] == kk.KIND_SHARE
    assert out["share_url"] == "https://pan.xunlei.com/s/VOtw0rXU99xNQ-XD-0vtBexoA1"
    assert out["pass_code"] == "gcsk"
    assert out["group_id"] == ""


def test_resolve_group_invite_returns_group_id(monkeypatch) -> None:
    """实测样本:「三岁分享」→ 群邀请链(`type=cmd`),要取出 group_id 去加群。"""
    _patch_search(monkeypatch,
                  "xunleiapp://xunlei.com/webAdCheck?url=https%3A%2F%2Fsj-m-ssl.xunlei.com"
                  "%2Fgroup-invite%3Ffrom_uid%3D887920981%26group_id%3D1550069837"
                  "%26type%3Dcmd%26from%3Dqr", type_="")
    out = kk.resolve("三岁分享")
    assert out["kind"] == kk.KIND_GROUP
    assert out["group_id"] == "1550069837"
    assert out["share_url"] == ""


def test_resolve_non_kouling_and_blank_return_none(monkeypatch) -> None:
    """不是口令的词(`diplay`/`My Dearest`)type 为空、无链接 → 不该被当成资源。"""
    _patch_search(monkeypatch, "", type_="")
    assert kk.resolve("diplay")["kind"] == kk.KIND_NONE
    assert kk.resolve("   ")["kind"] == kk.KIND_NONE          # 空串直接短路,不发请求


def test_resolve_degrades_when_no_cred(monkeypatch) -> None:
    """没配凭据 → kind=none,不抛(它跑在抖音线索链上,不能拖垮整轮)。"""
    monkeypatch.setattr(kk, "_headers", lambda: None)
    assert kk.resolve("玩车不求人")["kind"] == kk.KIND_NONE


def test_resolve_many_dedupes_and_keeps_order(monkeypatch) -> None:
    """批量解析去重保序。"""
    _patch_search(monkeypatch, "https://pan.xunlei.com/s/ABC?&pwd=1")
    out = kk.resolve_many(["甲", "乙", "甲", "", "  "])
    assert [x["kouling"] for x in out] == ["甲", "乙"]


# ---------------------------------------------------------------- 入库

def test_ingest_saves_transferred_resource(session, monkeypatch) -> None:
    """kind=share:转存成功后写进 `xunlei_resources`(parent_name 标「口令解析」便于识别)。"""
    from app.services import xunlei_transfer as xt

    _patch_search(monkeypatch, "https://pan.xunlei.com/s/ABC?&pwd=1")
    monkeypatch.setattr(xt, "transfer_and_share",
                        lambda url, parent_id="", settings=None: {
                            "status": "ok", "share_url": "https://pan.xunlei.com/s/OUR?pwd=zz",
                            "code": "zz", "fid": "FID1"})
    out = kk.ingest(session, 1, "玩车不求人")
    assert out["status"] == "ok" and out["our_url"].endswith("pwd=zz")
    row = session.query(XunleiResource).one()
    assert row.fid == "FID1" and row.parent_name == "口令解析" and row.name == "玩车不求人"


def test_ingest_group_defers_to_group_collector(session, monkeypatch) -> None:
    """kind=group:加群后交给群采集轮,这里不重复实现转存。"""
    monkeypatch.setattr(kk, "join_group", lambda gid: {"status": "ok", "newly_joined": True})
    _patch_search(monkeypatch, "https://sj-m-ssl.xunlei.com/group-invite?group_id=1550069837")
    out = kk.ingest(session, 1, "三岁分享")
    assert out["status"] == "deferred" and out["group_id"] == "1550069837"
    assert session.query(XunleiResource).count() == 0


def test_ingest_not_kouling_is_explicit(session, monkeypatch) -> None:
    """不是口令 → 明确返回 not_kouling,不写库。"""
    _patch_search(monkeypatch, "")
    assert kk.ingest(session, 1, "随便一个词")["status"] == "not_kouling"
    assert session.query(XunleiResource).count() == 0


def test_ingest_skips_when_gate_blocks(session, monkeypatch) -> None:
    """闸门挡下时 `status=skipped` 且**不调用转存**(盘满/泛化大包只留痕,不写库)。"""
    from app.services import xunlei_group, xunlei_transfer as xt

    _patch_search(monkeypatch, "https://pan.xunlei.com/s/ABC?&pwd=1")
    monkeypatch.setattr(xunlei_group, "admit_transfer",
                        lambda name, cred=None, settings=None, ratio=None:
                        (False, "泛化大包(名字含合集/大全/最全…),体积不可控,只推链不搬", False))
    monkeypatch.setattr(xt, "transfer_and_share",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该调用转存")))
    out = kk.ingest(session, 1, "玩车不求人")
    assert out["status"] == "skipped" and "泛化大包" in out["message"]
    assert session.query(XunleiResource).count() == 0


def test_ingest_disk_full_does_not_mark_as_done(session, monkeypatch) -> None:
    """⚠️ 盘满时**不能写库** —— 否则会被 `known_koulings` 当成"已搬过"而永不重试。

    盘满是**可重试**状态:抖音作业每天跑,空间清出来那天自然会再搬一次。
    """
    from app.services import xunlei_group
    from app.services import xunlei_transfer as xt

    _patch_search(monkeypatch, "https://pan.xunlei.com/s/ABC?&pwd=1")
    monkeypatch.setattr(xunlei_group, "admit_transfer",
                        lambda name, cred=None, settings=None, ratio=None:
                        (False, "盘快满了(已用 126%,阈值 90%),先清理再搬", True))
    monkeypatch.setattr(xt, "transfer_and_share",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该调用转存")))
    out = kk.ingest(session, 1, "玩车不求人")
    assert out["status"] == "disk_full" and "盘快满了" in out["message"]
    assert session.query(XunleiResource).count() == 0
    assert kk.known_koulings(session, 1) == set()          # 没被记成"已搬过"
