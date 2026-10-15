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


class TestReadCountSource:
    """★★ **「源活着」≠「有阅读数」**(2026-10-14)。

    用户当天的报障是「**阅读还是没修好**」,而这份体检当时**全绿** —— 它只测"源能不能答"。
    阅读数是另一个维度:WeRSS / 读书平台的返回结构里**没有** `read_num` 字段,
    只有自研 `wemp` **声称**带(⚠️ **2026-10-15 查实:这件事至今未验证** ——
    它"首次解析打出真实字段名"那条日志**一次都没出现过**,见 `chain_health._WEMP_READ_UNVERIFIED`)。
    ⇒ 只要 wemp 不可用,那一列就只能是 `—`,而"全绿"会让人继续以为没问题。
    """

    def _row(self, session):
        return _by_name(ch.check_list_sources(session))["公众号·阅读数来源"]

    def test_没有带阅读数的源时要明说阅读列只能是横杠(self, session, monkeypatch) -> None:
        """只配了 WeRSS(生产现状)⇒ **必须**说出来,不能让"列表源全绿"盖过去。"""
        _bench(session)
        _patch(monkeypatch, [("werss", _Be(3))])
        row = self._row(session)
        assert row["level"] == ch.YELLOW, "报绿就是假绿:源活着但根本不送阅读数"
        assert "只能是 `—`" in row["detail"]
        assert "wemp" in row["detail"], "要指出**声称**带阅读数的那条路(而不是说成已成立的事实)"

    def test_wemp配了但凭据死了不许报绿(self, session, monkeypatch) -> None:
        """⚠️ **交叉验活**:只按"配了没有"判绿,就会在 wemp 已死时报一行自相矛盾的绿。"""
        from app.services.wechat.wemp_client import WempAuthError

        _bench(session)
        _patch(monkeypatch, [("werss", _Be(3)),
                             ("wemp", _Be(exc=WempAuthError("会话失效(200003)")))])
        row = self._row(session)
        assert row["level"] == ch.YELLOW and "验活没过" in row["detail"]
        assert "仍然是 `—`" in row["detail"], "得把后果说清楚:阅读列还是空的"

    def test_wemp活着才报绿并给出怎么核对(self, session, monkeypatch) -> None:
        _bench(session)
        _patch(monkeypatch, [("werss", _Be(3)), ("wemp", _Be(2))])
        row = self._row(session)
        assert row["level"] == ch.GREEN
        # ★ 光说"绿"不够:得给出**在哪儿核对真的补上了**,否则下次还是只能猜
        assert "reads(" in row["detail"] and "unmatched" in row["detail"]

    # ---- ★ 2026-10-14 补:同一行"没验活过",但**限流**和**会话死**的处置相反 ----
    # 旧文案无论什么原因都叫人"修好凭据" —— 而限流时换凭据是无用动作(当天实测:换上一枚
    # 全新登录的凭据,服务端仍回 200013)。这就是"告警文案通病:方向对、动作错"。

    def test_wemp限流时只许叫人等_绝不许叫人换凭据(self, session, monkeypatch) -> None:
        """🟡 限流(200013)⇒ 处置是**等**。若文案叫人去重新复制 Cookie,就骗人白干一场。"""
        from app.services.wechat.wemp_client import WempRateLimited

        _bench(session)
        _patch(monkeypatch, [("werss", _Be(3)),
                             ("wemp", _Be(exc=WempRateLimited("appmsgpublish 频率限制(200013)")))])
        row = self._row(session)
        assert row["level"] == ch.YELLOW
        assert "只需等" in row["detail"], "限流的正确动作是等"
        assert "唯一需要人动手" not in row["detail"], "限流不是人动手的情形,说成要人动手就是给错动作"
        assert "重跑还会把配额推后" in row["detail"], "要明说重试有害,否则运营者会一直试"

    def test_wemp会话失效才叫人动手(self, session, monkeypatch) -> None:
        from app.services.wechat.wemp_client import WempAuthError

        _bench(session)
        _patch(monkeypatch, [("werss", _Be(3)),
                             ("wemp", _Be(exc=WempAuthError("会话失效(200003)")))])
        row = self._row(session)
        assert row["level"] == ch.YELLOW
        assert "唯一需要人动手" in row["detail"]
        assert "只需等" not in row["detail"], "会话死是等不来的,说'只需等'就是漏报人工卡点"

    def test_两种失败的处置文案必须不同(self, session, monkeypatch) -> None:
        """负向对照:退回"一句通用话"就会同时骗两种场景,这条守住它别回退。"""
        from app.services.wechat.wemp_client import WempAuthError, WempRateLimited

        _bench(session)
        _patch(monkeypatch, [("werss", _Be(3)),
                             ("wemp", _Be(exc=WempRateLimited("appmsgpublish 频率限制(200013)")))])
        limited = self._row(session)["detail"]
        _patch(monkeypatch, [("werss", _Be(3)),
                             ("wemp", _Be(exc=WempAuthError("会话失效(200003)")))])
        dead = self._row(session)["detail"]
        assert limited != dead, "两种完全相反的处置不能共用一句话"
        # 两者都必须保留"后果"这半句(否则读者不知道现在还能不能看到阅读数)
        assert "仍然是 `—`" in limited and "仍然是 `—`" in dead


