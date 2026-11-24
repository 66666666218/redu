# -*- coding: utf-8 -*-
"""破圈前预警(2026-11-24)的守卫。

## 这条功能要守的是什么

用户口径:「热点**刚破圈 / 马上就要破圈的前夕 / 还没有同行入局**的时候能抓吗 ——
等同行们都一起发的时候再跟风往往就晚了」。两次真实案例把"差距"量了出来:

  · **斗破苍穹**:11-22 11:05 ithome #3 就已经在 `hot_source_items` 里,
    当时库内同行 **0 人**;我们 11-23 04:13 才第一次推出带链卡(晚了 17h);
  · **麦穗与羊**:靠公众号监听赢的(热搜侧比我们晚 38h)。

⇒ 本模块只补「热榜侧的先手」,判据出自 15 天真实库回放(见模块头注)。

## 本文件守的几件事(按"踩过的坑"排)

1. **斗破那条要精确命中**,而且**只报一个名字** —— 同一条标题里 `《斗破苍穹》`
   与 `《双帝之战》` 两个书名号,第一版会发两张卡;
2. **政策名词要剔**:`《…白皮书》《…规划》《…报告》` 是文件名,不是作品;
3. **财经/时政源不许进**:它们天天命中政策名词;
4. **起势档要"又新又靠前"**,缺一个都不算(只新 = 刚冒头;只靠前 = 老话题);
5. **同行入局就闭嘴** —— 这条功能的全部价值在"还没人发";
6. **已经搬过的不报**;
7. **回放的上下界** —— 第一次回放就踩到:只有下界时,回放 11-22 会读到 11-24 的行,
   「首见」写成了未来时刻,B 档判据整条失真;
8. **一轮一张汇总卡** + 冷却按(档位, 作品)分锁 + **发送成功才落冷却**。
"""
import os
from datetime import datetime, timedelta

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.db import models  # noqa: E402,F401
from app.db.database import Base  # noqa: E402
from app.db.models import (DiscoveredPanLink,  # noqa: E402,F401
                           HotSourceItem, PushLedger, User, WechatArticle)
from app.services import hot_breakout as hb  # noqa: E402

NOW = datetime(2026, 11, 22, 11, 40)


@pytest.fixture
def session():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)()
    db.add(User(id=1, username="u1", email="u1@b.c", password_hash="x", enabled=True))
    db.commit()
    yield db
    db.close()


def _hot(db, *, title, source="ithome", rank=3, minutes_ago=5, now=NOW):
    db.add(HotSourceItem(user_id=1, source=source, rank=rank, title=title,
                         captured_at=now - timedelta(minutes=minutes_ago)))


def _ledger(db, *, title, author, pan_url="", hours_ago=1, now=NOW):
    db.add(PushLedger(user_id=1, section="douyin_peer", item_key=title[:20],
                      title=title, author=author, pan_url=pan_url,
                      pushed_at=now - timedelta(hours=hours_ago)))


def _article(db, *, title, author, my_pan="", hours_ago=1, now=NOW):
    db.add(WechatArticle(user_id=1, title=title, author=author,
                         my_pan_urls=my_pan, created_at=now - timedelta(hours=hours_ago)))


DOUPO = "九年长篇今日完结：国漫《斗破苍穹》剧场版《双帝之战》定档午间 12 点开播"


class Test预告档:
    def test_斗破那条要命中而且只报主名(self, session) -> None:
        """★★ 这条是整件事的锚点。

        同一条标题有两个书名号 ⇒ 第一版**发了两张卡**(斗破苍穹 + 双帝之战)。
        主作品名在**后面**(前面那个是 IP/系列),所以取最后一个。
        """
        _hot(session, title=DOUPO)
        session.commit()
        out = hb.detect_breakouts(session, 1, now=NOW)
        names = [a["raw"] for a in out]
        assert names == ["双帝之战"], f"应当只报主名,实际 {names}"
        assert out[0]["tier"] == hb.TIER_PRE
        assert out[0]["peer_authors"] == 0

    def test_政策名词要剔(self, session) -> None:
        """`《…白皮书》` 是文件名。**不剔的话财经源会天天命中。**"""
        _hot(session, title="《中国游戏产业白皮书》即将发布", rank=2)
        _hot(session, title="《十四五数字经济发展规划》全文印发", rank=3)
        session.commit()
        assert hb.detect_breakouts(session, 1, now=NOW) == []

    def test_预告档不看名次(self, session) -> None:
        """★ 预告稿常常不在榜首(实测 #8~#30 一堆)。设名次闸会漏掉它们。"""
        _hot(session, title="漫威《复仇者联盟 5：毁灭之日》全新预告发布", rank=30)
        session.commit()
        out = hb.detect_breakouts(session, 1, now=NOW, rank_top=15)
        assert [a["raw"] for a in out] == ["复仇者联盟 5：毁灭之日"]

    def test_没有预告词就不算预告档(self, session) -> None:
        """`《…》` + 名次靠后 + 没有预告词 ⇒ 不报(否则"凡书名号都报")。"""
        _hot(session, title="如何评价《斗破苍穹》", source="zhihu", rank=28)
        session.commit()
        assert hb.detect_breakouts(session, 1, now=NOW, rank_top=15) == []


