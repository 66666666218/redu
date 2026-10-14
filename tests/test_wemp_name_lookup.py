"""「公众号名 → 可监听标识」**自研化**单测(2026-10-14)。

## 为什么有这条链
按名收录(`candidates` → `add_benchmark_by_name`)原来**只有一条路**:WeRSS 的 `search_mp`
(它自己就是转发后台的 `searchbiz`)。那个容器一停,后果**完全静默**:

    runs.detail 里 `listenable=0` 连续四天;最近 7 天新建 37 个号里 36 个是哑号
    (既无 biz 也无 weread_book_id)⇒ 每天 8 个收录名额全花在"建一个哑行"上。

而**这套凭据我们手里就有**,接口也没被 `200013` 压住(当日实测:`appmsgpublish` 回 200013、
`searchbiz` 回 `ret=0` 命中 3 个号)⇒ **限流按端点**,所以收录不必再依赖任何容器/镜像。

## 这里守的四条
1. `fakeid`(base64(数字))↔ `MP_WXS_<数字>` **成对**:解不出就返回空串,**不猜**;
2. `search_biz` 的错误码分流与 `mp_articles` **共用一处**(`_get_json`)—— 不许有两个版本;
3. 昵称**精确比对、重名歧义不用**(搜「小猫爱吃米」会一起回形近号,猜错=把 A 号的文章当 B 号推);
4. 端到端:WeRSS 搜不到时,**自研顶上** ⇒ `listenable=True` 且 `biz == weread_book_id`。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.db.database import Base  # noqa: E402
from app.db import models  # noqa: E402,F401
from app.db.models import WechatBenchmark  # noqa: E402
from app.services import wechat_monitor  # noqa: E402
from app.services.wechat import _source as src  # noqa: E402
from app.services.wechat import wemp_client as wc  # noqa: E402


@pytest.fixture
def session():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)()
    yield db
    db.close()


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        if isinstance(self._p, Exception):
            raise self._p
        return self._p


def _fake_get(payload, seen: list | None = None):
    def _get(url, params=None, headers=None, timeout=None):  # noqa: ANN001
        if seen is not None:
            seen.append((url, dict(params or {})))
        return _Resp(payload)
    return _get


class _Settings:
    """够用的 settings:这些用例只走"自研按名找号",不碰别的配置。"""

    brand_name = "念飞思雪"


class _FakePlat:
    """WeRSS 侧的假件:`search_mp` 返回空(模拟"容器停着/上游搜不到")。"""

    def list_feeds(self, kw: str = "", limit: int = 100, offset: int = 0):
        return []

    def search_mp(self, kw: str, limit: int = 10):
        return []

    def add_feed(self, *a, **k):        # pragma: no cover - 不该被调到
        raise AssertionError("WeRSS 搜不到时不该走 add_feed")


# ---------------------------------------------------------------- ① fakeid ↔ MP_WXS

class TestFakeidRoundTrip:
    def test_两个方向成对(self) -> None:
        assert wc.mp_id_to_fakeid("MP_WXS_3902714095") == "MzkwMjcxNDA5NQ=="
        assert wc.fakeid_to_mp_id("MzkwMjcxNDA5NQ==") == "MP_WXS_3902714095"

    @pytest.mark.parametrize("bad", ["", "   ", "不是base64!!", "YWJj", "MTIzNA=="])
    def test_解不出就是空串_不猜(self, bad: str) -> None:
        """垃圾输入返回空串是**合法结果**:编一个 id 出来会把别人的号挂上去。"""
        if bad == "MTIzNA==":            # base64("1234") → 数字,合法
            assert wc.fakeid_to_mp_id(bad) == "MP_WXS_1234"
        else:
            assert wc.fakeid_to_mp_id(bad) == ""


# ---------------------------------------------------------------- ② search_biz 客户端

class TestSearchBizClient:
    def test_解析出三个字段(self, monkeypatch) -> None:
        seen: list = []
        monkeypatch.setattr(wc.requests, "get", _fake_get({
            "base_resp": {"ret": 0},
            "list": [{"fakeid": "MzkwMjcxNDA5NQ==", "nickname": "小猫爱吃米", "alias": "abc"},
                     {"fakeid": "AAA=", "nickname": ""}],       # 没昵称的丢掉
        }, seen))
        out = wc.WempClient("ck", "tk").search_biz("小猫爱吃米")
        assert out == [{"fakeid": "MzkwMjcxNDA5NQ==", "nickname": "小猫爱吃米", "alias": "abc"}]
        url, params = seen[0]
        assert url.endswith("/cgi-bin/searchbiz")
        assert params["action"] == "search_biz" and params["query"] == "小猫爱吃米"
        assert params["token"] == "tk"

    def test_限流与会话失效各走各的异常(self, monkeypatch) -> None:
        monkeypatch.setattr(wc.requests, "get", _fake_get({"base_resp": {"ret": 200013}}))
        with pytest.raises(wc.WempRateLimited):
            wc.WempClient("ck", "tk").search_biz("x")
        monkeypatch.setattr(wc.requests, "get", _fake_get({"base_resp": {"ret": 200003}}))
        with pytest.raises(wc.WempAuthError):
            wc.WempClient("ck", "tk").search_biz("x")

    def test_非JSON不静默(self, monkeypatch) -> None:
        monkeypatch.setattr(wc.requests, "get", _fake_get(ValueError("no json")))
        with pytest.raises(wc.WempError):
            wc.WempClient("ck", "tk").search_biz("x")

    def test_空词不出网(self, monkeypatch) -> None:
        def _boom(*a, **k):              # pragma: no cover
            raise AssertionError("空关键词不该发请求")
        monkeypatch.setattr(wc.requests, "get", _boom)
        assert wc.WempClient("ck", "tk").search_biz("") == []


# ---------------------------------------------------------------- ③ 自研兜底本身

def _use_wemp_cred(monkeypatch, cred: dict | None = None) -> None:
    """把 `wemp_cred.load` 换成假凭据(它是在 `_biz_from_wemp` 里**运行时**导入的)。"""
    import app.services.wemp_cred as wcred

    monkeypatch.setattr(wcred, "load", lambda s, u: cred if cred is not None else
                        {"cookie": "ck", "token": "tk"})


def _use_hits(monkeypatch, hits: list[dict]) -> None:
    monkeypatch.setattr(wc.WempClient, "search_biz",
                        lambda self, kw, limit=10: hits)


class TestBizFromWemp:
    def test_精确同名唯一时给出_MP_WXS(self, session, monkeypatch) -> None:
        _use_wemp_cred(monkeypatch)
        _use_hits(monkeypatch, [{"fakeid": "MzkwMjcxNDA5NQ==", "nickname": "小猫爱吃米",
                                 "alias": ""}])
        assert src._biz_from_wemp(session, 1, "小猫爱吃米") == ("MP_WXS_3902714095", "")

    def test_形近号不认(self, session, monkeypatch) -> None:
        """★ 实测教训:搜「小猫爱吃米」会一起回「小猫咪爱吃米」「爱吃的小猫」——
        相似不等于同一个号,认错就是把别人的号挂上来。"""
        _use_wemp_cred(monkeypatch)
        _use_hits(monkeypatch, [{"fakeid": "AAA==", "nickname": "小猫咪爱吃米", "alias": ""}])
        biz, why = src._biz_from_wemp(session, 1, "小猫爱吃米")
        assert biz == "" and "没搜到精确同名" in why

    def test_重名歧义不猜(self, session, monkeypatch) -> None:
        _use_wemp_cred(monkeypatch)
        _use_hits(monkeypatch, [{"fakeid": "MzkwMjcxNDA5NQ==", "nickname": "重名号", "alias": ""},
                                {"fakeid": "MzkwMjcxNDA5Ng==", "nickname": "重名号", "alias": ""}])
        biz, why = src._biz_from_wemp(session, 1, "重名号")
        assert biz == "" and "歧义" in why

    def test_没有凭据时说清是凭据的问题(self, session, monkeypatch) -> None:
        import app.services.wemp_cred as wcred

        monkeypatch.setattr(wcred, "load", lambda s, u: None)
        biz, why = src._biz_from_wemp(session, 1, "小猫爱吃米")
        assert biz == "" and "凭据" in why        # 不是"没搜到" —— 两句提示方向相反

    def test_限流不抛_只如实说(self, session, monkeypatch) -> None:
        _use_wemp_cred(monkeypatch)

        def _limited(self, kw, limit=10):
            raise wc.WempRateLimited("searchbiz 频率限制(200013)")

        monkeypatch.setattr(wc.WempClient, "search_biz", _limited)
        biz, why = src._biz_from_wemp(session, 1, "小猫爱吃米")
        assert biz == "" and "200013" in why


# ---------------------------------------------------------------- ④ 端到端:WeRSS 挂了也能收录

class TestEndToEnd:
    def test_weress搜不到时自研顶上_号立刻可监听(self, session, monkeypatch) -> None:
        """★ 这条是本次改动的**核心断言**:容器停着时,"按名收录"不再建哑号。

        `listenable=True` 的依据是 `biz` 落库 —— 而 `biz` 与 `weread_book_id` 同填,
        号**立刻能走微信读书那条路被监听**,不必等 WeRSS 抓。
        """
        monkeypatch.setattr(wechat_monitor, "_platform_client", lambda settings: _FakePlat())
        _use_wemp_cred(monkeypatch)
        _use_hits(monkeypatch, [{"fakeid": "MzkwMjcxNDA5NQ==", "nickname": "小猫爱吃米",
                                 "alias": ""}])
        out = wechat_monitor.add_benchmark_by_name(session, 1, "小猫爱吃米",
                                                   settings=_Settings())
        assert out["listenable"] is True and out["biz"] == "MP_WXS_3902714095"
        row = session.scalar(select(WechatBenchmark).where(
            WechatBenchmark.nickname == "小猫爱吃米"))
        assert row.biz == out["biz"] == row.weread_book_id
        assert out["hint"] == ""              # 成了就不该留"暂无监听能力"那种话

    def test_存量哑号也给它一次机会_就地回填(self, session, monkeypatch) -> None:
        """★ **存量死账**那条:哑号已经躺在表里了,原来"同名行在就早退" ⇒ 修好解析也救不回。

        实测背景:列表源停摆那几天每天建 8 个哑号,全库 83 个;而收录作业每天重新挑到它们时
        走的正是这条早退分支 ⇒ 不补这一步,修复只对新号生效、存量永远哑着。
        """
        stuck = WechatBenchmark(user_id=1, nickname="小猫爱吃米", biz="", weread_book_id="")
        session.add(stuck)
        session.commit()
        monkeypatch.setattr(wechat_monitor, "_platform_client", lambda settings: _FakePlat())
        _use_wemp_cred(monkeypatch)
        _use_hits(monkeypatch, [{"fakeid": "MzkwMjcxNDA5NQ==", "nickname": "小猫爱吃米",
                                 "alias": ""}])
        out = wechat_monitor.add_benchmark_by_name(session, 1, "小猫爱吃米",
                                                   settings=_Settings())
        assert out["created"] is False and out["listenable"] is True
        session.refresh(stuck)
        assert stuck.biz == "MP_WXS_3902714095" and stuck.weread_book_id == stuck.biz

    def test_哑号回填_拿不到就一个字都不写(self, session, monkeypatch) -> None:
        """拿不到 id 时**必须留空**,不许写半截或写错值 —— 空 = "还不知道",写错 = 把别人的号挂上。"""
        stuck = WechatBenchmark(user_id=1, nickname="小猫爱吃米", biz="", weread_book_id="")
        session.add(stuck)
        session.commit()
        import app.services.wemp_cred as wcred

        monkeypatch.setattr(wcred, "load", lambda s, u: None)     # 没有凭据 ⇒ 解析不可用
        out = wechat_monitor.add_benchmark_by_name(session, 1, "小猫爱吃米",
                                                   settings=_Settings())
        assert out["listenable"] is False
        session.refresh(stuck)
        assert stuck.biz == "" and stuck.weread_book_id == ""

    def test_两条都不成时_hint把两边原因都说清(self, session, monkeypatch) -> None:
        """⚠️ 提示**方向**必须对:是"号没搜到"还是"我们查不了",动作完全不同。"""
        monkeypatch.setattr(wechat_monitor, "_platform_client", lambda settings: _FakePlat())
        import app.services.wemp_cred as wcred

        monkeypatch.setattr(wcred, "load", lambda s, u: None)
        out = wechat_monitor.add_benchmark_by_name(session, 1, "小猫爱吃米",
                                                   settings=_Settings())
        assert out["listenable"] is False
        assert "未搜到" in out["hint"] and "凭据" in out["hint"]


# ---------------------------------------------------------------- ⑤ 哑号筛选 + 「停用」标记

class TestDumbAndRetired:
    """「哪些算哑号、哪些已停用」**只此一处**(`_source.dumb_benchmarks`)。

    两个脚本(回填 / 诊断)都调它 —— 两处各写一套查询迟早飘,而"数字对不上"会让人
    先怀疑数据而不是怀疑判据(本仓反复吃过这个亏)。
    """

    def _b(self, session, nickname: str, biz: str = "", book: str = "", note: str = ""):
        row = WechatBenchmark(user_id=1, nickname=nickname, biz=biz,
                              weread_book_id=book, note=note)
        session.add(row)
        session.commit()
        return row

    def test_两个标识都空的才算哑号(self, session) -> None:
        self._b(session, "哑的")
        self._b(session, "有biz", biz="MP_WXS_1")
        self._b(session, "有bookid", book="MP_WXS_2")
        got = [r.nickname for r in src.dumb_benchmarks(session, 1)]
        assert got == ["哑的"]

    def test_标了停用的默认不再出现_要看全量得显式要(self, session) -> None:
        """★ 「停用」= 微信侧已无精确同名、别再重试 ⇒ 默认**不该再占诊断/回填的名额**。"""
        self._b(session, "还在试的")
        self._b(session, "已放弃的", note="停用:微信侧无精确同名(2026-10-14)")
        assert [r.nickname for r in src.dumb_benchmarks(session, 1)] == ["还在试的"]
        assert len(src.dumb_benchmarks(session, 1, include_retired=True)) == 2

    def test_停用判据只认标记词(self, session) -> None:
        """⚠️ 别用模糊匹配:`note` 里随便提一句"停用"就把它算停用的话,人写备注就会误伤。"""
        assert src.RETIRED_MARK == "停用"
        assert src.is_retired(WechatBenchmark(user_id=1, nickname="x", note="停用")) is True
        assert src.is_retired(WechatBenchmark(user_id=1, nickname="x", note="")) is False
        assert src.is_retired(WechatBenchmark(user_id=1, nickname="x", note=None)) is False
