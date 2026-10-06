"""跨平台共振榜单测(2026-10-07)。

## 为什么它不能沿用现成的 `resonance_resources`
那个数的是"同一**盘链**被几个**公众号**发过"。跨平台**不能拿盘链当身份**:
**每个推广号自己建分享链**(实测两张表的盘链**零交集**)。
⇒ 身份只能是**资源名**;而资源名有 20+ 种写法(见下),所以还要一层**核心名归一化**。
"""
import os
from datetime import datetime, timedelta

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.db.database import Base  # noqa: E402
from app.db import models  # noqa: E402,F401
from app.db.models import DiscoveredPanLink, User, WechatArticle, WechatPanLink  # noqa: E402
from app.services.resource_library import (  # noqa: E402
    core_resource_name, cross_platform_resonance,
)


@pytest.fixture
def session():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)()
    db.add(User(id=1, username="u1", email="u1@b.c", password_hash="x", enabled=True))
    db.commit()
    yield db
    db.close()


class TestCoreName:
    """★ **同一个资源的 20+ 种写法必须落到同一个身份上** —— 这是整条榜的地基。

    这些全是**库里真实存在**的标题(2026-10-07 取),不是编的。
    """

    def test_高性价比的六种写法归成一个(self) -> None:
        variants = ["高性价比人生指南pdf电子版", "高性价比人生指南 共338页",
                    "《高性价比人生指南》pdf/", "2026高性价比人生指南.", "github高性价比人生指南",
                    "高性价比人生指南pdf电子版共"]
        got = {core_resource_name(v) for v in variants}
        assert got == {"高性价比人生指南"}, f"没归一成一个身份:{got}"

    def test_投票那条的两种写法归成一个(self) -> None:
        assert (core_resource_name("时代峰峻喜欢的脸top9投票最新")
                == core_resource_name("时代峰峻喜欢的脸top9投票入口")
                == "时代峰峻喜欢的脸top9投票")

    def test_别把不同的资源并到一起(self) -> None:
        """⚠️ 反向:归一化**宁可少并也不能乱并** —— 把两份不同资源并成一条会直接误导选品。"""
        a = core_resource_name("高性价比人生指南pdf电子版")
        b = core_resource_name("霸王茶姬杯贴自定义入口")
        assert a != b and a and b

    def test_太短的不当身份(self) -> None:
        """不足 4 字指不到具体东西,退回原名而不是硬凑一个短键。"""
        assert core_resource_name("pdf") == "pdf"


def _wx(session, title: str, author: str, pan: str) -> None:
    a = WechatArticle(user_id=1, title=title, author=author, url=f"https://mp.weixin.qq.com/s/{author}",
                      source="listen", created_at=datetime.now() - timedelta(days=1))
    session.add(a)
    session.flush()
    session.add(WechatPanLink(user_id=1, article_id=a.id, pan_url=pan,
                              created_at=datetime.now() - timedelta(days=1)))


def _disc(session, title: str, platform: str, author: str, pan: str) -> None:
    session.add(DiscoveredPanLink(user_id=1, platform=platform, title=title, author=author,
                                  origin_url=pan, status="ok"))


class TestCrossPlatformResonance:
    def test_同一资源跨平台才上榜_并给出平台数与号数(self, session) -> None:
        # 同一个资源,三个平台、三种写法
        _wx(session, "高性价比人生指南 pdf电子版，共388页", "兰兰_天空", "https://pan.quark.cn/s/a1")
        _wx(session, "《高性价比人生指南》pdf电子版（可下载）", "小U奇幻之旅", "https://pan.quark.cn/s/a2")
        _disc(session, "高性价比人生指南pdf电子版", "weibo", "用户8377", "https://pan.baidu.com/s/b1")
        _disc(session, "高性价比人生指南 共338页 PDF电子版", "tieba", "某甲", "https://pan.baidu.com/s/b2")
        # 只在一个平台的资源**不该上榜**(min_platforms=2)
        _wx(session, "霸王茶姬杯贴自定义入口", "墨滴", "https://pan.quark.cn/s/c1")
        session.commit()

        rows = cross_platform_resonance(session, 1, days=90, min_platforms=2)
        names = [r["name"] for r in rows]
        assert names == ["高性价比人生指南"], names
        r = rows[0]
        assert r["platform_count"] == 3 and r["account_count"] == 4, r
        assert set(r["platforms"]) == {"公众号", "weibo", "tieba"}
        # 四种原始标题里有两条的**中间清洗结果本就同名**,所以合并数 ≥3 而不是硬等于 4 ——
        # 钉死具体数字会让这条测试变成"清洗器的快照",它一改就红,而红的原因跟共振无关。
        assert r["variant_count"] >= 3, "多种写法该被并成一条,而不是散成多条"

    def test_单平台的不上榜(self, session) -> None:
        _wx(session, "某个只在一处的资源合集", "甲", "https://pan.quark.cn/s/x")
        session.commit()
        assert cross_platform_resonance(session, 1, days=90, min_platforms=2) == []

    def test_洗不出名字的不凑数(self, session) -> None:
        """空标题/太泛的名字不该进榜(它们只会制造噪音)。"""
        _wx(session, "链接：【我用夸克网盘给你分享了", "甲", "https://pan.quark.cn/s/y")
        _disc(session, "我用夸克网盘给你分享了", "weibo", "乙", "https://pan.quark.cn/s/z")
        session.commit()
        for r in cross_platform_resonance(session, 1, days=90, min_platforms=2):
            assert r["name"], "空名字不该出现"