# ---------------------- 记账解析的健壮性(2026-10-14:这条判据曾整整瞎掉)
#
# 事故形状:解析是**认顺序**的正则(`ok= app= off= … skipped=`),而当月记账里插进了
# `app_fail=` / `app_blocked=`(两条路的失败分账)⇒ 顺序一变就**一次都匹配不上**
# ⇒ 这个专门用来抓"当天全断"的指标,自己成了"看起来在跑、其实什么都没看"的那一个。

def test_轮转记账解析不认字段顺序(session) -> None:
    from app.services.chain_health import check_weread_list_rotation
    from app.services.tenant_base import _record_run

    # 新格式(中间插了 app_fail / app_blocked)—— 旧正则在这里会失明
    _record_run(session, 1, "wechat_listen", "success",
                "accounts=129 new=59 failed=0 weread_list(ok=0 app=0 app_fail=1 "
                "app_blocked=23 off=1 off_with_new=2 skipped=51)")
    session.commit()
    rows = check_weread_list_rotation(session, rounds=1)
    assert rows, "应该有结论行"
    detail = rows[0]["detail"]
    assert "格式变了" not in detail and "都没有" not in detail, f"解析又瞎了:{detail}"
    assert "网页问成 0" in detail and "App 问成 0" in detail, detail


def test_轮转记账真的没记时才报瞎(session) -> None:
    from app.services.chain_health import check_weread_list_rotation
    from app.services.tenant_base import _record_run

    _record_run(session, 1, "wechat_listen", "success", "accounts=1 new=0")
    session.commit()
    detail = check_weread_list_rotation(session, rounds=1)[0]["detail"]
    assert "记账" in detail        # 这一轮确实没有那两个数 ⇒ 如实说"没记账"


# ---------------------------------------------------------------------------
# ★★★ 列表源熔断:**单源时也必须生效**(2026-10-15)
#
# 实测事故:熔断原来是 `MultiSourceClient` 的**实例状态**,而 `_platform_client` 在
# **只有一个源**时直接返回**裸客户端** ⇒ 熔断器**根本不存在**
# ⇒ 每个号都去撞那个已经 `200013` 限流的源 ⇒ 一轮 **117 次无效请求**
# (而报告自己写着「重跑还会把配额推后」)。
# 现在状态提到**模块级**(按源名),`ask_platform` 是唯一入口。


