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
from app.db.models import RunRecord, User, XunleiGroupShare
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


# ---------------------------------------------------------------- 补翻(2026-10-03)
#
# `group_records` 每页只回 20 条,而旧实现只拉最新一页 → "两次采集之间消息超过 20 条"
# 的那段(停机/连续失败/群刷屏)里的分享会被**永久跳过且不报错**。
# 判据:拿库里已有的 share_id 当水位线,整页都是新分享时才往回翻(direction=1)。

def _share(rid: int, sid: str) -> dict:
    return _rec(rid, 887920981, {"type": 7, "data": {"share_id": sid,
                                                     "share_url": f"http://pan.xunlei.com/s/{sid}",
                                                     "title": f"资源{sid}"}})


def _fake_records(pages: dict, calls: list):
    """按 `record_id` 分页的假接口:键 0 = 最新一页,其余 = 以该 id 为游标往回翻。"""
    def _f(gid, count=20, record_id=0, direction=0):
        calls.append((record_id, direction))
        return pages.get(record_id, [])
    return _f


def test_catchup_costs_nothing_when_page_has_a_known_share(session, monkeypatch) -> None:
    """稳态:**这一页里有见过的分享** → 一次请求都不多花(补翻只在真漏消息时发生)。"""
    session.add(XunleiGroupShare(user_id=1, group_id="g1", group_name="群一",
                                 message_id="1", share_id="A1", origin_url="u"))
    session.commit()
    calls: list = []
    monkeypatch.setattr(xg, "list_groups", lambda: [{"group_id": "g1", "name": "群一"}])
    monkeypatch.setattr(xg, "group_records", _fake_records(
        {0: [_share(30, "A1"), _share(29, "B1")]}, calls))

    xg.sync_group_shares(session, 1, group_ids=["g1"])
    assert len(calls) == 1, f"稳态下多翻了页:{calls}"
    assert calls[0] == (0, 0)


def test_catchup_pages_back_when_whole_page_is_new(session, monkeypatch) -> None:
    """整页都是没见过的分享 = 真漏消息的信号 → 往回翻,把中间那批补回来。

    注意走的是 `record_id=<本页最旧 id>` + `direction=1`(实测=往更旧翻)。
    """
    calls: list = []
    monkeypatch.setattr(xg, "list_groups", lambda: [{"group_id": "g1", "name": "群一"}])
    monkeypatch.setattr(xg, "group_records", _fake_records(
        {0: [_share(30, "C1"), _share(29, "C2")],
         29: [_share(29, "C2"), _share(20, "B1"), _share(19, "B2")]}, calls))

    out = xg.sync_group_shares(session, 1)
    assert out["new"] == 4, "补翻回来的分享没入库"
    assert {r.share_id for r in session.scalars(select(XunleiGroupShare)).all()} == {
        "C1", "C2", "B1", "B2"}
    assert calls[1] == (29, 1), f"补翻没带游标/方向:{calls}"


def test_catchup_stops_as_soon_as_it_reaches_a_known_share(session, monkeypatch) -> None:
    """翻到"有见过的分享"的那一页就停 —— 说明已接上水位线,再往前是本轮之前采过的。"""
    session.add(XunleiGroupShare(user_id=1, group_id="g1", group_name="群一",
                                 message_id="10", share_id="B1", origin_url="u"))
    session.commit()
    calls: list = []
    monkeypatch.setattr(xg, "list_groups", lambda: [{"group_id": "g1", "name": "群一"}])
    monkeypatch.setattr(xg, "group_records", _fake_records(
        {0: [_share(30, "C1")],
         30: [_share(30, "C1"), _share(10, "B1")],     # 这页含已见过的 B1 → 到此为止
         10: [_share(9, "A1")]}, calls))

    out = xg.sync_group_shares(session, 1)
    assert out["new"] == 1                              # 只补回 C1(B1 已在库)
    assert len(calls) == 2, f"到达水位线后还在翻:{calls}"


def test_catchup_has_a_page_cap(session, monkeypatch) -> None:
    """保险丝:接口若一直不回我们见过的分享,不能无限翻下去打爆接口。"""
    calls: list = []

    def _always_new(gid, count=20, record_id=0, direction=0):
        calls.append((record_id, direction))
        base = 1000 - len(calls) * 10
        return [_share(base, f"S{len(calls)}-{base}")]

    monkeypatch.setattr(xg, "list_groups", lambda: [{"group_id": "g1", "name": "群一"}])
    monkeypatch.setattr(xg, "group_records", _always_new)
    xg.sync_group_shares(session, 1)
    assert len(calls) == xg._MAX_CATCHUP_PAGES, f"没按上限收手:{len(calls)} 次"


