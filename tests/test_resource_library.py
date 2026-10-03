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


# ---------------------------------------------------------------- 并入"公开平台发现"的链(2026-10-02)

def _mk_discovered(session, title, pan_url, our="", platform="zhihu", author="作者甲", days_ago=0):
    from app.db.models import DiscoveredPanLink

    session.add(DiscoveredPanLink(user_id=1, platform=platform, origin_url=pan_url,
                                  title=title, author=author, our_url=our,
                                  status="ok" if our else "pending",
                                  found_at=datetime.now() - timedelta(days=days_ago)))
    session.commit()


def test_search_resources_includes_discovered_links(session) -> None:
    """**公开平台发现的链要能在资源库里搜到** —— 用户口径:"加进资源库"。

    它的键(别人的原链)与公众号链是同一类,所以能天然合并展示;来源要标出来,
    别和公众号的"多少号同发"混为一谈。
    """
    from app.services.resource_library import search_resources

    _mk_discovered(session, "最近爆火的花少2人格测试", "https://pan.quark.cn/s/AAA",
                   our="https://pan.quark.cn/s/OUR")
    out = search_resources(session, 1, "花少2")
    assert len(out) == 1
    assert out[0]["pan_url"] == "https://pan.quark.cn/s/AAA"
    assert out[0]["source"] == "知乎" and out[0]["author"] == "作者甲"
    assert out[0]["my_link"] == "https://pan.quark.cn/s/OUR"     # 已有我方链要带出来


def test_search_resources_wechat_wins_on_same_url(session) -> None:
    """同一条链**两处都有**时只出一条,且以**公众号**为准(它带"多少号同发"这个更强信号)。"""
    from app.services.resource_library import search_resources

    _mk(session, "花少2人格测试", "号甲", "https://pan.quark.cn/s/SAME")
    _mk(session, "花少2人格测试", "号乙", "https://pan.quark.cn/s/SAME")
    _mk_discovered(session, "花少2人格测试", "https://pan.quark.cn/s/SAME")
    out = search_resources(session, 1, "花少2")
    assert len(out) == 1
    assert out[0]["accounts"] == 2 and out[0]["source"] == "公众号"


def test_resource_profile_falls_back_to_discovered(session) -> None:
    """画像也要认发现链(公众号里没有时)。"""
    from app.services.resource_library import resource_profile

    _mk_discovered(session, "某资源", "https://pan.quark.cn/s/BBB", our="OUR")
    p = resource_profile(session, 1, "https://pan.quark.cn/s/BBB")
    assert p and p["source"] == "知乎" and p["my_link"] == "OUR"


def test_library_summary_counts_discovered(session) -> None:
    """概览带上"发现"数,否则前端同屏两个数字会让人以为库变小了。"""
    from app.services.resource_library import library_summary

    _mk(session, "甲资源", "号甲", "https://pan.quark.cn/s/W1")
    _mk_discovered(session, "乙资源", "https://pan.quark.cn/s/D1")
    s = library_summary(session, 1)
    assert s["total_links"] == 1 and s["discovered"] == 1


def test_my_link_strips_trailing_text(session) -> None:
    """⚠️ 历史数据里我方链带过尾巴(`…fa (自分享)`)—— 整行拿去当链接**点不开**,
    取用端必须抠出干净的 URL(2026-10-02 跨平台热度卡片里发现)。"""
    from app.services.resource_library import _clean_link, _my_link_of

    assert _clean_link("https://pan.quark.cn/s/abc (自分享)") == "https://pan.quark.cn/s/abc"
    assert _clean_link("https://pan.quark.cn/s/abc（自分享）") == "https://pan.quark.cn/s/abc"
    assert _clean_link("不是链接") == ""

    _mk(session, "某资源", "号甲", "https://pan.quark.cn/s/abc",
        my="https://pan.quark.cn/s/abc (自分享)")
    assert _my_link_of(session, 1, "https://pan.quark.cn/s/abc") == "https://pan.quark.cn/s/abc"


# ---------------------------------------------------------------- 迅雷盘资源出口(2026-10-03)

def _mk_xl(session, name, share_url, days_ago=1, parent="最全文件"):
    from app.db.models import XunleiResource

    session.add(XunleiResource(user_id=1, fid=f"fid-{name}", name=name,
                               kind="drive#folder", parent_name=parent,
                               share_url=share_url, pass_code="abcd",
                               synced_at=datetime.now() - timedelta(days=days_ago)))


def test_xunlei_resources_are_searchable(session) -> None:
    """⚠️ **我方迅雷盘里的资源必须有出口**(2026-10-03 修)。

    此前 `xunlei_resources` **写入 2 处(口令转存 + 扫盘)、读取 0 处** —— 抖音口令搬进来的、
    扫盘扫出来的资源,**进了库谁也看不见、presence 也匹配不到**,等于白搬。
    """
    from app.services.resource_library import search_resources

    _mk_xl(session, "手机警报器", "https://pan.xunlei.com/s/OUR1?pwd=abcd")
    session.commit()
    rs = search_resources(session, 1, "手机警报器", days=365, limit=10)
    assert len(rs) == 1
    assert rs[0]["source"] == "迅雷盘"
    # 盘里的东西本来就是我们的 → pan_url 与 my_link 同一条链(与公众号那种"别人的原链"不同)
    assert rs[0]["my_link"] == rs[0]["pan_url"] == "https://pan.xunlei.com/s/OUR1?pwd=abcd"


def test_xunlei_resources_skips_rows_without_share_url(session) -> None:
    """没有分享链的行**不进库** —— 库的用途是"给出可用链",没链的放进来只会干扰。"""
    from app.services.resource_library import search_resources

    _mk_xl(session, "没链的资源", "")
    session.commit()
    assert search_resources(session, 1, "没链的资源", days=365, limit=10) == []


def test_search_dedupes_across_sources(session) -> None:
    """同一条链既在公众号、又在我们迅雷盘里 → **只出一次**(公众号优先,它带"多少号同发")。"""
    from app.services.resource_library import search_resources

    url = "https://pan.xunlei.com/s/SAME?pwd=x"
    _mk(session, "某资源分享", "号甲", url)
    _mk_xl(session, "某资源分享", url)
    session.commit()
    rs = search_resources(session, 1, "某资源分享", days=365, limit=10)
    assert len(rs) == 1 and rs[0]["source"] == "公众号"
