# -*- coding: utf-8 -*-
"""抖音**对标号**监控单测(2026-10-29)。

链路:库里 `cross_platform_accounts(platform="douyin", uid=sec_uid)` →
打开主页读最近作品 → **只推没见过的 vid** → 落 `douyin_peer_works` → 推抖音同行群。

⚠️ 本文件最要紧的三条:
  ① **首次扫描只建基线不推**(新号主页有 20+ 条旧作品,全推=上线即刷屏);
  ② **推送失败不许丢**(`pushed_at` 留空,下一轮补推);
  ③ **短视频/主页那套点赞数写法**(`1.7万`)要能解析 —— 解析成 0 就是"热度列全空"的假象。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.db import models  # noqa: F401
from app.db.database import Base
from app.db.models import CrossPlatformAccount, DouyinPeerWork, User  # noqa: E402
from app.services import douyin_peer_scan as dps  # noqa: E402


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    db.add(User(id=1, username="u1", email="u1@b.c", password_hash="x", enabled=True))
    db.commit()
    yield db
    db.close()


class _S:
    douyin_peer_enabled = True
    douyin_peer_max_accounts = 6
    douyin_peer_max_works = 6
    feishu_webhook_douyin_peer = "https://example.com/peer"
    feishu_secret = ""
    feishu_webhook_admin = ""
    feishu_webhook = ""
    feishu_webhook_douhot = ""


def _acct(session, uid="MS4wLjABAAAAtest", name="小游戏推广", status="active",
          last_scan_at=None):
    row = CrossPlatformAccount(user_id=1, platform="douyin", uid=uid, name=name,
                               url=f"https://www.douyin.com/user/{uid}",
                               status=status, last_scan_at=last_scan_at)
    session.add(row)
    session.commit()
    return row


# ---------------------------------------------------------------- 解析

def test_parse_metric_handles_chinese_units() -> None:
    """主页卡片的点赞数写法:`9217` / `1.7万` / `2.3亿`。

    ⚠️ 解析成 0 不会报错,只会让卡片「点赞」列**整列空白** —— 那种假象最难发现。
    """
    assert dps.parse_metric("9217") == 9217
    assert dps.parse_metric("1.7万") == 17000
    assert dps.parse_metric("12w") == 120000
    assert dps.parse_metric("2.3亿") == 230000000
    assert dps.parse_metric("") == 0
    assert dps.parse_metric("置顶") == 0          # 不是数字就不编


def test_parse_work_takes_last_line_as_title() -> None:
    """实测两种形状(2026-10-29 一手抓的):
        `13 / ##疯狂小鱼塘 …`              → 点赞 13,标题是最后一行
        `置顶 / 1.7万 / 亿万玩家用创意…`    → 点赞 1.7 万,置顶标记单独占一行
    """
    a = dps.parse_work("13\n##疯狂小鱼塘 ##抖音小游戏 #包塘抓鱼")
    assert a == {"title": "##疯狂小鱼塘 ##抖音小游戏 #包塘抓鱼", "likes": 13, "pinned": False}

    b = dps.parse_work("置顶\n1.7万\n亿万玩家用创意点燃小游戏")
    assert b["likes"] == 17000 and b["pinned"] is True
    assert b["title"] == "亿万玩家用创意点燃小游戏"

    assert dps.parse_work("") == {"title": "", "likes": 0, "pinned": False}


def test_rows_to_works_labels_niche_and_author() -> None:
    rows = [
        {"vid": "111", "text": "13\n#抖音小游戏 #包塘模拟器"},
        {"vid": "222", "text": "5\n四级真题pdf 资料合集"},
        {"vid": "111", "text": "重复"},                     # 同一个 vid 再来一次
    ]
    got = dps._rows_to_works(rows, {"uid": "u1", "name": "阿德"}, limit=6)
    assert [w["aweme_id"] for w in got] == ["111", "222"]
    assert got[0]["niche"] == "小游戏" and got[0]["author"] == "阿德"
    assert got[1]["niche"] == "资料"
    assert got[0]["url"] == "https://www.douyin.com/video/111"
    #: 台账认 `author`,不认 `account_name` —— 少了它日结的「作者」列会整列空
    assert got[0]["author"] == "阿德"


# ---------------------------------------------------------------- 基线 / 新作品

def test_first_scan_is_baseline_and_pushes_nothing(session) -> None:
    """首次扫描(这个号一条记录都没有)⇒ 全部落库、`pushed_at` 直接填上、**不推**。"""
    works = [{"aweme_id": "1", "url": "u1", "title": "t1", "likes": 1, "niche": "小游戏",
              "account_uid": "a", "account_name": "A", "author": "A"},
             {"aweme_id": "2", "url": "u2", "title": "t2", "likes": 2, "niche": "小游戏",
              "account_uid": "a", "account_name": "A", "author": "A"}]
    assert dps.record_new(session, 1, works, baseline=True) == []
    rows = session.scalars(select(DouyinPeerWork)).all()
    assert len(rows) == 2 and all(r.pushed_at is not None for r in rows)


def test_later_scan_returns_only_unseen(session) -> None:
    old = [{"aweme_id": "1", "url": "u1", "title": "old", "likes": 0, "niche": "",
            "account_uid": "a", "account_name": "A", "author": "A"}]
    dps.record_new(session, 1, old, baseline=True)
    got = dps.record_new(session, 1, old + [
        {"aweme_id": "3", "url": "u3", "title": "new", "likes": 9, "niche": "小游戏",
         "account_uid": "a", "account_name": "A", "author": "A"}], baseline=False)
    assert [w["aweme_id"] for w in got] == ["3"]
    fresh = session.scalar(select(DouyinPeerWork).where(DouyinPeerWork.aweme_id == "3"))
    assert fresh.pushed_at is None, "新作品要等推送成功才打戳"


def test_pending_is_retried_and_marked(session) -> None:
    """★ 推送失败**不许丢**:`pushed_at` 留空 ⇒ 下一轮 `pending()` 还能取回来。"""
    works = [{"aweme_id": "9", "url": "u9", "title": "t", "likes": 0, "niche": "小游戏",
              "account_uid": "a", "account_name": "A", "author": "A"}]
    dps.record_new(session, 1, works, baseline=False)
    assert [w["aweme_id"] for w in dps.pending(session, 1)] == ["9"]
    dps.mark_pushed(session, 1, works)
    session.flush()
    assert dps.pending(session, 1) == []


# ---------------------------------------------------------------- 目标账号

def test_target_accounts_only_active_douyin_and_rotates(session) -> None:
    from datetime import datetime, timedelta

    a1 = _acct(session, uid="u-new")                                  # 没扫过 → 最前
    a2 = _acct(session, uid="u-old", last_scan_at=datetime.now() - timedelta(days=2))
    _acct(session, uid="u-off", status="dismissed")
    session.add(CrossPlatformAccount(user_id=1, platform="bilibili", uid="b1", name="B站号"))
    session.commit()

    got = dps.target_accounts(session, 1, _S())
    assert [a["uid"] for a in got] == ["u-new", "u-old"]
    assert {a["id"] for a in got} == {a1.id, a2.id}


def test_target_accounts_respects_limit(session) -> None:
    for i in range(4):
        _acct(session, uid=f"u{i}")

    class _L(_S):
        douyin_peer_max_accounts = 2

    assert len(dps.target_accounts(session, 1, _L())) == 2


# ---------------------------------------------------------------- 推送

def test_push_works_card_groups_by_niche(monkeypatch) -> None:
    from app.services import feishu_client

    sent: dict = {}

    class _C:
        def __init__(self, webhook, secret):
            sent["webhook"] = webhook

        def send_card(self, card):
            sent["card"] = card
            return True

    monkeypatch.setattr(feishu_client, "FeishuClient", _C)
    works = [
        {"aweme_id": "1", "url": "https://d/1", "title": "狗哥现代 全80集 追剧",
         "likes": 12, "niche": "影视", "account_name": "甲", "author": "甲"},
        {"aweme_id": "2", "url": "https://d/2", "title": "#抖音小游戏 #包塘模拟器",
         "likes": 17000, "niche": "小游戏", "account_name": "乙", "author": "乙"},
    ]
    assert dps.push_works(works, _S()) is True
    assert sent["webhook"] == "https://example.com/peer"
    text = str(sent["card"])
    assert "抖音对标号" in text and "新作品 2 条" in text
    assert "https://d/1" in text and "https://d/2" in text
    assert "↗17000" in text                       # 点赞要显形
    markers = [e["text"]["content"] for e in sent["card"]["elements"]
               if e.get("tag") == "div" and "▍" in str(e.get("text"))]
    assert markers == ["**▍小游戏** · 1 条", "**▍影视** · 1 条"], markers


def test_push_works_without_webhook_is_noop() -> None:
    class _No(_S):
        feishu_webhook_douyin_peer = ""

    assert dps.push_works([{"aweme_id": "1", "title": "t"}], _No()) is False


# ---------------------------------------------------------------- 一轮作业

def test_run_for_user_baseline_then_new(session, monkeypatch) -> None:
    """第一次只建基线(**不推**);第二次出现新 vid ⇒ 推 1 条并把台账打上戳。"""
    _acct(session, uid="u1", name="阿德")
    pushed: list = []

    def _fake_fetch(accounts, settings, *, user_id=1):
        return {"u1": [
            {"aweme_id": "old", "url": "https://d/old", "title": "老作品",
             "likes": 1, "niche": "小游戏", "account_uid": "u1",
             "account_name": "阿德", "author": "阿德"}]}

    monkeypatch.setattr(dps, "fetch_accounts", _fake_fetch)
    monkeypatch.setattr(dps, "push_works",
                        lambda ws, s, **kw: pushed.append(list(ws)) or True)

    first = dps.run_for_user(session, 1, _S())
    assert first["new"] == 0 and first["baselines"] == ["阿德"] and not pushed

    def _fake_fetch2(accounts, settings, *, user_id=1):
        return {"u1": [
            {"aweme_id": "new", "url": "https://d/new", "title": "#抖音小游戏 新玩法",
             "likes": 3, "niche": "小游戏", "account_uid": "u1",
             "account_name": "阿德", "author": "阿德"}]}

    monkeypatch.setattr(dps, "fetch_accounts", _fake_fetch2)
    second = dps.run_for_user(session, 1, _S())
    assert second["new"] == 1 and second["pushed"] is True
    assert [w["aweme_id"] for w in pushed[0]] == ["new"]
    row = session.scalar(select(DouyinPeerWork).where(DouyinPeerWork.aweme_id == "new"))
    assert row.pushed_at is not None


def test_run_for_user_without_accounts_says_so(session) -> None:
    """没有号时**要写明原因** —— 否则"没配号"与"号今天没发"在运行记录上同形。"""
    res = dps.run_for_user(session, 1, _S())
    assert res["accounts"] == 0 and "douyin_peer_add" in res["note"]


def test_fetch_accounts_all_failed_raises(monkeypatch) -> None:
    """全部号抓取失败 ⇒ **必须抛**;返回空 dict 会被记成"这些号今天没发东西"。"""
    from app.services.douyin_cdp_source import DouyinCdpError

    class _Page:
        def goto(self, *a, **k):
            raise RuntimeError("boom")

        def close(self):
            pass

    def _fake_with_page(settings, fn, **kw):
        return fn(_Page())

    monkeypatch.setattr(dps, "_with_page", _fake_with_page)
    with pytest.raises(DouyinCdpError):
        dps.fetch_accounts([{"uid": "u1", "name": "A"}], _S())