def test_catchup_failure_does_not_lose_the_first_page(session, monkeypatch) -> None:
    """补翻失败**不拖垮整轮**:最新一页已经拿到手,顶多少补几条历史。"""
    monkeypatch.setattr(xg, "list_groups", lambda: [{"group_id": "g1", "name": "群一"}])

    def _boom(gid, count=20, record_id=0, direction=0):
        if record_id == 0:
            return [_share(30, "C1"), _share(29, "C2")]      # 首页正常
        raise xg.XunleiGroupError("补翻被挡")

    monkeypatch.setattr(xg, "group_records", _boom)
    out = xg.sync_group_shares(session, 1)
    assert out["status"] == "ok" and out["new"] == 2, "补翻失败把首页也丢了"


def test_first_page_failure_still_raises(session, monkeypatch) -> None:
    """**首页**拉不到仍然是硬失败(不能因为加了补翻就把这条契约吃掉)。

    这条守住 2026-10-03 修的另一点:"全群拉不到"必须冒出去记 `failed`,
    否则"被挡住"和"群里今天真没新分享"在运行记录里长得一模一样。
    """
    monkeypatch.setattr(xg, "list_groups", lambda: [{"group_id": "g1", "name": "群一"}])

    def _boom(*a, **k):
        raise xg.XunleiGroupError("全群消息拉取失败")

    monkeypatch.setattr(xg, "group_records", _boom)
    with pytest.raises(xg.XunleiGroupError):
        xg.sync_group_shares(session, 1)


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
        return {"status": "failed", "message": "未知错误(非终态)"}

    monkeypatch.setattr(xt, "transfer_and_share", fake_transfer)
    out = xg.transfer_pending(session, 1, limit=5)
    assert out["picked"] == 2 and out["ok"] == 1 and out["failed"] == 1
    assert out["items"][0]["share_url"] == "https://pan.xunlei.com/s/OUR1?pwd=abcd"

    rows = {r.share_id: r for r in session.scalars(select(XunleiGroupShare)).all()}
    assert rows["A1"].status == "ok" and rows["A1"].fid == "F1"
    assert rows["A1"].our_url.endswith("pwd=abcd")
    assert rows["B1"].status == "failed" and "未知错误" in rows["B1"].message


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


# ---------------------------------------------------------------- 转存闸门(2026-10-02)

def test_is_bulk_resource_flags_giant_collections() -> None:
    """泛化大包要认出来 —— 实测就是「【全网最齐】游戏软件资源合集」把盘顶爆的。"""
    assert xg.is_bulk_resource("【全网最齐】游戏软件资源合集")
    assert xg.is_bulk_resource("全网最全宝库")
    assert xg.is_bulk_resource("最全文件")
    assert not xg.is_bulk_resource("手机警报器（警笛模拟器）2.0版")
    assert not xg.is_bulk_resource("蓝河工具箱")
    assert not xg.is_bulk_resource("")


class _GateSettings:
    xunlei_transfer_max_usage_ratio = 0.9


def test_admit_transfer_blocks_when_disk_almost_full() -> None:
    """盘到阈值 → **整批不搬**(这次翻车的直接原因),原因要能读懂。"""
    ok, why, retryable = xg.admit_transfer("蓝河工具箱", settings=_GateSettings(), ratio=1.26)
    assert ok is False and "盘快满了" in why and "126%" in why
    assert retryable is True                     # 盘满**可重试**:清完空间该自动接着搬
    ok, _, _ = xg.admit_transfer("蓝河工具箱", settings=_GateSettings(), ratio=0.5)
    assert ok is True


def test_admit_transfer_blocks_bulk_even_when_space_ok() -> None:
    """空间够也不搬泛化大包 —— 体积不可控,只把链推给人。"""
    ok, why, retryable = xg.admit_transfer("【全网最齐】游戏软件资源合集",
                                           settings=_GateSettings(), ratio=0.3)
    assert ok is False and "泛化大包" in why
    assert retryable is False                    # 泛化大包**不可重试**:策略性不搬


def test_admit_transfer_allows_when_ratio_unknown() -> None:
    """配额**拿不到**时不能误判成"满" —— 否则接口一抖就整个停摆(探针失败≠盘满)。"""
    ok, _, _ = xg.admit_transfer("蓝河工具箱", settings=_GateSettings(), ratio=None)
    assert ok is True