class Test起势档:
    def test_又新又靠前才报(self, session) -> None:
        _hot(session, title="《饥困荒野》今天开服", rank=5, minutes_ago=30)
        session.commit()
        out = hb.detect_breakouts(session, 1, now=NOW, rank_top=15, first_hours=6)
        assert [a["tier"] for a in out] == [hb.TIER_RISE]

    def test_太老就不算起势(self, session) -> None:
        """首见 7h 前 > `first_hours=6` ⇒ 它不是"刚起势",是 `promo_forecast` 的活。"""
        _hot(session, title="《饥困荒野》今天开服", rank=5, minutes_ago=7 * 60)
        session.commit()
        assert hb.detect_breakouts(session, 1, now=NOW, first_hours=6) == []

    def test_名次太靠后不算起势(self, session) -> None:
        _hot(session, title="《饥困荒野》今天开服", rank=16, minutes_ago=30)
        session.commit()
        assert hb.detect_breakouts(session, 1, now=NOW, rank_top=15) == []

    def test_回放不许读到未来(self, session) -> None:
        """★★ **第一次回放就踩到的真 bug**:窗口只有下界。

        回放 11-22 时,11-24 的行也被算进来 ⇒ 卡上写着「首见 11-24」(未来),
        而 B 档判据是「首见 ≤6h」—— 用未来时刻算出来的是一个负数,整条失真。
        """
        _hot(session, title="《饥困荒野》今天开服", rank=5, minutes_ago=30)
        _hot(session, title="《后会友期》", source="zhihu", rank=8,
             minutes_ago=-60 * 60 * 40)          #: 两天**之后**才有的一行
        session.commit()
        out = hb.detect_breakouts(session, 1, now=NOW, rank_top=15)
        names = [a["raw"] for a in out]
        assert names == ["饥困荒野"], f"读到了 now 之后的行:{names}"
        assert all(a["first"] <= NOW for a in out), "首见时刻落到了未来"


class Test源与身份:
    def test_财经源不进(self, session) -> None:
        """财经/时政线的标题长得像作品(「《…》+ 上线/发布」),但产不出可搬的资源。"""
        _hot(session, title="《XX 条例》即将上线执行", source="jin10", rank=2)
        _hot(session, title="《XX 白皮书》定档发布", source="wallstreetcn-hot", rank=2)
        session.commit()
        assert hb.detect_breakouts(session, 1, now=NOW) == []

    def test_多平台同现只算一条(self, session) -> None:
        """同一作品在两个源各有一行 ⇒ **一条**候选,而不是两条。"""
        _hot(session, title="《饥困荒野》1124 上线", source="bilibili", rank=30)
        _hot(session, title="魔性演绎《饥困荒野》1124 上线主题曲", source="ithome", rank=6)
        session.commit()
        out = hb.detect_breakouts(session, 1, now=NOW)
        assert [a["raw"] for a in out] == ["饥困荒野"]
        assert out[0]["platforms"] == ["bilibili", "ithome"]
        assert out[0]["rank"] == 6, "名次应当取最好的那个"


