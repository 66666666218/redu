"""选题复盘周报单测(v2.9.0):执行概况/扩散榜/金矿/行动提示。"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db import models  # noqa: F401
from app.db.models import HotspotSuggestion, WechatArticle, WechatPanLink


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    yield db
    db.close()


def test_weekly_review_sections(session) -> None:
    from app.services.weekly_review import build_weekly_review

    now = datetime.now()
    # 已结算建议(扩散 9 / 0 各一条)
    session.add_all([
        HotspotSuggestion(user_id=1, keyword="花少2人格测试", kind="llm", growth=0,
                          acted=True, acted_at=now - timedelta(days=3),
                          settled_at=now - timedelta(days=2), repost_gain=9, created_at=now - timedelta(days=4)),
        HotspotSuggestion(user_id=1, keyword="普通热点", kind="llm", growth=0,
                          acted=True, acted_at=now - timedelta(days=3),
                          settled_at=now - timedelta(days=2), repost_gain=0, created_at=now - timedelta(days=4)),
        HotspotSuggestion(user_id=1, keyword="未执行的", kind="llm", growth=0, created_at=now - timedelta(days=2)),
    ])
    # 金矿:三号同发
    for i, author in enumerate(("号A", "号B", "号C")):
        a = WechatArticle(user_id=1, title=f"人格测试入口{i}", author=author,
                          url=f"https://mp.weixin.qq.com/s/g{i}",
                          pan_urls="https://pan.quark.cn/s/gold", created_at=now - timedelta(days=1))
        session.add(a)
        session.flush()
        session.add(WechatPanLink(user_id=1, article_id=a.id, pan_url="https://pan.quark.cn/s/gold"))
    session.commit()

    text = build_weekly_review(session, 1, days=7)
    assert "【执行概况】" in text and "建议 3 条" in text
    assert "盘链扩散 +9" in text                      # 扩散榜
    assert "【同行共振金矿】" in text and "×3号" in text
    assert "扩散为 0" in text                          # 行动提示(降权观察)
    # 无数据用户不炸
    assert "【执行概况】" in build_weekly_review(session, 99, days=7)