def test_transfer_pending_marks_skipped_instead_of_transferring(session, monkeypatch) -> None:
    """闸门挡下的行标 `skipped` 并留原因,**不调用转存**;剩下的照常处理。"""
    from app.services import xunlei_transfer as xt

    session.add_all([
        XunleiGroupShare(user_id=1, group_id="g", share_id="A", title="【全网最齐】资源合集",
                         origin_url="u-A", status="pending"),
        XunleiGroupShare(user_id=1, group_id="g", share_id="B", title="蓝河工具箱",
                         origin_url="u-B", status="pending")])
    session.commit()
    monkeypatch.setattr(xt, "quota_ratio", lambda cred=None: 0.3)
    called: list[str] = []
    monkeypatch.setattr(xt, "transfer_and_share",
                        lambda url, parent_id="", settings=None: (
                            called.append(url) or {"status": "ok", "share_url": "s",
                                                   "code": "", "fid": "f"}))
    out = xg.transfer_pending(session, 1, limit=5, settings=_GateSettings())
    assert out["ok"] == 1 and out["skipped"] == 1 and called == ["u-B"]
    rows = {r.share_id: r for r in session.scalars(select(XunleiGroupShare)).all()}
    assert rows["A"].status == "skipped" and "泛化大包" in rows["A"].message
    assert rows["B"].status == "ok"


def test_transfer_pending_keeps_rows_pending_when_disk_full(session, monkeypatch) -> None:
    """⚠️ 盘满时**整批停下、行保持 pending** —— 清完空间下一轮自动接着搬。

    若把盘满也标成终态(skipped),用户清理完还得人工重新排队:那是把方便留给代码、
    麻烦留给人。所以"可重试"与"策略性不搬"必须分开。
    """
    from app.services import xunlei_transfer as xt

    session.add(XunleiGroupShare(user_id=1, group_id="g", share_id="A", title="蓝河工具箱",
                                 origin_url="u-A", status="pending"))
    session.commit()
    monkeypatch.setattr(xt, "quota_ratio", lambda cred=None: 1.26)
    monkeypatch.setattr(xt, "transfer_and_share",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("盘满不该转存")))
    out = xg.transfer_pending(session, 1, limit=5, settings=_GateSettings())
    assert out["status"] == "disk_full" and out["picked"] == 0 and "盘快满了" in out["message"]
    assert session.scalars(select(XunleiGroupShare)).one().status == "pending"   # 没被标死


def test_is_space_error_recognizes_disk_full() -> None:
    """盘满要**认得出来** —— 它不是"这条资源的问题",是可重试的盘问题。"""
    from app.services import xunlei_transfer as xt

    assert xt.is_space_error("{'error': 'file_space_not_enough', ...}")
    assert xt.is_space_error("转存失败:{'error_description': '空间不足，请清理后再试'}")
    assert not xt.is_space_error("分享已失效")
    assert not xt.is_space_error("")


def test_transfer_pending_stops_and_keeps_pending_on_space_error(session, monkeypatch) -> None:
    """**第二层兜底**:闸门漏了(配额探针失效)也要保住 —— 真撞"空间不足"时
    行**保持 pending**、整批立刻停下,而不是标终态(标了就永远不再搬)。"""
    from app.services import xunlei_transfer as xt

    session.add_all([
        XunleiGroupShare(user_id=1, group_id="g", share_id="A", title="蓝河工具箱",
                         origin_url="u-A", status="pending"),
        XunleiGroupShare(user_id=1, group_id="g", share_id="B", title="警笛模拟器",
                         origin_url="u-B", status="pending")])
    session.commit()
    monkeypatch.setattr(xt, "quota_ratio", lambda cred=None: None)     # 探针失效 → 闸门放行
    monkeypatch.setattr(xt, "transfer_and_share",
                        lambda url, parent_id="", settings=None: {
                            "status": "failed",
                            "message": "{'error': 'file_space_not_enough', 'error_description': '空间不足'}"})
    out = xg.transfer_pending(session, 1, limit=5, settings=_GateSettings())
    assert out["status"] == "disk_full" and out["failed"] == 0
    # 探针拿不到 → **不下"闸门失准"的结论**(与 admit_transfer 的"不知道≠满"同一条纪律)
    assert out["gate_mismatch"] is False
    statuses = {r.share_id: r.status for r in session.scalars(select(XunleiGroupShare)).all()}
    assert statuses == {"A": "pending", "B": "pending"}                # 一条都没被标死