#: ⚠️ 熔断器是**进程级**状态,每个用例前要清 —— 由 `tests/conftest.py` 的
#: `_reset_source_breaker` **全局**负责(不在这里再写一份:那正是"同一件事两处")。


class WempClient:  # noqa: N801 - 名字**故意**与真类一致:`source_key` 按类名映射
    """假的 wemp 客户端:每次调用都抛限流,并数自己被打了几次。"""

    def __init__(self, boom: bool = True) -> None:
        self.calls = 0
        self._boom = boom

    def mp_articles(self, mp_id: str, page: int = 1, limit: int = 20) -> list[dict]:
        self.calls += 1
        if self._boom:
            from app.services.reader_platform_client import PlatformError

            raise PlatformError("/cgi-bin/appmsgpublish 频率限制(200013)")
        return [{"title": "t", "url": "u"}]


class Test单源也要熔断:
    def test_裸客户端与切换链的源名必须是同一个键(self) -> None:
        """★ 否则"单源"与"多源"两种形态下熔断**互不相认**,换个形态就绕过护栏。"""
        from app.services.wechat import _source as S
        from app.services.wechat.wemp_client import WempClient as 真

        assert S.source_key(真("a=b", "tk")) == "wemp"

    def test_第一次失败就熔断_后续不再请求(self) -> None:
        from app.services.reader_platform_client import PlatformError
        from app.services.wechat import _source as S

        be = WempClient()
        with pytest.raises(PlatformError):
            S.ask_platform(be, "MP_WXS_1")
        assert be.calls == 1, "第一次应当真的打出去"
        #: 冷却期内:**一次都不问**(与 `MultiSourceClient` 的语义一致)
        for _ in range(5):
            assert S.ask_platform(be, "MP_WXS_1") == []
        assert be.calls == 1, f"冷却期内又打了 {be.calls - 1} 次 —— 这正是那次 117 次请求的形状"

    def test_熔断是进程级的_换个客户端实例也认(self) -> None:
        from app.services.reader_platform_client import PlatformError
        from app.services.wechat import _source as S

        first, second = WempClient(), WempClient()
        with pytest.raises(PlatformError):
            S.ask_platform(first, "MP_WXS_1")
        assert S.ask_platform(second, "MP_WXS_1") == []
        assert second.calls == 0, "换了实例就当没熔断过 —— 而生产里正是每次新建"

    def test_冷却到期后重新放行(self) -> None:
        from app.services.wechat import _source as S

        be = WempClient(boom=False)
        S.trip_source("wemp")
        assert S.ask_platform(be, "MP_WXS_1") == [] and be.calls == 0
        S._SOURCE_TRIPPED["wemp"] = 0.0            #: 把冷却拨到过去
        assert S.ask_platform(be, "MP_WXS_1") == [{"title": "t", "url": "u"}]
        assert be.calls == 1

    def test_熔断只喊一次_不刷屏(self, monkeypatch) -> None:
        """★ 单源那一路会**每个号一行**。117 行 warning 会把真正的那条淹掉。"""
        from app.services.wechat import _source as S

        said: list[str] = []
        monkeypatch.setattr(S.logger, "warning",
                            lambda msg, *a, **k: said.append(str(msg)), raising=False)
        for _ in range(10):
            S.trip_source("wemp", "频率限制(200013)")
        assert len(said) == 1, f"喊了 {len(said)} 次 —— 应当只在'刚熔断'时喊一次"

    def test_多源那条不受本层影响(self, monkeypatch) -> None:
        """`MultiSourceClient` 自带**逐源**熔断 ⇒ `ask_platform` 不该在它之外再熔断整条链。

        (否则一个源坏了会把整条链都判冷 —— 那是**倒退**。)
        """
        from app.services.wechat import _source as S

        class _Multi:
            names = ["werss", "wemp"]

            def __init__(self) -> None:
                self.calls = 0

            def _cooling(self, name: str) -> bool:      #: 有它 = 自带熔断
                return False

            def mp_articles(self, mp_id: str, page: int = 1, limit: int = 20):
                self.calls += 1
                return [{"title": "t", "url": "u"}]

        be = _Multi()
        assert S.ask_platform(be, "MP_WXS_1") == [{"title": "t", "url": "u"}]
        assert S._SOURCE_TRIPPED == {}, "不该给'整条切换链'记一笔熔断"

    def test_有_mp_articles_with_reads_时优先用它(self) -> None:
        """`ask_platform` 要保住原来 `_listen` 里那段两分支逻辑(单一出处,别再写两遍)。"""
        from app.services.wechat import _source as S

        seen: dict = {}

        class _WithReads:
            def mp_articles_with_reads(self, mp_id, page=1, limit=20, stats=None):
                seen["with_reads"] = True
                return []

            def mp_articles(self, mp_id, page=1, limit=20):
                seen["plain"] = True
                return []

        S.ask_platform(_WithReads(), "MP_WXS_1", stats={})
        assert seen == {"with_reads": True}


