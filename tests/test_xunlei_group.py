"""迅雷**群组**采集单测(2026-10-02):消息解析、去重、采集/转存两步。

样本取自**真实接口返回**(群 1550069837「三岁分享」/ 1543650596「高山3️⃣」),不是编的。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db import models  # noqa: F401
from app.db.models import User, XunleiGroupShare
from app.services import xunlei_group as xg


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    # autoflush=False **与生产一致**(app/db/database.py 就是这么配的)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    db.add(User(id=1, username="admin", email="a@b.c", password_hash="x", enabled=True))
    db.commit()
    yield db
    db.close()


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """默认把所有真实 HTTP 打死;需要的用例自己再 patch 具体函数。"""
    monkeypatch.setattr(xg, "_headers", lambda: {"Authorization": "Bearer x"})


def _rec(rid: int, sender: int, content) -> dict:
    """造一条群消息(`content` 真实接口里是 JSON 字符串)。"""
    return {"id": rid, "group_id": 1550069837, "sender": sender,
            "content": content if isinstance(content, str) else json.dumps(content),
            "created_at": 1790916455}


# ---------------------------------------------------------------- 解析

def test_extract_shares_reads_type7_and_type15_and_skips_noise() -> None:
    """type 7 分享卡有现成 share_url;type 15 群文件库卡按 `share_id` 拼链;
    公告(type 11)/引导语(type 14)/纯文本一律跳过。"""
    records = [
        _rec(1, 887920981, {"type": 11, "data": {"announce": "欢迎加入群聊"}}),      # 公告
        _rec(2, 887920981, {"type": 14, "data": {"cmd": "三岁分享", "share_links": []}}),
        _rec(3, 887920981, "纯文本消息"),                                            # 无 type
        _rec(4, 887920981, {"type": 7, "data": {                                     # 分享卡
            "share_url": "http://pan.xunlei.com/s/VP2rVszEvka7-8jWT0PxdxolA1",
            "share_id": "VP2rVszEvka7-8jWT0PxdxolA1", "title": "手机警报器（警笛模拟器）2.0版",
            "kind": "drive#folder"}}),
        _rec(5, 1539146689, {"data": {"folders": [{"folder_name": "【全网最齐】游戏软件资源合集",
                                                   "share_id": "VP2qy26mXoC-KIE_MpqII5_mA1",
                                                   "pass_code": "", "files": []}]},
                              "type": 15}),   # ⚠️ 真实报文里 type 排在 data **后面**
    ]
    out = xg.extract_shares(records, "1550069837", "三岁分享")
    assert [s["share_id"] for s in out] == ["VP2rVszEvka7-8jWT0PxdxolA1",
                                            "VP2qy26mXoC-KIE_MpqII5_mA1"]
    first = out[0]
    assert first["title"] == "手机警报器（警笛模拟器）2.0版"
    assert first["origin_url"] == "http://pan.xunlei.com/s/VP2rVszEvka7-8jWT0PxdxolA1"
    assert first["group_name"] == "三岁分享" and first["message_id"] == "4"
    assert first["msg_time"] is not None
    # type 15 没给 share_url,要自己拼(下载端认这个格式)
    assert out[1]["origin_url"] == "https://pan.xunlei.com/s/VP2qy26mXoC-KIE_MpqII5_mA1"
    assert out[1]["title"] == "【全网最齐】游戏软件资源合集"


def test_extract_shares_dedupes_same_share_reposted() -> None:
    """同一条分享会被「群文件库更新卡」反复播报 —— 按 share_id 去重,否则会重复转存。"""
    same = {"type": 7, "data": {"share_id": "SAME", "share_url": "http://pan.xunlei.com/s/SAME",
                                "title": "警笛模拟器"}}
    out = xg.extract_shares([_rec(1, 1, same), _rec(2, 1, dict(same))], "g")
    assert len(out) == 1


def test_extract_shares_ignores_malformed_content() -> None:
    """`content` 不是合法 JSON 时不能炸(接口偶尔回半截)。"""
    bad = [{"id": 1, "sender": 1, "content": "{不是 json", "created_at": 0},
           {"id": 2, "sender": 1, "content": None, "created_at": 0}]
    assert xg.extract_shares(bad, "g") == []


# ---------------------------------------------------------------- 采集

def test_sync_group_shares_registers_pending_and_is_idempotent(session, monkeypatch) -> None:
    """采集只登记 pending;同一条分享跑两轮**不会重复登记**(唯一键 + 同轮去重)。"""
    monkeypatch.setattr(xg, "list_groups",
                        lambda: [{"group_id": "1550069837", "name": "三岁分享", "role": "member"}])
    monkeypatch.setattr(xg, "group_records", lambda *a, **k: [
        _rec(1, 887920981, {"type": 7, "data": {"share_id": "A1", "share_url": "u-A1",
                                                "title": "资源甲"}}),
        _rec(2, 887920981, {"type": 7, "data": {"share_id": "B1", "share_url": "u-B1",
                                                "title": "资源乙"}})])

    first = xg.sync_group_shares(session, 1)
    assert first["status"] == "ok" and first["new"] == 2
    second = xg.sync_group_shares(session, 1)          # 再跑一轮:一条都不该新增
    assert second["new"] == 0

    rows = session.scalars(select(XunleiGroupShare)).all()
    assert {r.share_id for r in rows} == {"A1", "B1"}
    assert {r.status for r in rows} == {"pending"}
    assert rows[0].group_name == "三岁分享"


def test_sync_group_shares_without_cred_returns_no_cred(session, monkeypatch) -> None:
    """没配迅雷凭据时给明确状态,不抛异常。"""
    monkeypatch.setattr(xg, "_headers", lambda: None)
    assert xg.sync_group_shares(session, 1)["status"] == "no_cred"


# ---------------------------------------------------------------- 转存

def test_transfer_pending_fills_our_share_and_marks_failed(session, monkeypatch) -> None:
    """转存成功 → 回填我方链/提取码/fid 且 status=ok;失败 → status=failed 并留原因。"""
    from app.services import xunlei_transfer as xt

    session.add_all([
        XunleiGroupShare(user_id=1, group_id="g", group_name="三岁分享", share_id="A1",
                         origin_url="u-A1", title="资源甲", status="pending"),
        XunleiGroupShare(user_id=1, group_id="g", group_name="三岁分享", share_id="B1",
                         origin_url="u-B1", title="资源乙", status="pending")])
    session.commit()

    def fake_transfer(url, parent_id="", settings=None):
        if url == "u-A1":
            return {"status": "ok", "share_url": "https://pan.xunlei.com/s/OUR1?pwd=abcd",
                    "code": "abcd", "fid": "F1"}
        return {"status": "failed", "message": "分享已失效"}

    monkeypatch.setattr(xt, "transfer_and_share", fake_transfer)
    out = xg.transfer_pending(session, 1, limit=5)
    assert out["picked"] == 2 and out["ok"] == 1 and out["failed"] == 1
    assert out["items"][0]["share_url"] == "https://pan.xunlei.com/s/OUR1?pwd=abcd"

    rows = {r.share_id: r for r in session.scalars(select(XunleiGroupShare)).all()}
    assert rows["A1"].status == "ok" and rows["A1"].fid == "F1"
    assert rows["A1"].our_url.endswith("pwd=abcd")
    assert rows["B1"].status == "failed" and "失效" in rows["B1"].message


def test_transfer_pending_respects_limit(session, monkeypatch) -> None:
    """限量:库里 pending 再多,一轮也只转 `limit` 条(转存慢且占盘)。"""
    from app.services import xunlei_transfer as xt

    session.add_all([XunleiGroupShare(user_id=1, group_id="g", share_id=f"S{i}",
                                      origin_url=f"u-{i}", title=f"资源{i}", status="pending")
                     for i in range(8)])
    session.commit()
    monkeypatch.setattr(xt, "transfer_and_share",
                        lambda url, parent_id="", settings=None: {"status": "ok",
                                                                  "share_url": "s", "code": "", "fid": "f"})
    assert xg.transfer_pending(session, 1, limit=3)["picked"] == 3


def test_list_group_shares_returns_transferred_url(session) -> None:
    """列表要带出我方链与状态,前端才看得到"采集结果"而不是只有群主原链。"""
    session.add(XunleiGroupShare(user_id=1, group_id="1550069837", group_name="三岁分享",
                                 share_id="A1", origin_url="u-A1", title="资源甲",
                                 status="ok", our_url="https://pan.xunlei.com/s/OUR1",
                                 pass_code="abcd"))
    session.commit()
    items = xg.list_group_shares(session, 1)
    assert items[0]["group_name"] == "三岁分享" and items[0]["our_url"].endswith("OUR1")
    assert xg.list_group_shares(session, 1, status="pending") == []