def test_oversized_single_resource_is_skipped_without_stalling_the_batch(
        session, monkeypatch) -> None:
    """⚠️ **2026-10-04 用户口径**:「不一定是搬不动,有没有可能是你**一次搬太多**」。

    实测:盘 30.10 TiB、已用 79.9%、**还剩 6.04 TiB**,而卡住的那个包要 **6.88 TiB**
    —— 所以**不是盘满,是这一个包比剩余空间大**。这两件事**该有完全不同的处置**:
    盘满 → 整批停(清空间后继续);单个太大 → **只跳过这一条,其余照搬**。
    旧实现一律走"整批停",于是**一个大包把后面 30 条全卡住了**(放大伤害)。
    """
    from app.services import xunlei_transfer as xt

    session.add_all([
        XunleiGroupShare(user_id=1, group_id="g", share_id="BIG", title="某大包资源",
                         origin_url="u-BIG", status="pending"),
        XunleiGroupShare(user_id=1, group_id="g", share_id="OK1", title="正常资源1",
                         origin_url="u-OK1", status="pending"),
        XunleiGroupShare(user_id=1, group_id="g", share_id="OK2", title="正常资源2",
                         origin_url="u-OK2", status="pending")])
    session.commit()
    monkeypatch.setattr(xt, "quota_ratio", lambda cred=None: None)     # 探针失效 → 闸门放行

    def fake(url, parent_id="", settings=None):
        if url == "u-BIG":
            return {"status": "failed", "code": "space_insufficient",
                    "required_size": 7_563_939_414_460, "free_size": 6_640_745_233_489,
                    "message": "空间不足:这个包需要 6.88 TiB,盘上只剩 6.04 TiB"}
        return {"status": "ok", "share_url": f"our-{url[-1]}", "code": "9", "fid": "N"}

    monkeypatch.setattr(xt, "transfer_and_share", fake)

    out = xg.transfer_pending(session, 1, limit=5, settings=_GateSettings())
    # ⚠️ **不是 disk_full** —— 盘还有空间,只是这一个包太大,整批照常跑完
    assert out["status"] == "ok" and out["too_large"] == 1 and out["ok"] == 2
    assert out["required_size"] == 7_563_939_414_460
    rows = {r.share_id: r for r in session.scalars(select(XunleiGroupShare)).all()}
    assert rows["OK1"].status == "ok" and rows["OK2"].status == "ok", "不能因一个大包拖停别人"
    # ⚠️ 超大那条**保持 pending**(清出空间后仍可搬),不是终态 —— 标死了就永远不再试
    assert rows["BIG"].status == "pending" and "空间不足" in (rows["BIG"].message or "")


def test_space_error_with_roomy_quota_is_flagged_as_gate_mismatch(session, monkeypatch) -> None:
    """⚠️ **2026-10-04 实测的矛盾**:配额探针报 **79.9%**(阈值 90%)= 闸门**本该放行**,
    转存却被迅雷挡回「空间不足」—— 说明**那道预闸门挡不住这件事**。

    必须把两个数一起交出去(供 tick 单独告警),否则只看运行记录只知"盘满",
    看不出"闸门其实没起作用",会一直误以为"到 90% 才会停"。
    """
    from app.services import xunlei_transfer as xt

    session.add(XunleiGroupShare(user_id=1, group_id="g", share_id="A", title="蓝河工具箱",
                                 origin_url="u-A", status="pending"))
    session.commit()
    monkeypatch.setattr(xt, "quota_ratio", lambda cred=None: 0.799)     # 闸门放行
    monkeypatch.setattr(xt, "transfer_and_share",
                        lambda url, parent_id="", settings=None: {
                            "status": "failed",
                            "message": "{'error': 'file_space_not_enough', "
                                       "'error_description': '空间不足'}"})

    out = xg.transfer_pending(session, 1, limit=5, settings=_GateSettings())

    assert out["status"] == "disk_full"
    assert out["gate_mismatch"] is True, "配额说还有空间、转存说不足 —— 这就是闸门失准"
    assert abs(out["quota_ratio"] - 0.799) < 1e-9 and out["quota_limit"] == 0.9


def test_gate_pre_stop_is_not_a_mismatch(session, monkeypatch) -> None:
    """预闸门**按设计**挡下的那种 `disk_full`(quota ≥ 阈值),不许被报成"失准"。"""
    from app.services import xunlei_transfer as xt

    session.add(XunleiGroupShare(user_id=1, group_id="g", share_id="A", title="蓝河工具箱",
                                 origin_url="u-A", status="pending"))
    session.commit()
    monkeypatch.setattr(xt, "quota_ratio", lambda cred=None: 1.26)
    monkeypatch.setattr(xt, "transfer_and_share",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("闸门挡下不该转存")))

    out = xg.transfer_pending(session, 1, limit=5, settings=_GateSettings())

    assert out["status"] == "disk_full" and out["gate_mismatch"] is False


def test_is_own_share_error_recognizes_our_own_share() -> None:
    """认得出"这是我们自己的分享" —— 它**永远不可能成功**,该直接终态。"""
    from app.services import xunlei_transfer as xt

    assert xt.is_own_share_error(
        "{'error': 'file_restore_own', 'error_description': '不能转存自己的文件(夹)'}")
    assert xt.is_own_share_error("转存失败:{'error_description': '转存自己的文件'}")
    assert not xt.is_own_share_error("{'error': 'file_space_not_enough'}")
    assert not xt.is_own_share_error("")


