"""抖音取词**并进其它源**(2026-10-07,用户口径 a「一视同仁」)。

## 用户报的现象
「冒险岛国际服」「派出所模拟器」**从迅雷群转存进来、卡也推了**,但**抖音从不去搜它们**。

## 查出来的根因
```
资源库「检索」收 4 个源:公众号 + 公开平台发现 + 我方迅雷盘 + 迅雷群转存   ✓ 全
资源库「共振榜」**只统计公众号**(门槛"被 ≥2 个号同发")
而 **抖音取词用的正是「共振榜」**
```
⇒ 迅雷群 / 小红书 / B站 / 贴吧 / 知乎 发现的资源**永远进不了抖音的搜索词池**。
**同一个"资源库",两处口径不一致。**

## 这条测试守两件事
1. 其它源的资源名**能进池子**(否则用户那道现象原样复发);
2. **两个源交替取** —— 先填满共振榜再补其它源的话,"并进来"等于没并
   (共振榜必然先占满 top 个名额)。**"交替"是这条修法的关键,不是实现细节。**
"""
import os
from datetime import datetime

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.db.database import Base  # noqa: E402
from app.db import models  # noqa: E402,F401
from app.db.models import DiscoveredPanLink, User, XunleiGroupShare  # noqa: E402
from app.services import cross_accounts as ca  # noqa: E402
from app.services import resource_library as rl  # noqa: E402


@pytest.fixture
def session():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)()
    db.add(User(id=1, username="u1", email="u1@b.c", password_hash="x", enabled=True))
    db.commit()
    yield db
    db.close()


class TestRecentSourceNames:
    def test_迅雷群和公开平台发现的都收(self, session) -> None:
        session.add(XunleiGroupShare(user_id=1, group_id="g", share_id="s1",
                                     title="冒险岛国际服", status="ok",
                                     synced_at=datetime.now()))
        session.add(DiscoveredPanLink(user_id=1, platform="tieba", title="派出所模拟器(手机+电脑）",
                                      origin_url="https://pan.xunlei.com/s/x",
                                      status="ok", found_at=datetime.now()))
        session.commit()
        names = rl.recent_source_names(session, 1)
        assert "冒险岛国际服" in names, names
        assert any("派出所模拟器" in n for n in names), names

    def test_取不到就返回空而不是抛(self, session, monkeypatch) -> None:
        """反向:某个源坏了**退回**另一个,别让取词整条崩。"""
        assert isinstance(rl.recent_source_names(session, 1), list)


class TestKeywordsMerge:
    # ⚠️ 假词必须 **>=4 字** —— `library_search_word` 会把"太短搜不出东西"的丢掉
    # (实测:我第一版用 `H1` 这种 2 字词,全被过滤,测试报 [] 而不是交替问题)。

    def _patch(self, monkeypatch, hot, others):
        monkeypatch.setattr("app.services.resource_library.resonance_resources",
                            lambda *a, **k: [{"titles": [t]} for t in hot])
        monkeypatch.setattr("app.services.resource_library.recent_source_names",
                            lambda *a, **k: list(others))

    def test_其它源的词进得了池子(self, session, monkeypatch) -> None:
        """★ 用户报的那两个资源必须能进来 —— 否则现象原样复发。"""
        self._patch(monkeypatch, hot=["公众号资源A"], others=["冒险岛国际服", "派出所模拟器"])
        assert "冒险岛国际服" in ca._keywords_from_library(session, 1, top=4)

    def test_两个源交替而不是一个填满(self, session, monkeypatch) -> None:
        """★★ **这条是修法的关键**:先填满共振榜再补其它源 = 等于没并。"""
        self._patch(monkeypatch, hot=["共振词甲号", "共振词乙号", "共振词丙号"],
                others=["其它词甲号", "其它词乙号", "其它词丙号"])
        kws = ca._keywords_from_library(session, 1, top=4)
        assert kws == ["共振词甲号", "其它词甲号", "共振词乙号", "其它词乙号"], (
            f"应当交替取(共振/其它 各占一半),实际 {kws} —— "
            f"如果前三个全是'共振词xx'就说明其它源又被挤掉了")

    def test_某一边没词也不空转(self, session, monkeypatch) -> None:
        """反向:一边为空时,另一边正常填满(不能因为"要交替"就只取一半)。"""
        self._patch(monkeypatch, hot=["共振词甲号", "共振词乙号", "共振词丙号"], others=[])
        assert ca._keywords_from_library(session, 1, top=3) == [
            "共振词甲号", "共振词乙号", "共振词丙号"]
        self._patch(monkeypatch, hot=[], others=["其它词甲号", "其它词乙号"])
        assert ca._keywords_from_library(session, 1, top=2) == ["其它词甲号", "其它词乙号"]