# ---------------------------------------------------------------------------
# ★★★ 阅读数文案**不许误导**(2026-10-15,用户口径「审查一下那些容易误导的」)
#
# 审出**四处**,每一处都会**把人指向错误的动作**(照做白跑):
#   ① 「唯一带阅读数的是自研 wemp…**配好它的凭据即可**」—— 把**未验证**的事写成了结论;
#   ② 「**唯一**」在同一份体检里出现**两次**(微信读书 App / wemp 各一次)⇒ 两行互相打架;
#   ③ 「恢复与否只看 `reads(src=…)`」—— 那个计数**只在 ≥2 个源都答上话时才写**,
#      而现在是**单源** ⇒ **它永远不会出现**,一条**永远等不到**的判据;
#   ④ 文档里「凭据已死,只能人工重新扫码」—— 而**现在**返回的是 `200013 限流`(会话是好的)。


class Test阅读数文案不许误导:
    def _row(self, session):
        return _by_name(ch.check_list_sources(session))["公众号·阅读数来源"]

    def test_被限流时要写明未验证与能用得出的判据(self, session, monkeypatch) -> None:
        from app.services.wechat.wemp_client import WempRateLimited

        _bench(session)
        _patch(monkeypatch, [("wemp", _Be(exc=WempRateLimited("频率限制(200013)")))])
        detail = self._row(session)["detail"]
        assert "未验证" in detail, "wemp 带阅读数这件事**必须标明未验证**,不许写成已成立"
        assert "appmsgpublish 文章字段" in detail, "要给一个**真能等到**的判据(首次解析那条日志)"
        assert "reads(src=" in detail, "要点明旧判据在**单源**时永远不会出现"

    def test_不许再把动作指死(self, session, monkeypatch) -> None:
        """★ 「配好它的凭据即可」/「配额恢复后自动出数」都是**把动作指死**的话 ——

        前者让人白弄凭据(凭据现在本来就是好的),后者断言了没验证过的事。
        """
        from app.services.wechat.wemp_client import WempRateLimited

        _bench(session)
        _patch(monkeypatch, [("wemp", _Be(exc=WempRateLimited("频率限制(200013)")))])
        detail = self._row(session)["detail"]
        for bad in ("配好它的凭据即可", "自动出数", "唯一带阅读数"):
            assert bad not in detail, f"又写成了已经成立的事实:{bad}"

    def test_体检标签不许自称唯一来源(self) -> None:
        """★ 同一份体检里**两行都自称"唯一"** ⇒ 必有一错,而查的人会挑对他有利的那条信。"""
        assert "唯一" not in ch._WEREAD_APP_LABEL, (
            f"另一个「唯一」又回来了:{ch._WEREAD_APP_LABEL!r} —— "
            "网页路 `/web/mp/articles` 同样带精确读数(10-14 实测 ok=13 app=6)")