def test_transfer_pending_marks_own_share_as_skipped_not_failed(session, monkeypatch) -> None:
    """自己的分享 → 标 `skipped` 终态(不是 failed)并继续处理下一条,不拖累整批。"""
    from app.services import xunlei_transfer as xt

    session.add_all([
        XunleiGroupShare(user_id=1, group_id="g", share_id="A", title="警笛模拟器",
                         origin_url="u-A", status="pending"),
        XunleiGroupShare(user_id=1, group_id="g", share_id="B", title="蓝河工具箱",
                         origin_url="u-B", status="pending")])
    session.commit()
    monkeypatch.setattr(xt, "quota_ratio", lambda cred=None: 0.3)

    def fake_transfer(url, parent_id="", settings=None):
        if url == "u-A":
            return {"status": "failed",
                    "message": "{'error': 'file_restore_own', 'error_description': '不能转存自己的文件'}"}
        return {"status": "ok", "share_url": "s", "code": "", "fid": "f"}

    monkeypatch.setattr(xt, "transfer_and_share", fake_transfer)
    out = xg.transfer_pending(session, 1, limit=5, settings=_GateSettings())
    assert out["ok"] == 1 and out["skipped"] == 1 and out["failed"] == 0
    rows = {r.share_id: r for r in session.scalars(select(XunleiGroupShare)).all()}
    assert rows["A"].status == "skipped" and "自己的分享" in rows["A"].message
    assert rows["B"].status == "ok"


# ---------------------------------------------------------------- 静默失败修复(2026-10-03)

def _stub_headers(monkeypatch, xg):
    monkeypatch.setattr(xg, "_headers", lambda: {"x": "y"})


def test_list_groups_raises_on_http_error(monkeypatch) -> None:
    """群列表**非 200 = 硬失败**(不是"这个账号没加群")。"""
    from app.services import xunlei_group as xg

    _stub_headers(monkeypatch, xg)

    class _R:
        status_code = 403
        text = "forbidden"

    monkeypatch.setattr(xg.requests, "get", lambda *a, **k: _R())
    with pytest.raises(xg.XunleiGroupError) as ei:
        xg.list_groups()
    assert "403" in str(ei.value)


def test_sync_returns_failed_when_group_list_fails(session, monkeypatch) -> None:
    """⚠️ **群列表拉不到不能当"没加群"**(2026-10-03 修)。

    旧实现返回空表 → tick 记 `success(群0 新0 转存0)`,与"今天群里真没新资源"无法区分,
    凭据失效时整条群链**静默停摆**。
    """
    from app.services import xunlei_group as xg

    _stub_headers(monkeypatch, xg)
    monkeypatch.setattr(xg, "list_groups", lambda: (_ for _ in ()).throw(
        xg.XunleiGroupError("群列表 HTTP 403")))
    out = xg.sync_group_shares(session, 1)
    assert out["status"] == "failed" and "403" in out["message"]


def test_sync_raises_when_every_group_fails(session, monkeypatch) -> None:
    """**每个群都拉不到** → 抛(让 tick 记 failed),不是"今天没有新分享"。"""
    from app.services import xunlei_group as xg

    _stub_headers(monkeypatch, xg)
    monkeypatch.setattr(xg, "list_groups", lambda: [
        {"group_id": "g1", "name": "群一"}, {"group_id": "g2", "name": "群二"}])
    monkeypatch.setattr(xg, "group_records", lambda *a, **k: (_ for _ in ()).throw(
        xg.XunleiGroupError("超时")))
    with pytest.raises(xg.XunleiGroupError) as ei:
        xg.sync_group_shares(session, 1)
    assert "全部拉取失败" in str(ei.value)


def test_sync_keeps_going_when_some_groups_fail(session, monkeypatch) -> None:
    """**部分群失败**要保住其余群的产出,并把失败群**记名**(否则"某个群一直拉不到"查不出来)。"""
    from app.services import xunlei_group as xg

    _stub_headers(monkeypatch, xg)
    monkeypatch.setattr(xg, "list_groups", lambda: [
        {"group_id": "g1", "name": "群一"}, {"group_id": "g2", "name": "坏群"}])

    def _rec(gid, *a, **k):
        if gid == "g2":
            raise xg.XunleiGroupError("超时")
        return [{"content": '{"type":7,"data":{"share_id":"S1","share_url":"https://p/s/S1",'
                            '"title":"某资源","share_user_id":"u"}}'}]

    monkeypatch.setattr(xg, "group_records", _rec)
    out = xg.sync_group_shares(session, 1)
    assert out["status"] == "ok" and out["new"] == 1
    assert out["failed_groups"] == ["坏群"]


