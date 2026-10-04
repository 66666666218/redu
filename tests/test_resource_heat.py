"""资源热度:同一资源多人发布 → 按聚合判断(2026-10-04 用户口径)。

钉住的核心是那句「**而不是随机一个文章或者作品来判断**」——
所以最要紧的两条:① **同一发布者多次发布取最大,不求和**(否则自己能把自己刷成爆款);
② **样本数 N 必须带出来**,N=1 不能说成和 N=5 一样可信。
"""
import os
from datetime import datetime, timedelta

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.db.database import Base  # noqa: E402
from app.db.models import WechatArticle, WechatPanLink  # noqa: E402
from app.services import resource_heat as rh  # noqa: E402


@pytest.fixture()
def session():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()


def _post(session, author: str, pan_url: str, read: int, days_ago: int = 1) -> None:
    art = WechatArticle(user_id=1, author=author, title="t", url=f"u{read}",
                        read_num=read, created_at=datetime.now() - timedelta(days=days_ago))
    session.add(art)
    session.flush()
    session.add(WechatPanLink(user_id=1, article_id=art.id, pan_url=pan_url))
    session.commit()


# ---------------------------------------------------------------- 纯函数
def test_single_publisher_is_flagged_as_low_confidence() -> None:
    """★ **N=1 必须说出来** —— 单篇的热度可能只是那个号自己的粉丝结构。"""
    conf, ok = rh.label_confidence(1)
    assert ok is False and "单样本" in conf and "可信度低" in conf
    conf2, ok2 = rh.label_confidence(3)
    assert ok2 is True and "3 个发布者" in conf2


def test_build_heat_uses_the_single_source_for_the_estimate() -> None:
    """曝光→预估必须走 `conversion`,不许在这儿再写一遍 0.3。"""
    from app.services import conversion
    h = rh.build_heat("https://pan.quark.cn/s/x", 3, 1000)
    expect = conversion.estimate("wechat", {"read_num": 1000})["estimate"]
    assert h["estimate"] == expect
    assert h["trusted"] is True and h["publishers"] == 3
    assert "跨发布者求和" in h["exposure_note"]      # 口径要跟着结果走


def test_summary_only_counts_trusted_resources() -> None:
    """合计**只算够可信的** —— 单样本混进去会让总数虚高。"""
    heats = [rh.build_heat("a", 1, 100), rh.build_heat("b", 3, 1000)]
    line = rh.summary_line(heats)
    only_trusted = rh.build_heat("b", 3, 1000)["estimate"]
    assert str(only_trusted) in line
    assert "被反复验证的 1 条" in line


# ---------------------------------------------------------------- 聚合查询
def test_same_publisher_posting_repeatedly_takes_max_not_sum(session) -> None:
    """★ **核心纪律**:同一发布者多次发布同一资源 → 取其**最大值**,**不求和**。

    同一个人反复发,受众高度重叠;求和会把他自己刷成"爆款",与"多人在发"完全两回事。
    """
    _post(session, "同一人", "https://pan.quark.cn/s/AAA", 500)
    _post(session, "同一人", "https://pan.quark.cn/s/AAA", 800)   # 同一人再发一次
    _post(session, "同一人", "https://pan.quark.cn/s/AAA", 300)
    heats = rh.wechat_resource_heat(session, 1)
    assert len(heats) == 1
    h = heats[0]
    assert h["publishers"] == 1, "发布者数按 DISTINCT author 算,不是文章数"
    assert h["exposure"] == 800, f"应取最大 800,实际 {h['exposure']}(1500 说明求和了)"
    assert h["trusted"] is False and "单样本" in h["confidence"]


def test_multiple_publishers_exposure_is_summed(session) -> None:
    """★ 跨发布者才求和 —— 不同人发 = 不同受众看到。"""
    _post(session, "甲", "https://pan.quark.cn/s/BBB", 1000)
    _post(session, "乙", "https://pan.quark.cn/s/BBB", 400)
    _post(session, "乙", "https://pan.quark.cn/s/BBB", 600)      # 乙自己发两次,取最大 600
    h = rh.wechat_resource_heat(session, 1)[0]
    assert h["publishers"] == 2
    assert h["exposure"] == 1000 + 600, "甲 1000 + 乙取最大 600"
    assert h["trusted"] is True


def test_different_resources_are_aggregated_separately(session) -> None:
    """聚合的是"**同一个资源**" —— 不同 pan_url 各算各的(这正是要避免"随机拿一篇代表")。"""
    _post(session, "甲", "https://pan.quark.cn/s/C1", 100)
    _post(session, "乙", "https://pan.quark.cn/s/C2", 900)
    heats = rh.wechat_resource_heat(session, 1)
    assert [h["pan_url"] for h in heats] == ["https://pan.quark.cn/s/C2",
                                             "https://pan.quark.cn/s/C1"]   # 按曝光降序


def test_old_articles_and_blank_authors_are_excluded(session) -> None:
    """窗口外的不算;作者名为空的算不出"发布者",不该混进来。"""
    _post(session, "甲", "https://pan.quark.cn/s/D", 999, days_ago=30)
    _post(session, "", "https://pan.quark.cn/s/D", 888)
    assert rh.wechat_resource_heat(session, 1, days=7) == []


def test_summary_line_when_nothing_to_aggregate(session) -> None:
    """没有可聚合的资源时说清楚**为什么**(需要有盘链的文章),而不是给个空字符串。"""
    assert "需要文章里带盘链" in rh.summary_line([])
