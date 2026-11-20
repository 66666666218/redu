"""**爆款预警**(`app/services/promo_forecast.py`)的口径守卫。

## 为什么值得钉

这张卡会让人**照着去搬一条资源**。四类错误各自对着一件真事:

① **窗口没走完就下结论** ⇒ 刚出现 2 小时、已有 3 人的桶被判成"不会爆";
② **自有链按存在判而不是按时刻判** ⇒ 早窗口**之后**才搬的链把真能搬的挡掉
   (可动手量被系统性低估,而数字变小看起来像"保守");
③ **把纯套话当作品** ⇒ 卡片标题是「链接自取可直接打开」,照它无从搬起;
④ **把已有链/没有源链的也算进候选** ⇒ 报了也动不了手,预警变成噪音。

⚠️ 头注里的阈值(≥4 人 = 41.4%)出自 `scripts/backtest_promo_forecast.py`,
这里只钉**口径**,不重算统计量 —— 统计量的守卫在 `tests/test_backtest_promo_forecast.py`。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

from datetime import datetime, timedelta  # noqa: E402

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.db import models  # noqa: F401
from app.db.database import Base  # noqa: E402
from app.db.models import PushLedger, User  # noqa: E402
from app.services import promo_forecast as pf  # noqa: E402

PAN = "https://pan.xunlei.com/s/FORECAST001"
PK = "pan.xunlei.com/s/FORECAST001"


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    db.add(User(id=1, username="u1", email="u1@b.c", password_hash="x", enabled=True))
    db.commit()
    yield db
    db.close()


def _add(session, author, *, title="资源《某作品》", pan=PAN, at=None, section="bilibili",
         pan_type="迅雷"):
    session.add(PushLedger(user_id=1, section=section, item_key=f"{author}-{title}",
                           title=title, pan_url=pan, pan_type=pan_type, author=author,
                           pushed_at=at or (datetime.now() - timedelta(hours=30))))


def _hands(session, n, *, start_ago_hours=30.0, work="某作品", pan=PAN, pan_type="迅雷"):
    """`n` 个不同作者,在**头 24h 窗口内**陆续出现。"""
    for i in range(n):
        _add(session, f"号{i}", title=f"《{work}》第{i}版", pan=pan, pan_type=pan_type,
             at=datetime.now() - timedelta(hours=start_ago_hours - i * 0.5))


def _own_xunlei(session, url=PAN, when=None):
    from app.db.models import XunleiGroupShare

    session.add(XunleiGroupShare(user_id=1, group_id="g", group_name="群", share_id="s",
                                 origin_url="https://pan.xunlei.com/s/SRC", title="源",
                                 status="ok", our_url=url,
                                 msg_time=when or datetime.now() - timedelta(days=10),
                                 moved_at=when or datetime.now() - timedelta(days=10)))


# ------------------------------------------------------------------ ① 窗口要完整

def test_头24h攒够人且我们没有链_会报(session) -> None:
    _hands(session, 5)
    session.commit()
    out = pf.detect_forecasts(session, 1)
    assert len(out) == 1, out
    assert out[0]["hands"] == 5
    assert out[0]["work_key"] == "某作品"
    assert out[0]["src_urls"] == {"迅雷": PAN}


def test_窗口还没走完就不许下结论(session) -> None:
    """★ 刚出现 3 小时、已有 4 人的桶 —— 后面 21 小时还可能涨,现在判是"拿半个窗口当全窗口"。"""
    for i in range(4):
        _add(session, f"号{i}", title=f"《新作品》{i}",
             at=datetime.now() - timedelta(hours=3 - i * 0.2))
    session.commit()
    stats: dict = {}
    assert pf.detect_forecasts(session, 1, stats=stats) == []
    assert stats["eval"] == 0, "窗口未走完的桶不该进入可评估集合"


def test_人不够就不报_但达标数要记账(session) -> None:
    _hands(session, 3)
    session.commit()
    stats: dict = {}
    assert pf.detect_forecasts(session, 1, stats=stats) == []
    assert stats["eval"] == 1 and stats["hit"] == 0, "静默丢桶 ⇒ 库里查不出'为什么不报'"


def test_窗口之后才来的人不算进头24h(session) -> None:
    """头 24h 只有 2 人,第 40 小时又来了 3 人 —— 仍然不该报(判据是头 24h)。"""
    now = datetime.now()
    for i in range(2):
        _add(session, f"早{i}", title=f"《某作品》早{i}", at=now - timedelta(hours=40 - i))
    for i in range(3):
        _add(session, f"晚{i}", title=f"《某作品》晚{i}", at=now - timedelta(hours=8 - i))
    session.commit()
    stats: dict = {}
    assert pf.detect_forecasts(session, 1, stats=stats) == []
    assert stats["hit"] == 0


# ------------------------------------------------------------------ ② 自有链按时刻判

def test_早窗口之前就有自有链_不报(session) -> None:
    _own_xunlei(session, when=datetime.now() - timedelta(days=10))
    _hands(session, 5)
    session.commit()
    stats: dict = {}
    assert pf.detect_forecasts(session, 1, stats=stats) == []
    assert stats["owned"] == 1


def test_自有链是早窗口之后才搬的_仍然要报(session) -> None:
    """★ 前视偏差的反面:这条链现在是我们的,但**当时不是** ⇒ 当时确实缺,该报。

    写成 `pk in owned` 就会把它挡掉 —— 可动手量被系统性低估(回测里差 20/153 个桶)。
    """
    _own_xunlei(session, when=datetime.now() - timedelta(hours=1))   # 刚搬进来 1 小时
    _hands(session, 5)                                              # 30 小时前出现
    session.commit()
    out = pf.detect_forecasts(session, 1)
    assert len(out) == 1, "早窗口之后才搬的链被当成了「当时已有」"
    assert out[0]["src_urls"] == {"迅雷": PAN}


# ------------------------------------------------------------------ ③④ 可动手性

def test_没有可转存源链就不报(session) -> None:
    """★ 报了也动不了手 ⇒ 只增加噪音。

    这里的链**能当身份**(`pan_key` 认得出)但**不是三种可转存盘型之一** ——
    正是"我们在推、别人也在转,但我们拿不到可直接转存的源"的形状。
    """
    _hands(session, 5, pan="https://example.com/s/not-a-pan", pan_type="")
    session.commit()
    stats: dict = {}
    assert pf.detect_forecasts(session, 1, stats=stats) == []
    assert stats["no_src"] == 1, "无源链的桶必须记账(静默丢 ⇒ 库里查不出'为什么不报')"


def test_纯套话作品名不报(session) -> None:
    for i in range(5):
        _add(session, f"号{i}", title=f"《链接自取可直接打开》{i}",
             at=datetime.now() - timedelta(hours=30 - i * 0.5))
    session.commit()
    stats: dict = {}
    assert pf.detect_forecasts(session, 1, stats=stats) == []
    assert stats["cta"] == 1


def test_含动作词的真作品仍然要报(session) -> None:
    """★ `七宗罪测试` 含「测试」以外的动作词也是真资源 —— 一刀切会静默丢真货。"""
    _hands(session, 5, work="七宗罪测试")
    session.commit()
    out = pf.detect_forecasts(session, 1)
    assert len(out) == 1 and out[0]["work_key"] == "七宗罪测试"


def test_太老的桶不再报(session) -> None:
    """`max_age_hours` 之外首次出现的桶是前几批已经看过的,重复报没有信息量。"""
    _hands(session, 5, start_ago_hours=24 * 10)
    session.commit()
    assert pf.detect_forecasts(session, 1) == []


def test_板块分布与人数同一口径(session) -> None:
    """`sections` 各板块之和 ≤ `hands`(同一个人跨板块会各算一次,但绝不会更大)。"""
    now = datetime.now()
    for i, sec in enumerate(("bilibili", "kuaishou", "tieba", "zhihu", "weibo_peer")):
        _add(session, f"号{i}", title=f"《某作品》{i}", section=sec,
             at=now - timedelta(hours=30 - i * 0.5))
    session.commit()
    out = pf.detect_forecasts(session, 1)
    assert sum(out[0]["sections"].values()) <= out[0]["hands"]


# ------------------------------------------------------------------ 发送与冷却

class _Settings:
    feishu_secret = ""
    feishu_webhook = ""
    feishu_webhook_admin = "https://example.invalid/hook"
    promo_forecast_early_hours = 24
    promo_forecast_min_hands = 4
    promo_forecast_max_age_hours = 72
    promo_forecast_top = 5
    promo_forecast_cooldown_hours = 168


def test_发不出去就不许留冷却行(session, monkeypatch) -> None:
    """★ 「冷却 = **已经发出去过**」,不是「跑过一次检测」(与 `push_surge` 同一语义)。"""
    import app.db
    from app.db.models import FeishuAlert
    from app.services import feishu_client

    class _Boom:
        def __init__(self, *a, **kw):
            pass

        def send(self, text):
            return False

    _hands(session, 5)
    session.commit()
    assert len(pf.detect_forecasts(session, 1)) == 1, "前置:本该有预警"
    monkeypatch.setattr(app.db, "get_session_local", lambda: (lambda: session))
    monkeypatch.setattr(feishu_client, "FeishuClient", _Boom)
    assert pf.promo_forecast_tick(settings=_Settings()) == 0
    session.rollback()
    assert session.query(FeishuAlert).filter(
        FeishuAlert.section == "promo_forecast").count() == 0


def test_发成功后才记冷却_第二轮不重复(session, monkeypatch) -> None:
    import app.db
    from app.db.models import FeishuAlert
    from app.services import feishu_client

    class _Ok:
        def __init__(self, *a, **kw):
            pass

        def send(self, text):
            return True

    _hands(session, 5)
    session.commit()
    monkeypatch.setattr(app.db, "get_session_local", lambda: (lambda: session))
    monkeypatch.setattr(feishu_client, "FeishuClient", _Ok)
    assert pf.promo_forecast_tick(settings=_Settings()) == 1
    assert session.query(FeishuAlert).filter(
        FeishuAlert.section == "promo_forecast").count() == 1
    assert pf.promo_forecast_tick(settings=_Settings()) == 0, "冷却没生效,同一条被重复报"