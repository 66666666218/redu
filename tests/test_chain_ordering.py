"""跨链先后台账单测(2026-10-07)。

**这个模块最怕的不是漏配,是误配** —— 把两份不相干的资源并成一份,于是"谁先谁后"
被算成一个不存在的先后(结论错得看不出来)。所以下面每条"不许并"的用例
都对应一个**实测踩到的**误并,不是假想的:

  · 纯数字当年份身份 ⇒ 「2027公考资料合集」并上了「2027最新行测5000题」(只因都写 2027);
  · 一个共用拉丁词就算命中 ⇒ 「ForgeTax游戏下载」并上了「友情粉碎机PEAK」(只因都带 steam);
  · 盘链 URL 进身份 ⇒ 人人都有 `panquarkcn`,两条不相干的资源互相拉近。
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
from app.db.models import (  # noqa: E402
    DouyinLead, User, WechatArticle, WechatPanLink, XunleiGroupShare,
)
from app.services import chain_ordering as co  # noqa: E402

NOW = datetime.now()
_SEQ = [0]


def _row(chain: str, name: str, hours_ago: float) -> co._Row:
    return co._Row(chain=chain, name=name, ts=NOW - timedelta(hours=hours_ago),
                   detail="test")


@pytest.fixture
def session():
    """内存库 + 一个启用用户。放在模块级:TestReport 与 TestTick 都要用。"""
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)()
    db.add(User(id=1, username="u1", email="u1@b.c", password_hash="x", enabled=True))
    db.commit()
    yield db
    db.close()


class TestIdentity:
    """★ 身份判定 —— 该并的必须并,不该并的**一条也不许并**。"""

    def test_短名能配进长标题(self) -> None:
        """跨链最常见的形态:抖音写一整句、群里只写资源名。"""
        a = co._grams(co._norm("都快点去看《高性价比人生指南》！GitHub开源20天"))
        b = co._grams(co._norm("高性价比人生指南"))
        assert co.containment(a, b) >= co.CONTAIN

    def test_dice会漏而包含度不会(self) -> None:
        """⚠️ 这条是"为什么不用现成的 `dice_similarity`"的证据,钉住它免得被改回去。"""
        from app.services.events import dice_similarity

        long_t = co._norm("都快点去看《高性价比人生指南》!GitHub开源20天,直接飙到近2万Star")
        short = co._norm("高性价比人生指南")
        assert dice_similarity(long_t, short) < 0.5          # 长度差把 Dice 压下去了
        assert co.containment(co._grams(long_t), co._grams(short)) == 1.0

    def test_emoji不影响配对(self) -> None:
        """迅雷群的资源名带「🔥🔥」「📤」是常态(库里真实存在)。"""
        assert co._norm("🔥🔥高性价比人生指南") == co._norm("高性价比人生指南")
        assert co._norm("宝可梦羁绊之旅📤") == co._norm("宝可梦羁绊之旅")

    def test_纯数字年份不算身份(self) -> None:
        """⚠️ 实测误并:两份毫无关系的资源只因都写着 2027 就被判成一回事。"""
        a, b = "2027公考资料合集（全网最新）", "2027最新行测5000题+申论100题 PDF电子版"
        assert co._latin_tokens(a) == set()                  # 年份不该进词集
        assert co._latin_tokens("pdf") == set()              # 通用尾巴也不算
        assert co.containment(co._grams(co._norm(a)), co._grams(co._norm(b))) < co.CONTAIN

    def test_共用一个泛词不算同一份资源(self) -> None:
        """⚠️ 实测误并:两条游戏只因都带 `steam` 就并成一组。"""
        a = co._latin_tokens("ForgeTax游戏下载 forgetax铸剑大师下载 #Forg... steam")
        b = co._latin_tokens("友情粉碎机 PEAK 免费! Steam特别好评的Q版攀岩合作游戏")
        assert co.latin_similarity(a, b) < co.CONTAIN

    def test_词集几乎相同就算同一份(self) -> None:
        """而真的同一份(只差大小写/标点),拉丁词集应当**几乎相等**。"""
        a = co._latin_tokens("forgetax（铸剑纳贡）防止河蟹")
        b = co._latin_tokens("铸剑纳贡（ForgeTax）")
        assert co.latin_similarity(a, b) >= co.CONTAIN

    def test_盘链URL不进身份(self) -> None:
        """⚠️ 实测误并根源:标题里带整条盘链,而 `pan.quark.cn/s/` 是**人人都有的**前缀。"""
        n = co._norm("《高性价比人生指南》夸克：https://pan.quark.cn/s/a2f2c0aacbf4")
        assert "quark" not in n and "http" not in n
        assert n.startswith("高性价比人生指南")

    def test_太短的名字不参与匹配(self) -> None:
        rows = [_row("抖音", "警笛", 1), _row("迅雷群", "警灯", 2)]
        assert co.group_by_resource(rows) == []


class TestGrouping:
    def test_可变代表名会漂移而固定种子不会(self) -> None:
        """⚠️ 实测误并:「查岗照片」与「安卓冷门软件」(containment **0.000**)
        因为"代表名"随组变大而更换,被**链式**并进了同一组。固定种子后不可能发生。"""
        rows = [_row("公众号", "300张超全版国庆假期应付对象查岗照片", 90),
                _row("公众号", "安卓系统有哪些冷门但逆天的手机软件", 80)]
        groups = co.group_by_resource(rows)
        assert len(groups) == 2

    def test_跨链同名并成一组(self) -> None:
        rows = [_row("抖音", "【安卓/电脑】《精灵宝可梦：羁绊之旅》非常优秀的同人游戏", 30),
                _row("迅雷群", "宝可梦羁绊之旅📤", 20)]
        groups = co.group_by_resource(rows)
        assert len(groups) == 1 and len(groups[0]["members"]) == 2


class TestReport:
    def _seed_douyin(self, db, title: str, hours_ago: float) -> None:
        _SEQ[0] += 1
        db.add(DouyinLead(user_id=1, aweme_id=f"a{_SEQ[0]}",
                          title=title, kind="share",
                          publish_at=NOW - timedelta(hours=hours_ago),
                          found_at=NOW))
        db.commit()

    def _seed_xunlei_group(self, db, title: str, hours_ago: float) -> None:
        _SEQ[0] += 1
        db.add(XunleiGroupShare(user_id=1, group_id="g1", share_id=f"s{_SEQ[0]}",
                                title=title, msg_time=NOW - timedelta(hours=hours_ago),
                                synced_at=NOW))
        db.commit()

    def _seed_wechat(self, db, title: str, hours_ago: float,
                     publish: bool = True) -> None:
        art = WechatArticle(user_id=1, title=title,
                            publish_at=(NOW - timedelta(hours=hours_ago)) if publish else None,
                            created_at=NOW - timedelta(hours=hours_ago))
        db.add(art)
        db.commit()
        db.add(WechatPanLink(user_id=1, article_id=art.id, pan_url=f"http://p/{art.id}",
                             created_at=art.created_at))
        db.commit()

    def test_谁先谁后与滞后算得对(self, session) -> None:
        self._seed_douyin(session, "《精灵宝可梦：羁绊之旅》同人游戏", 30)
        self._seed_xunlei_group(session, "宝可梦羁绊之旅📤", 10)
        rep = co.ordering_report(session, 1, days=90)
        g = rep["groups"][0]
        assert g["order"] == ["抖音", "迅雷群"]
        assert g["lag_h"] == pytest.approx(20.0, abs=0.1)
        assert rep["first_counts"] == {"抖音": 1}

    def test_单链资源不进报告(self, session) -> None:
        """只在一处出现的资源回答不了"谁先",不该占位。"""
        self._seed_douyin(session, "从来没人发过的资源", 5)
        assert co.ordering_report(session, 1, days=90)["groups"] == []

    def test_公众号按发布时间而不是入库时间(self, session) -> None:
        """⚠️ 入库时刻是"我们什么时候同步到",拿它当先后会把结论整体推后。"""
        art = WechatArticle(user_id=1, title="宝可梦羁绊之旅",
                            publish_at=NOW - timedelta(hours=50),
                            created_at=NOW - timedelta(hours=1))
        session.add(art)
        session.commit()
        session.add(WechatPanLink(user_id=1, article_id=art.id, pan_url="http://p/9",
                                  created_at=art.created_at))
        session.commit()
        self._seed_xunlei_group(session, "宝可梦羁绊之旅📤", 10)
        g = co.ordering_report(session, 1, days=90)["groups"][0]
        assert g["order"][0] == "公众号" and g["lag_h"] >= 39.0

    def test_同链同名只留最早(self, session) -> None:
        """同一份资源在某链被发了 20 次,不该撑出 20 条样本。"""
        for h in (5, 10, 40):
            self._seed_douyin(session, "宝可梦羁绊之旅", h)     # 同一标题、不同时间
        self._seed_xunlei_group(session, "宝可梦羁绊之旅📤", 1)
        assert co.ordering_report(session, 1, days=90)["ledger_n"] == 2

    def test_窗口外的发布时间不许混进来(self, session) -> None:
        """⚠️ 实测踩到:老文章**今天才被同步进来**,带着几周前的发布时间混进 14 天窗口
        —— 于是"公众号最先"被算得**虚高**(14 天窗口里冒出 `公众号 2026-08-05`)。
        筛选与排序必须同一个量(`coalesce(发布时间, 入库时间)`)。"""
        art = WechatArticle(user_id=1, title="宝可梦羁绊之旅",
                            publish_at=NOW - timedelta(days=40),   # 窗口外
                            created_at=NOW)                        # 今天才入库
        session.add(art)
        session.commit()
        session.add(WechatPanLink(user_id=1, article_id=art.id, pan_url="http://p/8",
                                  created_at=art.created_at))
        session.commit()
        self._seed_xunlei_group(session, "宝可梦羁绊之旅📤", 2)
        rep = co.ordering_report(session, 1, days=14)
        assert rep["ledger_n"] == 1, "窗口外的老发布时间混进来了"

    def test_左截断提示必须打出来(self, session) -> None:
        """⚠️ 只报"谁最先"而不说各链历史长度不同,会让人得出**错的结论**
        (公众号收得早 ⇒ 天然更容易当最先)。这条钉住那行提示,别被删掉。"""
        self._seed_douyin(session, "宝可梦羁绊之旅", 3)
        self._seed_xunlei_group(session, "宝可梦羁绊之旅📤", 2)
        rep = co.ordering_report(session, 1, days=90)
        assert rep["truncation_note"] == co.TRUNCATION_NOTE
        assert "左截断" in co.TRUNCATION_NOTE
        assert set(rep["span"]) == {"抖音", "迅雷群"}     # 各链数据起止要能看出来

    def test_读不到的表不留空结论(self, session) -> None:
        """一条链都没有数据时,报告应当说"没有跨链资源",而不是崩掉。"""
        assert co.ordering_report(session, 1, days=90)["ledger_n"] == 0


class TestTick:
    """定时推送。⚠️ 它要**自己先说清误配风险**,不能只报数字。"""

    def _seed(self, db) -> None:
        # ⚠️ 用户由 `session` fixture 建好,**这里别再插一遍**(id 会撞唯一约束)
        db.add(DouyinLead(user_id=1, aweme_id="a1", title="宝可梦羁绊之旅", kind="share",
                          publish_at=NOW - timedelta(hours=30), found_at=NOW))
        db.add(XunleiGroupShare(user_id=1, group_id="g1", share_id="s1",
                                title="宝可梦羁绊之旅📤", msg_time=NOW - timedelta(hours=2),
                                synced_at=NOW))
        db.commit()

    def test_没配管理群就不推也不崩(self, monkeypatch) -> None:
        monkeypatch.setattr("app.services.feishu_client.webhook_for", lambda *a, **k: "")
        assert co.chain_ordering_tick() == 0

    def test_推的正文里必须带左截断提醒(self, session, monkeypatch) -> None:
        """只报"谁最先"而不提醒"各链历史长度不同",会让人下**错结论**。"""
        self._seed(session)
        sent: dict = {}

        class _Hook:
            def __init__(self, *a, **k):
                pass

            def send(self, text: str) -> bool:
                sent["text"] = text
                return True

        class _S:
            feishu_secret = ""

        monkeypatch.setattr("app.services.feishu_client.webhook_for", lambda *a, **k: "hook")
        monkeypatch.setattr("app.services.feishu_client.FeishuClient", _Hook)
        monkeypatch.setattr("app.db.get_session_local", lambda: (lambda: session))

        assert co.chain_ordering_tick(settings=_S()) == 1
        assert "左截断" in sent["text"]
        assert "抖音" in sent["text"] and "迅雷群" in sent["text"]

    def test_周卡三块必须都在(self, session, monkeypatch) -> None:
        """★ 集成:台账 / 交付看板 / 调度建议三块都要在卡上。

        ⚠️ 为什么单独钉一条:交付与调度两块各自有 `try` 兜底(一块挂掉不许带走上限),
        而**兜底会把 bug 变成一行小字** —— 实测就撞过一次(`recommend_cadence` 的关键字
        参数改了名,卡照样发出去,只在末尾多一句"交付看板本轮渲染失败")。
        没有这条断言,那种"静默降级"能一路混到线上。
        """
        self._seed(session)
        sent: dict = {}

        class _Hook:
            def __init__(self, *a, **k):
                pass

            def send(self, text: str) -> bool:
                sent["text"] = text
                return True

        class _S:
            feishu_secret = ""

        monkeypatch.setattr("app.services.feishu_client.webhook_for", lambda *a, **k: "hook")
        monkeypatch.setattr("app.services.feishu_client.FeishuClient", _Hook)
        monkeypatch.setattr("app.db.get_session_local", lambda: (lambda: session))

        assert co.chain_ordering_tick(settings=_S()) == 1
        t = sent["text"]
        assert "交付看板" in t, "交付那块没渲染出来"
        assert "调度建议" in t, "调度建议那块没渲染出来"
        assert "渲染失败" not in t, f"有块降级了:\n{t[-300:]}"
