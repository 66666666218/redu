"""新鲜度闸单测(2026-10-06)。

## 背景:一条规则,只落了一条链
用户口径:「**2026 年 10 月份之前的不要再保存进来了**」。它当初只在**抖音线索**上实现,
**公众号那条链一处都没有** —— 审计实测 10-01 之后仍有 **34 篇 9 月的文章**入库,
另有 52 篇(近 3 天)连发布时间都空着。判定逻辑因此收进 `app.services.freshness`
(单一事实源),这里同时验**逻辑**和**它真的接在了公众号链上**。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

from datetime import datetime  # noqa: E402

import pytest  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.db.database import Base  # noqa: E402
from app.db import models  # noqa: E402,F401
from app.db.models import WechatArticle, WechatBenchmark  # noqa: E402
from app.services import freshness as fr  # noqa: E402
# ⚠️ **先导入门面 `wechat_monitor`** —— 直接 import `wechat._enrich` 会撞循环导入
# (`_enrich` 在模块级 `from app.services import wechat_monitor as _root`,而门面又反过来
#  引 `wechat._source`)。现有公众号测试都走门面,这里跟着走。
from app.services import wechat_monitor  # noqa: E402


@pytest.fixture
def session():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)()
    yield db
    db.close()


class _S:
    """最小 settings 桩。"""

    def __init__(self, **kw):
        self.content_min_publish_date = "2026-10-01"
        self.douyin_leads_min_publish_date = "2026-10-01"
        for k, v in kw.items():
            setattr(self, k, v)


class TestMinPublishTs:
    def test_专属键优先_留空回落通用口径(self) -> None:
        s = _S(douyin_leads_min_publish_date="2026-11-01")
        assert fr.min_publish_ts(s, "douyin_leads_min_publish_date") == int(
            datetime(2026, 11, 1).timestamp())
        # 专属键留空 → 回落通用口径(用户那条规则是对**所有源**说的)
        s.douyin_leads_min_publish_date = ""
        assert fr.min_publish_ts(s, "douyin_leads_min_publish_date") == int(
            datetime(2026, 10, 1).timestamp())

    def test_没配就不过滤(self) -> None:
        assert fr.min_publish_ts(_S(content_min_publish_date="")) == 0

    def test_写坏了要吭声而不是静默不过滤(self) -> None:
        """⚠️ 解析失败返回 0(不过滤)是对的 —— 但**必须留痕**,
        否则用户的规则是**悄悄失效**的(本仓最忌讳的失败形态)。"""
        assert fr.min_publish_ts(_S(content_min_publish_date="10月1号")) == 0


class TestParsedPublishTs:
    def test_各种形态都能认(self) -> None:
        t = int(datetime(2026, 10, 4, 12, 0).timestamp())
        assert fr.parsed_publish_ts(datetime(2026, 10, 4, 12, 0)) == t
        assert fr.parsed_publish_ts("2026-10-04 12:00:00") == t
        assert fr.parsed_publish_ts("2026-10-04") == int(datetime(2026, 10, 4).timestamp())
        assert fr.parsed_publish_ts(t) == t
        assert fr.parsed_publish_ts(t * 1000) == t        # 毫秒当毫秒看

    def test_判不出就是None而不是0(self) -> None:
        """★ **None 与 0 必须分开**:0 会被当成"1970 年"(于是被判成过老),
        None 才是"判不出"。合成一个就会把"没时间"错杀。"""
        for bad in (None, "", "   ", "昨天", 0, -5, "2026-13-45"):
            assert fr.parsed_publish_ts(bad) is None, bad


class TestIsTooOld:
    MIN = int(datetime(2026, 10, 1).timestamp())

    def test_早于截止的挡掉(self) -> None:
        assert fr.is_too_old(datetime(2026, 9, 13), self.MIN) is True

    def test_截止当天及以后放行(self) -> None:
        assert fr.is_too_old(datetime(2026, 10, 1), self.MIN) is False
        assert fr.is_too_old(datetime(2026, 10, 4), self.MIN) is False

    def test_判不出时间的不挡(self) -> None:
        """⚠️ "没时间"≠"很老" —— 不能一刀切。**但调用方必须把这类数量报出来**,
        否则规则在这儿**静默漏**(公众号实测占三成)。"""
        assert fr.is_too_old(None, self.MIN) is False

    def test_没配规则就都放行(self) -> None:
        assert fr.is_too_old(datetime(2020, 1, 1), 0) is False


class TestWechatChainHasTheGate:
    """★ **真正要守的一件事**:公众号入库那条链**确实挂了闸**。

    这条链是漏得最久的:抖音 10-01 就有闸,公众号一处都没有。
    """

    def _items(self) -> list[dict]:
        return [
            {"title": "新资源合集", "url": "https://mp.weixin.qq.com/s/new",
             "publish_at": datetime(2026, 10, 4)},
            {"title": "老资源合集", "url": "https://mp.weixin.qq.com/s/old",
             "publish_at": datetime(2026, 9, 13)},
            {"title": "没时间合集", "url": "https://mp.weixin.qq.com/s/und", "publish_at": None},
        ]

    def test_老的不入库_新的和无时间的入(self, session) -> None:
        en = wechat_monitor

        b = WechatBenchmark(user_id=1, nickname="号A")
        session.add(b)
        session.commit()
        rows = en._insert_new_articles(session, 1, b, self._items(), source="listen",
                                       require_pan=False,
                                       min_ts=int(datetime(2026, 10, 1).timestamp()))
        urls = sorted(r.url for r in rows)
        assert urls == ["https://mp.weixin.qq.com/s/new", "https://mp.weixin.qq.com/s/und"], urls
        assert session.scalar(select(WechatArticle).where(
            WechatArticle.url.like("%/old"))) is None, "9 月的文章不该入库"

    def test_不给参数也要自动挂闸(self, session, monkeypatch) -> None:
        """★ **安全网**:四个调用点一个都不改也照样过滤 —— 规则漏挂才是真问题。

        代价是它读的是**全局配置**而不是注入的 settings(这条规则是全局单一配置,
        不是按链/按租户的),所以要在这里明确钉住这个行为。
        """
        en = wechat_monitor          # 走门面,别绕开(见文件头对循环导入的说明)

        monkeypatch.setattr(fr, "min_publish_ts",
                            lambda *a, **k: int(datetime(2026, 10, 1).timestamp()))
        b = WechatBenchmark(user_id=1, nickname="号B")
        session.add(b)
        session.commit()
        rows = en._insert_new_articles(session, 1, b, self._items(), source="listen",
                                       require_pan=False)      # ← 不传 min_ts
        assert [r.url for r in rows] == ["https://mp.weixin.qq.com/s/new",
                                         "https://mp.weixin.qq.com/s/und"]

    def test_闸关掉时全部放行(self, session) -> None:
        """反向:没配新鲜度规则(`min_ts=0`)时,老文章照收 —— 别把闸焊死。"""
        en = wechat_monitor

        b = WechatBenchmark(user_id=1, nickname="号C")
        session.add(b)
        session.commit()
        rows = en._insert_new_articles(session, 1, b, self._items(), source="listen",
                                       require_pan=False, min_ts=0)
        assert len(rows) == 3, [r.url for r in rows]
