"""公众号**列表源验活**单测(2026-10-06)。

## 为什么单独立一个文件
`wemp`(自研兜底,本该是"WeRSS 挂了"的保险)自 2026-10-02 14:00 起每轮报
`200003 会话失效`,**连着 4 天没人知道** —— 而每天 09:30 推管理群的那份体检报告上
**它连一行都没有**。和 10-05 给微信读书补验活是同一个病:**只看"配了没"是假绿**。

## 这里守的两条纪律
1. **主动问**,不能靠日志:WeRSS 答上了的号 `wemp` 根本不会被调用,它的死**不会进日志**;
2. **分"会自愈"和"要人工"**:限流(200013)报红就是**假红**,而假红会训练人忽略整份报告。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.db.database import Base  # noqa: E402
from app.db import models  # noqa: E402,F401
from app.db.models import WechatBenchmark  # noqa: E402
from app.services import chain_health as ch  # noqa: E402
from app.services import wechat_monitor  # noqa: E402


@pytest.fixture
def session():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)()
    yield db
    db.close()


def _bench(session, biz: str = "MP_WXS_3902714095") -> WechatBenchmark:
    b = WechatBenchmark(user_id=1, nickname="小猫爱吃米", biz=biz)
    session.add(b)
    session.commit()
    return b


class _Be:
    """假后端:按脚本抛错或返回条数。"""

    def __init__(self, n: int | None = 0, exc: Exception | None = None) -> None:
        self.n, self.exc = n, exc

    def mp_articles(self, mp_id, page=1, limit=20):
        if self.exc is not None:
            raise self.exc
        return [{"title": f"t{i}", "url": f"https://x/{i}"} for i in range(self.n or 0)]


def _patch(monkeypatch, backends):
    monkeypatch.setattr(wechat_monitor, "configured_backends",
                        lambda settings, session=None, user_id=None: backends)


def _by_name(rows):
    return {r["name"]: r for r in rows}


class TestClassify:
    """★ **"要人工"与"会自愈"必须分开** —— 判据错了,报告就没人信了。"""

    def test_会话失效算要人工(self) -> None:
        from app.services.wechat.wemp_client import WempAuthError
        assert ch._classify_source_error(
            WempAuthError("公众号后台会话失效(200003),请重新扫码授权"))[0] == "auth"

    def test_限流算自愈而不是要人工(self) -> None:
        """⚠️ **这条最容易错**:200013 会自己好,报红就是假红。"""
        from app.services.wechat.wemp_client import WempRateLimited
        assert ch._classify_source_error(
            WempRateLimited("appmsgpublish 频率限制(200013)"))[0] == "selfheal"

    def test_网络错误算自愈(self) -> None:
        assert ch._classify_source_error(TimeoutError("Connection timed out"))[0] == "selfheal"

    def test_认不出的不硬判要人工(self) -> None:
        """⚠️ 认不出就 🟡(**宁可漏不可假红**)—— 不能默认当"要人工"。"""
        assert ch._classify_source_error(ValueError("某种新错误"))[0] == "unknown"


class TestCheckListSources:
    def test_凭据失效判红并给出修法(self, session, monkeypatch) -> None:
        from app.services.wechat.wemp_client import WempAuthError

        _bench(session)
        _patch(monkeypatch, [("werss", _Be(0)),
                             ("wemp", _Be(exc=WempAuthError("会话失效(200003)")))])
        rows = ch.check_list_sources(session)
        w = _by_name(rows)["公众号·列表源·wemp"]
        assert w["level"] == ch.RED
        assert "wemp_cred.py" in w["detail"], "判红必须**直接给出修法**,否则等于只喊了一声"

    def test_限流只判黄不判红(self, session, monkeypatch) -> None:
        """★ **假红防线**:限量会自己好,报红会训练人忽略整份报告。"""
        from app.services.wechat.wemp_client import WempRateLimited

        _bench(session)
        _patch(monkeypatch, [("werss", _Be(0)),
                             ("wemp", _Be(exc=WempRateLimited("频率限制(200013)")))])
        rows = ch.check_list_sources(session)
        assert _by_name(rows)["公众号·列表源·wemp"]["level"] == ch.YELLOW

    def test_两个源都活就不报单点(self, session, monkeypatch) -> None:
        _bench(session)
        _patch(monkeypatch, [("werss", _Be(3)), ("wemp", _Be(2))])
        rows = ch.check_list_sources(session)
        assert all(r["level"] == ch.GREEN for r in rows), rows
        assert "公众号·列表源兜底" not in _by_name(rows)

    def test_只剩一个活源要报单点(self, session, monkeypatch) -> None:
        """★ 这是**最初几天看不见的那个信号**:还有源在答 ⇒ 所有产出指标照常绿。"""
        from app.services.wechat.wemp_client import WempAuthError

        _bench(session)
        _patch(monkeypatch, [("werss", _Be(0)),
                             ("wemp", _Be(exc=WempAuthError("会话失效(200003)")))])
        rows = ch.check_list_sources(session)
        note = _by_name(rows)["公众号·列表源兜底"]
        assert note["level"] == ch.YELLOW and "只剩 1 个" in note["detail"]

    def test_一个源都没配判红(self, session, monkeypatch) -> None:
        _bench(session)
        _patch(monkeypatch, [])
        assert ch.check_list_sources(session)[0]["level"] == ch.RED

    def test_没有可试的对标号时明说跳过不算通过(self, session, monkeypatch) -> None:
        """⚠️ **跳过 ≠ 通过** —— 把"没验"显示成绿灯,就是给假绿开门。"""
        _patch(monkeypatch, [("werss", _Be(0))])
        rows = ch.check_list_sources(session)          # 没建对标号
        assert rows[0]["level"] == ch.YELLOW
        assert "跳过不等于通过" in rows[0]["detail"]

    def test_探针崩了不毁整份报告(self, session, monkeypatch) -> None:
        """体检里一个探针异常不该让整份报告变成空白(与 `_weread_alive` 同一纪律)。"""
        _bench(session)

        def _boom(settings, session=None, user_id=None):
            raise RuntimeError("配置读不了")
        monkeypatch.setattr(wechat_monitor, "configured_backends", _boom)
        rows = ch.check_list_sources(session)
        assert rows and rows[0]["level"] == ch.YELLOW

    def test_源自己抛的怪异常也不外泄(self, session, monkeypatch) -> None:
        _bench(session)
        _patch(monkeypatch, [("werss", _Be(exc=RuntimeError("闻所未闻")))])
        rows = ch.check_list_sources(session)          # 不该抛
        assert rows[0]["level"] == ch.YELLOW

    def test_体检报告收录了这一段(self, session, monkeypatch) -> None:
        """★ 它必须**真出现在报告里** —— 否则修了也白修(4 天没人知道就是因为它不在)。"""
        _bench(session)
        _patch(monkeypatch, [("werss", _Be(0))])
        secs = ch.collect_sections(session)
        titles = [t for t, _ in secs]
        assert any("凭证" in t for t in titles)
        names = [r["name"] for t, rows in secs if "凭证" in t for r in rows]
        assert any("列表源" in n for n in names), names
