"""列表额度预算(`wechat_listen_reads_budget_per_acct`)的行为(2026-10-22)。

## 为什么需要这组测试
2026-10-20 改「24h 有新文才查列表」之后,闸门**只判有没有新文、不判额度够不够**。
积压多的一轮(10-22 04:24 实测 `new=420`)会把 60+ 个号一次问完,当天额度当场耗光;
之后 08:04 / 14:05 / 17:20 三轮回 `-2041`,精确阅读数**一整天为 0**。

预算的作用是把"一口气打穿"换成"按最久没问过轮转":
  · 本轮最多发 `每账号份额 × 账号数` 次列表请求;
  · 超出的号只取 cover,**不花额度**;
  · 与"没有新文"分开计数(`budget_skip`),否则覆盖不足会被误读成"今天没新文"。
"""
import os
from datetime import datetime, timedelta

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db import models  # noqa: F401
from app.db.models import WechatBenchmark
from app.services import wechat_monitor
from app.services.cookie_store import set_cookie as _set_cookie
from app.services.wechat._listen import (
    _needs_reads_refresh,
    _reads_budget,
    _reads_marks,
)
from config.settings import Settings


def _settings(**kw) -> Settings:
    base = {"wechat_sync_max_pages": 2, "weread_shelf_gate": False,
            "pan_transfer_enabled": False}
    base.update(kw)
    return Settings(_env_file=None, is_dev=True, **base)


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    yield db
    db.close()


@pytest.fixture(autouse=True)
def _no_quiet_hours(monkeypatch):
    from app.services import feishu_client as fc

    monkeypatch.setattr(fc, "is_quiet_hours", lambda settings, now=None: False)
    monkeypatch.setattr(wechat_monitor, "is_quiet_hours", lambda settings, now=None: False)
    #: 正文抓取走真实 HTTP(`_listen._resolve` 经门面查这个名字) —— 测试里钉成空串,
    #: 否则离线审计会把 `socket.getaddrinfo` 拦成 PermissionError(不是被测行为)。
    monkeypatch.setattr(wechat_monitor, "fetch_article_content", lambda *a, **k: "")


class _CountingWeread:
    """cover 永远有一篇;列表每次调用都成功,并记下**真发了几次**。"""

    def __init__(self) -> None:
        self.list_calls = 0

    def latest_article(self, book_id):
        return {"title": f"{book_id} 的封面文", "url": f"https://mp.weixin.qq.com/s/{book_id}",
                "review_id": f"{book_id}_c", "digest": ""}

    def mp_content(self, review_id: str) -> str:
        return ""

    def shelf(self) -> list:
        return []

    def mp_articles(self, book_id, offset=0, count=20):
        self.list_calls += 1
        import time as _t

        now = int(_t.time())
        return {"reviews": [{"createTime": now, "subReviews": [{"review": {
            "mpInfo": {"title": f"{book_id} 的新文", "originalId": f"id_{book_id}",
                       "readNum": 42, "likeNum": 1},
            "reviewId": f"{book_id}_r"}, "createTime": now}]}], "synckey": 1}


def _add_benchmarks(session, n: int) -> None:
    for i in range(n):
        session.add(WechatBenchmark(user_id=1, nickname=f"号{i}",
                                    weread_book_id=f"MP_WXS_{i}", anchor_url=""))
    session.commit()


# ---------------------------------------------------------------- 纯函数

def test_预算按账号数翻倍() -> None:
    """一个账号 20、两个账号 40 —— 额度是按账号记的,窗口也就该按账号开。"""
    class _Pool:
        def __len__(self):
            return 2

    assert _reads_budget(_settings(), None) == 20
    assert _reads_budget(_settings(), _Pool()) == 40
    assert _reads_budget(_settings(wechat_listen_reads_budget_per_acct=15), _Pool()) == 30


def test_预算对非法配置回落而不是抛() -> None:
    """配置写成 0(或对象根本没有这个属性)时**不许把整轮带崩**,按 20 算。"""
    assert _reads_budget(_settings(wechat_listen_reads_budget_per_acct=0), None) == 20

    class _Bare:
        """没有该配置的替身(测试里常见的 SimpleNamespace 形态)。"""

    assert _reads_budget(_Bare(), None) == 20


# ---------------------------------------------------------------- 轮内闸门

