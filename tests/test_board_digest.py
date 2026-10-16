"""板块日结/周结单测(2026-10-16)。

用户口径四条(见 `board_digest` 头注):各板块客户群 / 每天 08:30 与周一 09:00 /
**只讲「有什么」** / 数据源是推送台账。这份测试就是**把这四条钉住**。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import json
from datetime import datetime, time, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import models  # noqa: F401
from app.db.database import Base
from app.db.models import SystemConfig, User
from app.services import board_digest as bd
from app.services import push_ledger as pl


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    db.add(User(id=1, username="u1", email="u1@b.c", password_hash="x", enabled=True))
    db.add(User(id=2, username="u2", email="u2@b.c", password_hash="x", enabled=True))
    db.commit()
    yield db
    db.close()


class _S:
    feishu_webhook = ""                       #: 主群留空 ⇒ "没群"这件事测得出来
    feishu_secret = ""
    brand_name = "念飞思雪"
    feishu_webhook_xiaohongshu = "https://example.com/xhs"
    feishu_webhook_kuaishou = "https://example.com/ks"
    board_digest_enabled = True
    board_digest_top = 8
    board_digest_boards = ""
    board_digest_weekly_cron = "3 9 * * 1"      #: 周一 = 周结日(那天不发日结)
    board_digest_daily_skip_on_weekly = True


def _yesterday() -> datetime:
    """窗口是**本地零点**;取昨天的中午 = 稳稳落在窗口里。"""
    return datetime.combine((datetime.now() - timedelta(days=1)).date(), time(12, 0))


def _seed(session, section, title, *, when=None, user_id=1, **kw):
    item = {"title": title}
    item.update(kw)
    return pl.record(session, user_id, section, [item], when=when or _yesterday())


class Test窗口:
    def test_日结看昨天整天(self) -> None:
        since, until, tag = bd.window_of("daily")
        today = datetime.now().date()
        assert until == datetime.combine(today, time.min), "到今天零点为止"
        assert since == until - timedelta(days=1), "整整一天"
        assert tag.startswith(today.isoformat()) and tag.endswith("daily")

    def test_周结看过去_7_天(self) -> None:
        since, until, tag = bd.window_of("weekly")
        assert (until - since).days == 7
        assert tag.endswith("weekly")

    def test_不认识的期名退回日结(self) -> None:
        """⚠️ 别让一个拼错的名字变成"抛异常 ⇒ 整轮不发"。"""
        assert bd.window_of("w33k")[2].endswith("daily")


class Test卡片口径:
    def _card(self, session, section="xiaohongshu", period="daily", top=8) -> str:
        since, until, _ = bd.window_of(period)
        data = bd.build(session, 1, section, since, until, top=top)
        return json.dumps(bd.render_card(data, period, top=top), ensure_ascii=False)

    def test_资源_作品_热度三处都在(self, session) -> None:
        _seed(session, "xiaohongshu", "花少2人格测试",
              url="https://www.xiaohongshu.com/explore/abc",
              our_url="https://pan.quark.cn/s/OUR", metrics={"liked_count": 12000})
        blob = self._card(session)
        assert "花少2人格测试" in blob
        assert "https://pan.quark.cn/s/OUR" in blob and "🔴夸克" in blob
        assert "https://www.xiaohongshu.com/explore/abc" in blob and "看作品" in blob
        assert "♥12000" in blob, "热度要进卡(客户看的就是这个)"

    def test_反复推的要有次数标记(self, session) -> None:
        for h in (9, 13, 17):
            _seed(session, "xiaohongshu", "反复推的资源",
                  when=datetime.combine(_yesterday().date(), time(h, 0)))
        blob = self._card(session)
        assert "🔁3次" in blob, "客户抱怨的就是同一条反复出现 ⇒ 次数要写在脸上"

    def test_只讲有什么_不许出现内部话(self, session) -> None:
        """★ **用户口径 ③ 的守卫**:客户群里出现的只能是"有什么"。

        失败率/覆盖率/限流/风控/登录态都是**我们自己的运维状态**;
        「我方链」三个字还会暴露运营方身份(本仓 2026-10-04 就为这两个字改过口径)。
        """
        _seed(session, "xiaohongshu", "某资源", our_url="https://pan.quark.cn/s/A")
        since, until, _ = bd.window_of("daily")
        data = bd.build(session, 1, "xiaohongshu", since, until)
        #: 把可能的脏东西塞进去,验证渲染**不会**把它们带出来
        data["items"][0]["metrics"] = {}
        blob = json.dumps(bd.render_card(data, "daily"), ensure_ascii=False)
        for bad in ("失败", "覆盖率", "限流", "风控", "未登录", "我方链", "采集", "重试"):
            assert bad not in blob, f"客户口径的卡片里不该出现「{bad}」:{blob[-300:]}"

    def test_没有链也要如实留破折号(self, session) -> None:
        _seed(session, "xiaohongshu", "没找到链的资源")
        assert "没找到链的资源 · —" in self._card(session)

    def test_超过_top_要说明还有多少(self, session) -> None:
        for i in range(5):
            _seed(session, "xiaohongshu", f"资源{i}")
        blob = self._card(session, top=3)
        assert "还有 2 个资源" in blob, "别让'只剩 3 个'与'总共就 3 个'同形"

    def test_剩下的数按资源算不按推送行数算(self, session) -> None:
        """★ 2026-10-16 预览时抓到的:同一个资源推 2 次 = 2 行,
        拿**行数**去减会算出「还有 1 条」这种鬼话(客户要的是"还有几个**资源**")。"""
        _seed(session, "xiaohongshu", "推两次的资源",
              when=datetime.combine(_yesterday().date(), time(9)))
        _seed(session, "xiaohongshu", "推两次的资源",
              when=datetime.combine(_yesterday().date(), time(19)))
        blob = self._card(session, top=1)
        assert "还有" not in blob, f"只有一个资源,不该说'还有':{blob[-300:]}"

    def test_对标作品那条链来自_sample_urls(self, session) -> None:
        """★ 那条链的原帖链在 `sample_urls`(list)里 —— 不认它,这一列恒 `—`。

        ⚠️ 这里面还带着"**空串要跳过**"那一半:`sample_urls` 里源没给链时是空串,
        取到空串等于这一列还是点不动。
        """
        _seed(session, "xiaohongshu", "某资源",
              sample_urls=["", "https://www.xiaohongshu.com/explore/xyz"])
        since, until, _ = bd.window_of("daily")
        card = bd.render_card(bd.build(session, 1, "xiaohongshu", since, until), "daily")
        cells = [c["elements"][0]["text"]["content"]
                 for row in card["elements"] if row.get("tag") == "column_set"
                 for c in row["columns"]]
        assert "[▶ 看作品](https://www.xiaohongshu.com/explore/xyz)" in cells

    def test_没登记的板块名也用自己发得出去(self, session) -> None:
        """新板块**没登记进 `BOARD_LABELS`** 时,不许因此发不出去(宁可名字是原键)。"""
        _seed(session, "newboard", "某资源")
        assert bd.board_label("newboard") == "newboard"
        assert "某资源" in self._card(session, section="newboard")

    def test_周结标题与日结不同(self, session) -> None:
        _seed(session, "xiaohongshu", "某资源")
        assert "昨日汇总" in self._card(session, period="daily")
        assert "本周汇总" in self._card(session, period="weekly")


class Test发送与幂等:
    @pytest.fixture
    def sent(self, monkeypatch):
        """打桩飞书:记下 (webhook, card),`fail_hooks` 里的 hook 返回失败。"""
        from app.services import feishu_client

        box = {"calls": [], "fail_hooks": set()}

        class _C:
            def __init__(self, webhook, secret):
                self.hook = webhook

            def send_card(self, card):
                box["calls"].append((self.hook, card))
                return self.hook not in box["fail_hooks"]

        monkeypatch.setattr(feishu_client, "FeishuClient", _C)
        return box

    def test_空窗口不发(self, session, sent) -> None:
        """纪律 1:那个板块昨天没内容 ⇒ **不发空卡**。"""
        assert bd.send_digest(session, 1, "xiaohongshu", "daily", _S()) is False
        assert sent["calls"] == []

    def test_没配群的不发(self, session, sent) -> None:
        _seed(session, "zhihu", "知乎那边发现的资源")   # `_S` 里没有 zhihu 的 webhook
        assert bd.send_digest(session, 1, "zhihu", "daily", _S()) is False
        assert sent["calls"] == []

    def test_同一期同一个群只发一次(self, session, sent) -> None:
        _seed(session, "xiaohongshu", "某资源")
        assert bd.send_digest(session, 1, "xiaohongshu", "daily", _S()) is True
        assert bd.send_digest(session, 1, "xiaohongshu", "daily", _S()) is False
        assert len(sent["calls"]) == 1, "重启/补跑不许把汇总重发一遍"

    def test_发送失败_不打标_下次还能补(self, session, sent) -> None:
        """★ 失败就标"已发"= 那个群**这一天永远收不到**汇总(静默丢一整天)。"""
        _seed(session, "xiaohongshu", "某资源")
        sent["fail_hooks"].add("https://example.com/xhs")
        assert bd.send_digest(session, 1, "xiaohongshu", "daily", _S()) is False
        assert session.get(SystemConfig, "bd_sent_daily_xiaohongshu") is None
        sent["fail_hooks"].clear()
        assert bd.send_digest(session, 1, "xiaohongshu", "daily", _S()) is True

    def test_日结与周结各自记各自的(self, session, sent) -> None:
        _seed(session, "xiaohongshu", "某资源")
        assert bd.send_digest(session, 1, "xiaohongshu", "daily", _S()) is True
        assert bd.send_digest(session, 1, "xiaohongshu", "weekly", _S()) is True
        assert len(sent["calls"]) == 2


class Test作业入口:
    def test_只给有台账且有群的板块发(self, session, monkeypatch) -> None:
        from app.services import feishu_client

        calls: list[str] = []

        class _C:
            def __init__(self, webhook, secret):
                self.hook = webhook

            def send_card(self, card):
                calls.append(self.hook)
                return True

        monkeypatch.setattr(feishu_client, "FeishuClient", _C)
        _seed(session, "xiaohongshu", "小红书那边的")
        _seed(session, "kuaishou", "快手那边的")
        _seed(session, "zhihu", "知乎那边的")          # 没配群 ⇒ 不发
        n = bd.run_board_digest_all_users("daily", settings=_S(), session=session)
        assert n == 2 and sorted(calls) == ["https://example.com/ks", "https://example.com/xhs"]
        note = session.query(models.RunRecord).filter_by(kind="board_digest_daily", user_id=1).one().detail
        assert "已发:kuaishou,xiaohongshu" in note and "zhihu(无群)" in note
        #: ★ 台账行数必须写出来:「一个群都没发」可能是"真没内容",也可能是"台账没在记" ——
        #: 带上这个数,后一种才有机会被发现(本仓母题:别让两种情况同形)。
        assert "台账3行" in note

    def test_没发成功的要在运行记录里显形(self, session, monkeypatch) -> None:
        """⚠️ 与「这个板块本来就没内容」必须分得开(本仓母题:没做成 ≠ 没做)。"""
        from app.services import feishu_client

        class _C:
            def __init__(self, webhook, secret):
                self.hook = webhook

            def send_card(self, card):
                return self.hook != "https://example.com/ks"

        monkeypatch.setattr(feishu_client, "FeishuClient", _C)
        _seed(session, "xiaohongshu", "成")
        _seed(session, "kuaishou", "败")
        assert bd.run_board_digest_all_users("daily", settings=_S(), session=session) == 1
        note = session.query(models.RunRecord).filter_by(kind="board_digest_daily", user_id=1).one().detail
        assert "**未发:kuaishou**" in note

    def test_白名单生效(self, session, monkeypatch) -> None:
        from app.services import feishu_client

        calls: list[str] = []

        class _C:
            def __init__(self, webhook, secret):
                self.hook = webhook

            def send_card(self, card):
                calls.append(self.hook)
                return True

        class _S2(_S):
            board_digest_boards = "kuaishou"

        monkeypatch.setattr(feishu_client, "FeishuClient", _C)
        _seed(session, "xiaohongshu", "不该发")
        _seed(session, "kuaishou", "该发")
        bd.run_board_digest_all_users("daily", settings=_S2(), session=session)
        assert calls == ["https://example.com/ks"]

    def test_总开关关掉就一条都不发(self, session, monkeypatch) -> None:
        from app.services import feishu_client

        monkeypatch.setattr(feishu_client, "FeishuClient",
                            lambda *a, **k: pytest.fail("关掉了就不该建客户端"))

        class _S3(_S):
            board_digest_enabled = False

        _seed(session, "xiaohongshu", "某资源")
        assert bd.run_board_digest_all_users("daily", settings=_S3(), session=session) == 0

    def test_只给启用用户发(self, session, monkeypatch) -> None:
        from app.services import feishu_client

        class _C:
            def __init__(self, webhook, secret):
                pass

            def send_card(self, card):
                return True

        monkeypatch.setattr(feishu_client, "FeishuClient", _C)
        session.get(User, 2).enabled = False
        session.commit()
        _seed(session, "xiaohongshu", "u1 的", user_id=1)
        _seed(session, "xiaohongshu", "u2 的", user_id=2)
        assert bd.run_board_digest_all_users("daily", settings=_S(), session=session) == 1


def _patch_send(monkeypatch, fail_hooks=()) -> list[str]:
    """打桩飞书,返回被调到的 webhook 列表(`fail_hooks` 里的返回失败)。"""
    from app.services import feishu_client

    calls: list[str] = []

    class _C:
        def __init__(self, webhook, secret):
            self.hook = webhook

        def send_card(self, card):
            calls.append(self.hook)
            return self.hook not in fail_hooks

    monkeypatch.setattr(feishu_client, "FeishuClient", _C)
    return calls


class Test周结日不发日结:
    """★ 用户口径(2026-10-16)「**可以按照你的建议来进行**」:周结日**不发**日结 ——
    周结的窗口(过去 7 天)已经包含昨天 ⇒ 那天两张卡是把同一批资源说两遍,
    而客户抱怨的恰恰是消息太多。
    """

    MON = datetime(2026, 10, 19, 8, 32)     # 2026-10-19 是周一(周结日)
    TUE = datetime(2026, 10, 20, 8, 32)
    SUN = datetime(2026, 10, 18, 12, 0)

    def test_星期从周结的_cron_读_不写死周一(self) -> None:
        """⚠️ 判据只能有一处:改 cron 就等于改行为,别再抄一份"周一"常量。"""
        class _S1(_S):
            board_digest_weekly_cron = "3 9 * * 1"

        class _S2(_S):
            board_digest_weekly_cron = "3 9 * * sun"

        assert bd._weekly_dow(_S1()) == 0
        assert bd._weekly_dow(_S2()) == 6
        assert bd._is_weekly_day(_S1(), self.MON) is True
        assert bd._is_weekly_day(_S1(), self.TUE) is False

    def test_cron_读不出来就照常发_不许静默停掉日结(self) -> None:
        """⚠️ 宁可多发一条消息,也不要因为一行 cron 被改写就把日结**静默停掉**。"""
        class _S4(_S):
            board_digest_weekly_cron = "写坏了"

        assert bd._weekly_dow(_S4()) is None
        assert bd._is_weekly_day(_S4(), self.MON) is False

    def test_周一日结不发_但要留下原因(self, session, monkeypatch) -> None:
        calls = _patch_send(monkeypatch)
        pl.record(session, 1, "xiaohongshu", [{"title": "周日的资源"}], when=self.SUN)
        assert bd.run_board_digest_all_users("daily", settings=_S(), session=session,
                                             now=self.MON) == 0
        assert calls == []
        note = session.query(models.RunRecord).filter_by(
            kind="board_digest_daily", user_id=1).one().detail
        assert "周结日" in note, f"「没日结」必须与「日结坏了」分得开:{note}"

    def test_周二日结照常发(self, session, monkeypatch) -> None:
        calls = _patch_send(monkeypatch)
        pl.record(session, 1, "xiaohongshu", [{"title": "周一的资源"}],
                  when=datetime(2026, 10, 19, 12))
        assert bd.run_board_digest_all_users("daily", settings=_S(), session=session,
                                             now=self.TUE) == 1
        assert calls == ["https://example.com/xhs"]

    def test_周结日那天周结自己照发(self, session, monkeypatch) -> None:
        calls = _patch_send(monkeypatch)
        pl.record(session, 1, "xiaohongshu", [{"title": "周日的资源"}], when=self.SUN)
        assert bd.run_board_digest_all_users("weekly", settings=_S(), session=session,
                                             now=self.MON) == 1
        assert calls == ["https://example.com/xhs"]

    def test_开关关掉就回到天天发(self, session, monkeypatch) -> None:
        """可回退:不想"周一少一条"就把它改 False。"""
        class _S5(_S):
            board_digest_daily_skip_on_weekly = False

        calls = _patch_send(monkeypatch)
        pl.record(session, 1, "xiaohongshu", [{"title": "周日的资源"}], when=self.SUN)
        assert bd.run_board_digest_all_users("daily", settings=_S5(), session=session,
                                            now=self.MON) == 1
        assert calls == ["https://example.com/xhs"]