class TestPushCard:
    """★ **榜做出来没人看等于没做** —— 接进推送(2026-10-07,用户口径「每天推一次」)。"""

    def _patch_feishu(self, monkeypatch):
        import app.services.feishu_client as fc
        sent: dict = {}

        class _C:
            def __init__(self, hook, secret=None) -> None:
                sent["hook"] = hook

            def send_card(self, card):
                sent["card"] = card
                return True

        monkeypatch.setattr(fc, "FeishuClient", _C)
        return sent

    def _row(self, session, name: str, plats: list[str], authors: list[str]) -> None:
        for i, (p, a) in enumerate(zip(plats, authors)):
            if p == "公众号":
                _wx(session, f"{name} pdf电子版", a, f"https://pan.quark.cn/s/{name}{i}")
            else:
                _disc(session, f"{name} pdf电子版", p, a, f"https://pan.baidu.com/s/{name}{i}")
        session.commit()

    def test_推给多平台群且平台名是中文(self, session, monkeypatch) -> None:
        from app.services import resource_library as rl
        from config.settings import Settings

        self._row(session, "高性价比人生指南", ["公众号", "weibo", "tieba"],
                  ["甲", "乙", "丙"])
        sent = self._patch_feishu(monkeypatch)
        st = Settings(_env_file=None, is_dev=True)
        monkeypatch.setattr(st, "feishu_webhook_multiplatform", "https://hook/mp", raising=False)
        monkeypatch.setattr(st, "brand_name", "", raising=False)

        assert rl.push_cross_platform_resonance(session, 1, st, days=90) is True
        assert sent["hook"] == "https://hook/mp", "该推「多平台监控」群"
        body = str(sent["card"])
        assert "全平台共振榜" in body and "高性价比人生指南" in body
        # ⚠️ 平台列给人看,**不能是英文代号**(注意排序:中文排在 ASCII 之后)
        for cn in ("公众号", "微博", "贴吧"):
            assert cn in body, body[:400]
        assert "weibo" not in body and "tieba" not in body, body[:400]

    def test_没上榜就不发(self, session, monkeypatch) -> None:
        """反向:一条都没有时**别发空卡**(空卡只会训练人忽略这个群)。"""
        from app.services import resource_library as rl
        from config.settings import Settings

        sent = self._patch_feishu(monkeypatch)
        st = Settings(_env_file=None, is_dev=True)
        monkeypatch.setattr(st, "feishu_webhook_multiplatform", "https://hook/mp", raising=False)
        assert rl.push_cross_platform_resonance(session, 1, st, days=90) is False
        assert "card" not in sent

    def test_单人多平台不算共振(self, session, monkeypatch) -> None:
        """★ **矩阵号不是共振**:同一个人在 3 个平台发同一份资源,凑得出平台数 3,
        但"需求被验证过"这句话不成立 —— agent 的判据里尤其不能放过(`min_accounts=2`)。"""
        from app.services import resource_library as rl
        from config.settings import Settings

        self._row(session, "某个资源", ["公众号", "weibo", "tieba"], ["同一个人"] * 3)
        self._patch_feishu(monkeypatch)
        st = Settings(_env_file=None, is_dev=True)
        monkeypatch.setattr(st, "feishu_webhook_multiplatform", "https://hook/mp", raising=False)
        assert rl.push_cross_platform_resonance(session, 1, st, days=90) is False