def test_预算用完后剩余号只取cover(session, monkeypatch: pytest.MonkeyPatch) -> None:
    """2 个号 + 预算 1 ⇒ 只发 1 次列表;另一个号走 cover,并记 `budget_skip=1`。

    这条钉的是"**一天只有凌晨一轮拿得到读数**"的机制:
    闸门只看新文、不看额度时,这一轮会把 2 个(生产上是 60+)号全问一遍。
    """
    _set_cookie(session, 1, "weread", "vid=1; skey=x")
    _add_benchmarks(session, 2)
    fake = _CountingWeread()
    monkeypatch.setattr(wechat_monitor, "WereadClient", lambda cookie: fake)

    out = wechat_monitor.run_wechat_listen(
        session, 1, settings=_settings(wechat_listen_reads_budget_per_acct=1), weread=fake)

    assert fake.list_calls == 1, f"预算 1 却发了 {fake.list_calls} 次列表请求"
    stats = out.get("weread_list") or {}
    assert stats.get("weread_list_budget_skipped") == 1, (
        f"超预算的号必须单独计数,而不是记成'没新文':{stats}")
    assert out["new"] >= 1, "被预算挡下的号仍要取 cover —— 省额度不等于失明"


def test_预算充足时两个号都问列表(session, monkeypatch: pytest.MonkeyPatch) -> None:
    """反向:预算够就与旧行为一致(两个号各问一次),别把正常覆盖也压掉。"""
    _set_cookie(session, 1, "weread", "vid=1; skey=x")
    _add_benchmarks(session, 2)
    fake = _CountingWeread()
    monkeypatch.setattr(wechat_monitor, "WereadClient", lambda cookie: fake)

    out = wechat_monitor.run_wechat_listen(
        session, 1, settings=_settings(wechat_listen_reads_budget_per_acct=25), weread=fake)

    assert fake.list_calls == 2
    assert (out.get("weread_list") or {}).get("weread_list_budget_skipped", 0) == 0


# ---------------------------------------------------------------- 按成功刷新轮转
# 2026-11-28:旧判据"近 24h 有新文才查"对天天发文的号恒为真 ⇒ 同一批热号每轮重问,
# 没新文的号永远不问(近 8 天每天只覆盖 16~25 个号)。新判据按**成功刷新**轮转。


def _bench(book_id: str = "MP_WXS_1"):
    return WechatBenchmark(id=1, user_id=1, nickname="号", weread_book_id=book_id)


class TestNeedsReadsRefresh:
    def test_从没刷新过就查(self) -> None:
        assert _needs_reads_refresh(
            _bench(), {}, {}, datetime.now(), _settings()) == (True, "never")

    def test_新文早于上次刷新_不重复查(self) -> None:
        now = datetime.now()
        last = now.timestamp()
        assert _needs_reads_refresh(
            _bench(), {"MP_WXS_1": last}, {"MP_WXS_1": last - 3600},
            now, _settings()) == (False, "fresh")

    def test_刷新之后又发新文_要查(self) -> None:
        now = datetime.now()
        last = now.timestamp() - 2 * 3600
        assert _needs_reads_refresh(
            _bench(), {"MP_WXS_1": last}, {"MP_WXS_1": last + 60},
            now, _settings()) == (True, "new")

    def test_到期补刷(self) -> None:
        now = datetime.now()
        last = (now - timedelta(hours=25)).timestamp()
        assert _needs_reads_refresh(
            _bench(), {"MP_WXS_1": last}, {"MP_WXS_1": last - 60},
            now, _settings()) == (True, "periodic")

    def test_关闭周期补刷_只按新文(self) -> None:
        now = datetime.now()
        last = (now - timedelta(days=30)).timestamp()
        st = _settings(wechat_listen_reads_refresh_hours=0)
        assert _needs_reads_refresh(
            _bench(), {"MP_WXS_1": last}, {"MP_WXS_1": last - 60},
            now, st) == (False, "fresh")


