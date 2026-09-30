"""资源库单测(v2.4.0):检索/共振榜/画像/我方链检测。"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db import models  # noqa: F401
from app.db.models import WechatArticle, WechatPanLink


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    yield db
    db.close()


def _mk(session, title, author, pan_url, days_ago=1, my=""):
    a = WechatArticle(user_id=1, title=title, author=author, url=f"https://mp.weixin.qq.com/s/{title[:6]}",
                      pan_urls=pan_url, my_pan_urls=my,
                      created_at=datetime.now() - timedelta(days=days_ago))
    session.add(a)
    session.flush()
    session.add(WechatPanLink(user_id=1, article_id=a.id, pan_url=pan_url))
    session.commit()
    return a


def test_search_and_resonance(session) -> None:
    from app.services.resource_library import resonance_resources, search_resources

    # 同一链被 3 个号同发(金矿) + 一条单号
    _mk(session, "花少2人格测试入口", "号A", "https://pan.quark.cn/s/gold", my="https://pan.quark.cn/s/mine1")
    _mk(session, "花少2测试最新链接", "号B", "https://pan.quark.cn/s/gold")
    _mk(session, "花少2测试直达", "号C", "https://pan.quark.cn/s/gold")
    _mk(session, "无关资源", "号D", "https://pan.quark.cn/s/other")

    rows = search_resources(session, 1, "花少")
    assert len(rows) == 1  # 聚合到链级
    r = rows[0]
    assert r["accounts"] == 3 and r["pan_type"] == "夸克"
    assert "mine1" in r["my_link"]  # 我方链已复用到位

    hot = resonance_resources(session, 1, days=30, min_accounts=2)
    assert len(hot) == 1 and hot[0]["accounts"] == 3
    assert resonance_resources(session, 1, days=30, min_accounts=4) == []

    # 过短查询词不检索(防噪声)
    assert search_resources(session, 1, "花") == []


def test_resource_profile_and_summary(session) -> None:
    from app.services.resource_library import library_summary, resource_profile

    _mk(session, "资源一", "号A", "https://pan.baidu.com/s/xyz")
    p = resource_profile(session, 1, "https://pan.baidu.com/s/xyz")
    assert p and p["pan_type"] == "百度" and p["accounts"] == 1
    assert resource_profile(session, 1, "https://pan.quark.cn/s/none") is None
    s = library_summary(session, 1)
    assert s["total_links"] == 1


def test_detect_viral_resources(session) -> None:
    """资源级爆款检测:近窗口内多号新同发的链才算(全历史老共振不算);号数排序。"""
    from app.services.resource_library import detect_viral_resources

    # 近 24h 三号同发(爆款) + 30 天前的三号同发(老共振,窗口外)
    _mk(session, "花少2人格测试入口", "号A", "https://pan.quark.cn/s/viral", days_ago=0)
    _mk(session, "花少2测试最新", "号B", "https://pan.quark.cn/s/viral", days_ago=0)
    _mk(session, "花少2测试直达", "号C", "https://pan.quark.cn/s/viral", days_ago=0)
    _mk(session, "老资源", "号A", "https://pan.quark.cn/s/old", days_ago=30)
    _mk(session, "老资源2", "号B", "https://pan.quark.cn/s/old", days_ago=30)
    _mk(session, "老资源3", "号C", "https://pan.quark.cn/s/old", days_ago=30)

    v = detect_viral_resources(session, 1, hours=24, min_accounts=3)
    assert len(v) == 1 and v[0]["accounts"] == 3 and "viral" in v[0]["pan_url"]
    # 窗口对齐:全历史视角(90 天)两条都算
    v2 = detect_viral_resources(session, 1, hours=24 * 90, min_accounts=3)
    assert len(v2) == 2
