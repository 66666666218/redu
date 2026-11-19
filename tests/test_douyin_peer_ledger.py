# -*- coding: utf-8 -*-
"""抖音对标号推送的**盘链闸**端到端单测(2026-11-19)。

链路:`push_works` 在 AI 筛选之后、分赛道渲染之前过 `push_ledger.drop_recent`。

⚠️ 这条链与公众号那条**口径不同**:`hot_min=0.0`(不开热点放行)。
原因:这条链的 `metrics` 大多为空(近 7 天 81 行里 57 行空),点赞常常是 0 ——
若套用"没有基准 ⇒ 挡"的严格口径,会把**正常的新资源**一起挡掉。
本文件验:库里同一个盘链推过 ⇒ 换个 `aweme_id` 也不推;批内重复也挡;无盘链不受影响。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.db import models  # noqa: F401,E402
from app.db.database import Base  # noqa: E402
from app.db.models import User  # noqa: E402
from app.services import douyin_peer_scan as dps  # noqa: E402


class _S:
    douyin_peer_enabled = True
    douyin_peer_require_pan = False
    douyin_peer_max_enrich = 0
    douyin_peer_fresh_hours = 0
    douyin_peer_fresh_allow_unknown = True
    feishu_webhook_douyin_peer = "https://example.com/peer"
    feishu_secret = ""
    feishu_webhook_admin = ""
    feishu_webhook = ""
    feishu_webhook_douhot = ""
    #: 与生产默认一致(30%);这里只是为了显式写出,免得用例依赖 Settings 默认值。
    push_hot_rise_pct = 0.30


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    db.add(User(id=1, username="u1", email="u1@b.c", password_hash="x", enabled=True))
    db.commit()
    yield db
    db.close()


def _fake_feishu(monkeypatch, sent: list) -> None:
    from app.services import feishu_client

    class _C:
        def __init__(self, webhook, secret):
            pass

        def send_card(self, card):
            sent.append(card)
            return True

    monkeypatch.setattr(feishu_client, "FeishuClient", _C)


def _work(vid: str, pan: str = "", likes: int = 10, title: str = "作品") -> dict:
    return {"aweme_id": vid, "url": f"https://www.douyin.com/video/{vid}",
            "title": title, "likes": likes, "niche": "影视",
            "pan_url": pan, "mark": "", "account_uid": "a", "account_name": "甲",
            "author": "甲"}


def test_同一盘链换作品不再推(session, monkeypatch) -> None:
    """同一个对标号反复发同一份资源(`aweme_id` 不同)⇒ 第二轮被挡。

    实测(近 3 天):`pan.xunlei.com/s/VP6XqobV5vdtY7KuROqmhouKA1` 被推了 **14 次**。
    """
    sent: list = []
    _fake_feishu(monkeypatch, sent)
    pan = "https://pan.xunlei.com/s/VP6XqobV5vdtY7KuROqmhouKA1"

    assert dps.push_works([_work("1", pan, title="第一条")], _S(),
                          session=session, user_id=1) is True
    assert len(sent) == 1

    #: 换了个作品 id、同一份资源
    assert dps.push_works([_work("2", pan, title="第二条")], _S(),
                          session=session, user_id=1) is True
    assert len(sent) == 1, "同一份资源 72h 内推过,不该再发一张卡"


def test_没盘链的作品不受影响(session, monkeypatch) -> None:
    sent: list = []
    _fake_feishu(monkeypatch, sent)
    assert dps.push_works([_work("1", "", title="没盘一")], _S(),
                          session=session, user_id=1) is True
    assert dps.push_works([_work("2", "", title="没盘二")], _S(),
                          session=session, user_id=1) is True
    assert len(sent) == 2


def test_批内重复也挡且返回处理过(monkeypatch) -> None:
    """`session=None`(纯渲染)时库里的判不了,但**同一批里**的重复仍要挡。

    全部被挡时 `push_works` 返回 `True` = 「这一批处理过了」,
    否则调用方会把这批永远留在 `pushed_at IS NULL` 里反复重试。
    """
    sent: list = []
    _fake_feishu(monkeypatch, sent)
    pan = "https://pan.quark.cn/s/d1d3c3385b95"
    works = [_work("1", pan, title="第一条"), _work("2", pan, title="第二条")]
    assert dps.push_works(works, _S()) is True
    assert len(sent) == 1
    card = str(sent[0])
    assert "第一条" in card and "第二条" not in card


def test_全部被挡时不发空卡但算处理过(session, monkeypatch) -> None:
    sent: list = []
    _fake_feishu(monkeypatch, sent)
    pan = "https://pan.xunlei.com/s/VP5RHJ4osQZQn3E5RWTCJMFFA1"
    assert dps.push_works([_work("1", pan, title="先推")], _S(),
                          session=session, user_id=1) is True
    before = len(sent)
    assert dps.push_works([_work("9", pan, title="再推")], _S(),
                          session=session, user_id=1) is True, \
        "全被挡要返回 True,否则这批永远补推"
    assert len(sent) == before, "不许为 0 条发一张空卡"