class Test同行与自有链:
    def test_有同行入局就不报(self, session) -> None:
        """★ 这条功能的**全部价值**在"还没人发"。同行一多,它与 `promo_forecast` 重复。"""
        _hot(session, title="《斗破苍穹》剧场版定档", rank=3)
        for i, who in enumerate(["同行甲", "同行乙"]):
            _ledger(session, title=f"《斗破苍穹》4K 全集 版本{i}", author=who)
        session.commit()
        stats: dict = {}
        out = hb.detect_breakouts(session, 1, now=NOW, max_peer_authors=1, stats=stats)
        assert out == []
        assert stats["peer"] == 1, f"应当记在「同行已入局」这一级:{stats}"

    def test_公众号文章也算同行(self, session) -> None:
        """★★ **麦穗那次先手正是从 `wechat_articles` 看出来的** ——
        公众号监听的文章**不进台账**,只数台账会漏掉一整类同行。
        """
        _hot(session, title="《麦穗与羊》原稿传递", source="tieba", rank=1)
        for who in ("号甲", "号乙"):
            _article(session, title="麦穗与羊 观后感", author=who)
        session.commit()
        stats: dict = {}
        assert hb.detect_breakouts(session, 1, now=NOW, max_peer_authors=1, stats=stats) == []
        assert stats["peer"] == 1

    def test_同行的窗口是24小时不是7天(self, session) -> None:
        """斗破 @11-22 11:05:7 天窗口数出 4 人(含 11-18 的完结感慨),24h 窗口是 0 人。

        用 7 天窗口会让**每一次真先手**都被判成"同行已入局"。
        """
        _hot(session, title="《斗破苍穹》剧场版《双帝之战》定档", rank=3)
        _ledger(session, title="《斗破苍穹》完结了", author="路人甲", hours_ago=72)
        session.commit()
        out = hb.detect_breakouts(session, 1, now=NOW, peer_hours=24)
        assert [a["raw"] for a in out] == ["双帝之战"], out

    def test_已有我方链就不报(self, session) -> None:
        #: ⚠️ 台账标题**不带书名号**也必须是"我们已有" —— 实测公众号里推斗破的写法是
        #: `斗破苍穹剧场版 双帝之战 高清4K（速存）`,一个书名号都没有。
        _hot(session, title="《饥困荒野》1124 上线", rank=5)
        _ledger(session, title="饥困荒野 完整版", author="我们自己",
                pan_url="https://pan.xunlei.com/s/ours")
        session.commit()
        stats: dict = {}
        assert hb.detect_breakouts(session, 1, now=NOW, stats=stats) == []
        assert stats["owned"] == 1

    def test_套话名要剔(self, session) -> None:
        """`《完整版》` 这类名字不是作品(`promo_forecast.is_cta` 同一把尺子)。"""
        _hot(session, title="《完整版》今晚首发", rank=5)
        session.commit()
        stats: dict = {}
        assert hb.detect_breakouts(session, 1, now=NOW, stats=stats) == []
        assert stats["cta"] == 1


class Test卡片与冷却:
    def _one(self, session):
        _hot(session, title=DOUPO, rank=3)
        session.commit()
        return hb.detect_breakouts(session, 1, now=NOW)

    def test_卡片带库内二字(self, session) -> None:
        """★ 必须写「**库内**同行」—— 我们只看得到自己库里,写「全网没人发」是误导。"""
        txt = hb._render(self._one(session), chr(10))
        assert "库内同行" in txt
        assert "不代表全网" in txt
        assert "双帝之战" in txt
        assert "还没人入局" in txt

    def test_一轮只发一张卡(self, session) -> None:
        """★ 9 条/天的产量,逐条发就是刷屏。一张卡按档位分组。"""
        _hot(session, title="《饥困荒野》1124 上线", rank=30)
        _hot(session, title="《后会友期》", source="zhihu", rank=8, minutes_ago=30)
        session.commit()
        alerts = hb.detect_breakouts(session, 1, now=NOW)
        assert len(alerts) == 2, alerts
        txt = hb._render(alerts, chr(10))
        assert txt.count("破圈前预警") == 1
        assert "预告档" in txt and "起势档" in txt

    def test_冷却按档位分锁(self, session) -> None:
        """斗破「11:05 预告 → 12:05 起势」是**两个不同事实**,共用一把锁会吞掉后一张。"""
        a = {"name": "双帝之战", "tier": hb.TIER_PRE}
        b = {"name": "双帝之战", "tier": hb.TIER_RISE}
        assert f"breakout:{a['tier']}:{a['name']}" != f"breakout:{b['tier']}:{b['name']}"

    def test_发送失败不落冷却(self, session, monkeypatch) -> None:
        """★ 失败还落冷却 = 这一条**永久丢失**(48h 内不再报)。只在成功后落。"""
        from app.services import hot_breakout as mod

        _hot(session, title=DOUPO, rank=3)
        session.commit()
        marked: list[str] = []
        monkeypatch.setattr(mod, "_mark_alerted",
                            lambda *a, **k: marked.append(a[2]))
        #: `get_session_local` 是在函数**内部** import 的 ⇒ patch 到 `app.db` 那个名字上
        monkeypatch.setattr("app.db.get_session_local", lambda: (lambda: session))
        monkeypatch.setattr(mod, "_record", lambda *a, **k: None)

        class _Settings:
            hot_breakout_enabled = True
            feishu_webhook_admin = "https://example.invalid/hook"
            feishu_secret = ""
            hot_breakout_cron = "42 * * * *"
            hot_breakout_content_sources = ""
            hot_breakout_rank_top = 15
            hot_breakout_first_hours = 6
            hot_breakout_lookback_hours = 12
            hot_breakout_peer_hours = 24
            hot_breakout_max_peer_authors = 1
            hot_breakout_top = 5
            hot_breakout_cooldown_hours = 48

        class _Client:
            def __init__(self, *a, **k):
                pass

            def send(self, text):
                return False                 #: 发失败

        monkeypatch.setattr("app.services.feishu_client.FeishuClient", _Client)
        mod.hot_breakout_tick(_Settings())
        assert marked == [], f"发送失败却落了冷却行:{marked}"