def test_成功后同一窗口不再重问_到期才补刷(session, monkeypatch: pytest.MonkeyPatch) -> None:
    """成功刷新后 24h 内不再问同一号 —— 这正是把额度让给没轮到的号的关键。

    没有新文、现成的刷新记号 ⇒ 第二轮只取 cover;把记号改回 25 小时前 ⇒ 第三轮到期补刷。
    """
    import json
    import time

    from sqlalchemy import select

    from app.db.models import SystemConfig

    _set_cookie(session, 1, "weread", "vid=1; skey=x")
    _add_benchmarks(session, 2)
    fake = _CountingWeread()
    monkeypatch.setattr(wechat_monitor, "WereadClient", lambda cookie: fake)
    st = _settings(wechat_listen_reads_budget_per_acct=25)

    out1 = wechat_monitor.run_wechat_listen(session, 1, settings=st, weread=fake)
    assert fake.list_calls == 2
    assert (out1.get("weread_list") or {}).get("weread_list_periodic_due", 0) == 0
    marks = _reads_marks(session, 1)
    assert set(marks) == {"MP_WXS_0", "MP_WXS_1"}, "成功刷新必须逐号记时刻"

    out2 = wechat_monitor.run_wechat_listen(session, 1, settings=st, weread=fake)
    assert fake.list_calls == 2, f"成功刷新后同一窗口内不许重问:{fake.list_calls}"
    assert (out2.get("weread_list") or {}).get("weread_list_new_skipped", 0) == 2

    row = session.scalar(select(SystemConfig).where(
        SystemConfig.key == "weread_reads_marks_1"))
    old = json.loads(row.value)
    row.value = json.dumps({k: time.time() - 25 * 3600 for k in old})
    # 文章发布时刻也要压到刷新之前 —— 否则判据会先走"刷新后又发过新文"那一条,
    # 测不到「到期补刷」。生产上刷新时刻本就晚于已看到的文章。
    from app.db.models import WechatArticle

    session.query(WechatArticle).update(
        {"publish_at": datetime.now() - timedelta(days=2)})
    session.commit()

    out3 = wechat_monitor.run_wechat_listen(session, 1, settings=st, weread=fake)
    assert fake.list_calls == 4, "到期必须补刷,否则没有新文的号永远拿不到读数"
    assert (out3.get("weread_list") or {}).get("weread_list_periodic_due", 0) == 2


class _FailingWeread(_CountingWeread):
    def mp_articles(self, book_id, offset=0, count=20):
        self.list_calls += 1
        raise wechat_monitor.WereadError("微信读书接口返回错误(-2041):请求频率过高")


def test_失败不记刷新_下轮仍会重试(session, monkeypatch: pytest.MonkeyPatch) -> None:
    """失败/被挡**不能**写成"已刷新" —— 否则那个号 24h 内再也不会被问(假绿灯)。

    与 `_save_shelf_marks` 只收"问过且答上"的号同一条纪律;
    同轮轮转仍按 `_list_marks`(问过就记),失败号下一轮让位给没问过的号。
    """
    _set_cookie(session, 1, "weread", "vid=1; skey=x")
    _add_benchmarks(session, 2)
    fake = _FailingWeread()
    monkeypatch.setattr(wechat_monitor, "WereadClient", lambda cookie: fake)

    wechat_monitor.run_wechat_listen(session, 1, settings=_settings(), weread=fake)

    assert _reads_marks(session, 1) == {}, "被 -2041 挡下不是刷新成功"


def test_新文触发_不必等到期(session, monkeypatch: pytest.MonkeyPatch) -> None:
    """上次刷新之后又发过新文 ⇒ 即使没到 24h 也要问(热号的新文读数不能等一天)。"""
    import json
    import time

    from sqlalchemy import select

    from app.db.models import SystemConfig, WechatArticle

    _set_cookie(session, 1, "weread", "vid=1; skey=x")
    _add_benchmarks(session, 2)
    fake = _CountingWeread()
    monkeypatch.setattr(wechat_monitor, "WereadClient", lambda cookie: fake)
    st = _settings(wechat_listen_reads_budget_per_acct=25)
    wechat_monitor.run_wechat_listen(session, 1, settings=st, weread=fake)
    assert fake.list_calls == 2

    last = time.time() - 2 * 3600
    row = session.scalar(select(SystemConfig).where(
        SystemConfig.key == "weread_reads_marks_1"))
    row.value = json.dumps({k: last for k in json.loads(row.value)})
    # 两号都有第一轮入库的文章:把号0 的发布时刻压到刷新之前,只有号1 之后发过新文。
    session.query(WechatArticle).filter(WechatArticle.benchmark_id == 1).update(
        {"publish_at": datetime.now() - timedelta(hours=3)})
    session.query(WechatArticle).filter(WechatArticle.benchmark_id == 2).update(
        {"publish_at": datetime.now() - timedelta(hours=1)})
    session.commit()

    out = wechat_monitor.run_wechat_listen(session, 1, settings=st, weread=fake)
    assert fake.list_calls == 3, "只有有新文的那个号该被追加问一次"
    stats = out.get("weread_list") or {}
    assert stats.get("weread_list_periodic_due", 0) == 0
    assert stats.get("weread_list_new_skipped", 0) == 1