def test_tick_records_no_cred_as_failed(session, monkeypatch) -> None:
    """没配凭据**不算成功** —— 它在运行记录里同样会伪装成"群0 新0 一切正常"。"""
    from sqlalchemy import select
    from app.db.models import RunRecord
    import app.db as db_mod
    from app.services import xunlei_group as xg

    monkeypatch.setattr(db_mod, "get_session_local", lambda: (lambda: session))
    monkeypatch.setattr(xg, "sync_group_shares",
                        lambda *a, **k: {"status": "no_cred", "groups": 0, "new": 0,
                                         "failed_groups": [], "message": "未配迅雷凭据"})
    monkeypatch.setattr(xg, "transfer_pending", lambda *a, **k: {})
    xg.xunlei_group_tick()
    runs = session.scalars(select(RunRecord).where(RunRecord.kind == "xunlei_group")).all()
    assert len(runs) == 1 and runs[0].status == "failed"


# ---------------------------------------------------------------- 推送目的地(2026-10-03 用户口径)

class _FakeFeishu:
    last_webhook = ""
    last_card = None

    def __init__(self, webhook, secret="") -> None:
        _FakeFeishu.last_webhook = webhook

    def send_card(self, card: dict) -> bool:
        _FakeFeishu.last_card = card
        return True


class _S:
    feishu_webhook = "CUSTOMER"
    feishu_webhook_admin = "ADMIN"
    feishu_secret = ""
    brand_name = "念飞思雪"
    xunlei_transfer_parent = "最全文件"


def test_new_shares_card_goes_to_customer_group(monkeypatch) -> None:
    """⚠️ **内容卡走客户群,不是管理群**(2026-10-03 用户口径:

    "5个平台卡片推到管理群推的是什么?如果是内容,推客户群里面就行;管理群职责是接受维护的信息")。
    这张卡是**资源 + 可直接用的链** = 内容 → 客户群。以前推管理群是错的。
    """
    from app.services import feishu_client as fc
    from app.services import xunlei_group as xg

    monkeypatch.setattr(fc, "FeishuClient", _FakeFeishu)
    ok = xg.push_new_shares([{"title": "某资源", "share_url": "https://p/s/X", "code": "abcd"}], _S())
    assert ok and _FakeFeishu.last_webhook == "CUSTOMER"


def test_new_shares_card_leads_with_conclusion(monkeypatch) -> None:
    """**结论先行**(用户口径"推送是为了用户更好总结"):先说这批是什么、能不能直接用,

    再上列表 —— 光甩一张表,看的人还要自己数、自己猜。
    """
    from app.services import feishu_client as fc
    from app.services import xunlei_group as xg

    monkeypatch.setattr(fc, "FeishuClient", _FakeFeishu)
    xg.push_new_shares([{"title": "某资源", "share_url": "https://p/s/X", "code": ""}], _S())
    body = _FakeFeishu.last_card["elements"][0]["text"]["content"]
    assert "点开即用" in body and "已全部转存" in body


def test_tick_sends_internal_note_to_admin_only_when_needed(session, monkeypatch) -> None:
    """⚠️ **维护信息 → 管理群**,而且**只在真有需处理项时才推**。

    内容卡去了客户群,但"哪个群拉不到 / 哪条被闸门挡下 / 哪条转存失败"是运营要知道的
    内部消息;每 20 分钟无条件推一条又会变噪音被无视,所以要有条件。
    """
    import app.db as db_mod
    from app.services import alert_service, xunlei_group as xg

    monkeypatch.setattr(db_mod, "get_session_local", lambda: (lambda: session))
    calls: list = []
    monkeypatch.setattr(alert_service, "notify_incident", lambda *a, **k: calls.append(a) or True)

    base = {"status": "ok", "groups": 9, "new": 0, "failed_groups": []}
    clean = {"ok": 0, "skipped": 0, "failed": 0, "items": []}
    monkeypatch.setattr(xg, "sync_group_shares", lambda *a, **k: base)
    monkeypatch.setattr(xg, "transfer_pending", lambda *a, **k: clean)
    monkeypatch.setattr(xg, "push_new_shares", lambda *a, **k: False)
    xg.xunlei_group_tick()
    assert not calls, "一切正常时不该推内部消息(否则就是噪音)"

    dirty = {"ok": 2, "skipped": 3, "failed": 1, "items": []}
    monkeypatch.setattr(xg, "transfer_pending", lambda *a, **k: dirty)
    xg.xunlei_group_tick()
    assert calls and "被闸门挡下" in calls[0][4] and "转存失败" in calls[0][4]


class TestTransferFailuresSurface:
    """⚠️ **转存失败必须反映到运行记录的状态里**(2026-10-04 修)。

    实测:迅雷 `refresh_token` 失效 → 转存 5 条**全挂**,而作业每 20 分钟照报
    `success(群9 新0 转存0)` —— 因为旧实现只看"群采集"的 status,`out['failed']` 被完全忽略。
    采集只用缓存凭据、不需要新 token,**所以只有转存这一步会暴露**;
    它一被吞掉,整条链断了也没人知道。
    """

    def _tick(self, session, monkeypatch, *, t_ok: int, t_bad: int, msg: str = ""):
        monkeypatch.setattr(xg, "sync_group_shares",
                            lambda *a, **k: {"status": "ok", "groups": 9, "new": 0})
        monkeypatch.setattr(xg, "transfer_pending",
                            lambda *a, **k: {"status": "ok", "picked": t_ok + t_bad,
                                             "ok": t_ok, "failed": t_bad, "skipped": 0,
                                             "message": msg, "items": []})
        monkeypatch.setattr(xg, "push_new_shares", lambda *a, **k: None)
        xg.xunlei_group_tick(self._S())
        from sqlalchemy import select as _sel
        return session.scalars(_sel(RunRecord).where(RunRecord.kind == "xunlei_group")
                               .order_by(RunRecord.id.desc())).first()

    class _S:
        xunlei_group_enabled = True
        xunlei_group_transfer_limit = 5
        xunlei_transfer_max_usage_ratio = 0.9
        # 转存失败时 tick 会往**管理群**推一条内部说明 —— 这里留空表示"没配群",
        # `notify_incident` 会直接返回(否则 `webhook_for` 取 `settings.feishu_webhook` 会 AttributeError)
        feishu_webhook = ""
        feishu_webhook_admin = ""
        feishu_secret = ""

    def test_all_transfers_failing_is_recorded_as_failed(self, session, monkeypatch) -> None:
        # `xunlei_group_tick` 用 get_session_local() 自建会话 → 指向测试库
        import app.db as appdb
        monkeypatch.setattr(appdb, "get_session_local",
                            lambda: sessionmaker(bind=session.get_bind()))
        row = self._tick(session, monkeypatch, t_ok=0, t_bad=5,
                         msg="迅雷刷新 token 失败：invalid refresh token")
        assert row.status == "failed", f"转存全失败却记成 {row.status}"
        assert "转存全失败" in row.detail and "失败5" in row.detail

    def test_partial_failures_are_recorded_as_partial(self, session, monkeypatch) -> None:
        import app.db as appdb
        monkeypatch.setattr(appdb, "get_session_local",
                            lambda: sessionmaker(bind=session.get_bind()))
        row = self._tick(session, monkeypatch, t_ok=2, t_bad=1, msg="某条失败")
        assert row.status == "partial"
        assert "部分转存失败" in row.detail

    def test_disk_full_is_not_reported_as_success(self, session, monkeypatch) -> None:
        """⚠️ 盘满时 `failed=0`(那批行**保持 pending** 等清空间),但它**绝不是"成功"** ——
        它意味着"有货但搬不进去"。旧写法会让它落进 success 分支,于是"盘满停摆"和
        "今天群里真没新资源"又长得一样。"""
        import app.db as appdb
        monkeypatch.setattr(appdb, "get_session_local",
                            lambda: sessionmaker(bind=session.get_bind()))
        from app.services.tenant_base import _record_run  # noqa: F401
        monkeypatch.setattr(xg, "sync_group_shares",
                            lambda *a, **k: {"status": "ok", "groups": 9, "new": 3})
        monkeypatch.setattr(xg, "transfer_pending",
                            lambda *a, **k: {"status": "disk_full", "picked": 0, "ok": 0,
                                             "failed": 0, "skipped": 0,
                                             "message": "转存返回空间不足", "items": []})
        monkeypatch.setattr(xg, "push_new_shares", lambda *a, **k: None)
        xg.xunlei_group_tick(self._S())
        row = session.scalars(select(RunRecord).where(RunRecord.kind == "xunlei_group")
                              .order_by(RunRecord.id.desc())).first()
        assert row.status == "failed" and "盘满" in row.detail

    def test_oversized_skip_is_visible_in_the_run_record(self, session, monkeypatch) -> None:
        """⚠️ "单个太大跳过 N 条"**必须写进运行记录** —— 否则运维只看到"转存 N 条",
        完全不知道有货被这样跳过了(它不标终态,连失败计数都不涨)。
        这是"静默失败=假成功"那条纪律在**计数**上的版本。"""
        import app.db as appdb
        monkeypatch.setattr(appdb, "get_session_local",
                            lambda: sessionmaker(bind=session.get_bind()))
        monkeypatch.setattr(xg, "sync_group_shares",
                            lambda *a, **k: {"status": "ok", "groups": 9, "new": 3})
        monkeypatch.setattr(xg, "transfer_pending",
                            lambda *a, **k: {"status": "ok", "picked": 3, "ok": 2,
                                             "failed": 0, "skipped": 0, "too_large": 1,
                                             "required_size": 7_563_939_414_460,
                                             "free_size": 6_640_745_233_489,
                                             "message": "", "items": []})
        monkeypatch.setattr(xg, "push_new_shares", lambda *a, **k: None)
        xg.xunlei_group_tick(self._S())
        row = session.scalars(select(RunRecord).where(RunRecord.kind == "xunlei_group")
                              .order_by(RunRecord.id.desc())).first()
        assert "单个太大跳过1" in row.detail, row.detail
        # 两个数要**同量纲**读得出来(6.88/6.04 TiB),别报成 861.88 GiB 让人自己换算
        assert "6.88 TiB" in row.detail and "6.04 TiB" in row.detail, row.detail
        assert "未标终态" in row.detail, "要说清它还能搬,否则会被当成永久丢弃"

    def test_no_failures_stays_success(self, session, monkeypatch) -> None:
        import app.db as appdb
        monkeypatch.setattr(appdb, "get_session_local",
                            lambda: sessionmaker(bind=session.get_bind()))
        row = self._tick(session, monkeypatch, t_ok=3, t_bad=0)
        assert row.status == "success" and "失败0" in row.detail

    def test_miscalibrated_gate_raises_a_dedicated_alert(self, session, monkeypatch) -> None:
        """⚠️ 盘满的原因**也得分清**:"闸门按设计挡下" 与 "闸门**没起作用**" 是两回事。

        实测(2026-10-04):配额只报 **79.9%**(阈值 90%)、闸门本该放行,转存却已被迅雷挡回
        「空间不足」—— 一路连撞 30 多轮。只看运行记录只知道"盘满",看不出**闸门失准**,
        于是会一直误以为"到 90% 才会停"。**所以要单独告警。**
        反向验证:摘掉 tick 里那段 `if _mm:` 告警,本测试立刻变红。
        """
        import app.db as appdb
        from app.services import alert_service

        monkeypatch.setattr(appdb, "get_session_local",
                            lambda: sessionmaker(bind=session.get_bind()))
        monkeypatch.setattr(xg, "sync_group_shares",
                            lambda *a, **k: {"status": "ok", "groups": 9, "new": 3})
        monkeypatch.setattr(xg, "transfer_pending",
                            lambda *a, **k: {"status": "disk_full", "picked": 0, "ok": 0,
                                             "failed": 0, "skipped": 0,
                                             "quota_ratio": 0.799, "quota_limit": 0.9,
                                             "gate_mismatch": True,
                                             "message": "转存返回空间不足", "items": []})
        monkeypatch.setattr(xg, "push_new_shares", lambda *a, **k: None)
        sent: list[tuple] = []
        monkeypatch.setattr(alert_service, "notify_incident",
                            lambda db, uid, kind, title, detail, **k: (
                                sent.append((kind, title, detail)) or True))

        xg.xunlei_group_tick(self._S())

        assert sent, "闸门失准却没告警 —— 又变成'只有运行记录、没人会看'"
        kind, title, detail = sent[0]
        assert kind == "pan" and "闸门失准" in title
        assert "79.9%" in detail and "90%" in detail
        assert not any(ch.isdigit() for ch in title), \
            "标题不能含数字:冷却门按标题去重,带数字就每轮都算新告警、每轮刷屏"
        row = session.scalars(select(RunRecord).where(RunRecord.kind == "xunlei_group")
                              .order_by(RunRecord.id.desc())).first()
        assert "闸门失准" in row.detail, "运行记录里也要留痕,否则翻记录同样看不出来"

    def test_calibrated_gate_stop_stays_quiet(self, session, monkeypatch) -> None:
        """预闸门**按设计**挡下的盘满不许告警 —— 否则正常的"盘满了"会变噪音被无视。"""
        import app.db as appdb
        from app.services import alert_service

        monkeypatch.setattr(appdb, "get_session_local",
                            lambda: sessionmaker(bind=session.get_bind()))
        monkeypatch.setattr(xg, "sync_group_shares",
                            lambda *a, **k: {"status": "ok", "groups": 9, "new": 3})
        monkeypatch.setattr(xg, "transfer_pending",
                            lambda *a, **k: {"status": "disk_full", "picked": 0, "ok": 0,
                                             "failed": 0, "skipped": 0,
                                             "quota_ratio": 1.26, "quota_limit": 0.9,
                                             "gate_mismatch": False,
                                             "message": "盘快满了", "items": []})
        monkeypatch.setattr(xg, "push_new_shares", lambda *a, **k: None)
        sent: list[tuple] = []
        monkeypatch.setattr(alert_service, "notify_incident",
                            lambda db, uid, kind, title, detail, **k: (
                                sent.append((kind, title)) or True))

        xg.xunlei_group_tick(self._S())

        assert sent == [], "闸门正常挡下也告警 → 告警变噪音"
