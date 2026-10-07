"""B站对标号**投稿标题**采集单测(2026-10-05)。

链路:59 个 B站网盘推广号 → 取它们最近投稿的**标题** → 落 `hot_source_items`
(`source="bili-pan"`)→ 选题 Agent 当候选 → 回**资源库**查"这个资源我们有没有"。
(用户口径:「b站如果没有链可以只采集标题,从资源库里搜然后完善」)

⚠️ 本文件**最要紧**的一条是 `TestRateLimitIsNotSilentEmpty`:
B站 `space` 接口匿名额度极低(实测连发即 `412`/`-352`),而**"被限流"与"这号没投稿"
都表现为"拿不到条目"** —— 一旦吞成空列表,"被挡住"就会被记成"正常空轮",
与[[静默失败 = 假成功]]完全同源。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest
from datetime import datetime, timedelta  # noqa: E402
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import models  # noqa: F401
from app.db.database import Base
from app.db.models import CrossPlatformAccount, HotSourceItem, User
from app.services import bili_account_scan as bas


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    db.add(User(id=1, username="u1", email="u1@b.c", password_hash="x", enabled=True))
    db.commit()
    yield db
    db.close()


class _S:
    bili_scan_enabled = True
    bili_scan_accounts_per_run = 1
    bili_scan_tthin_below = 2


def _mk_accounts(session, n: int = 3) -> None:
    for i in range(n):
        session.add(CrossPlatformAccount(user_id=1, platform="bilibili", uid=f"60000{i}",
                                         name=f"网盘号{i}", status="active"))
    session.commit()


class TestFetchUserTitles:
    def test_正常返回标题(self, monkeypatch) -> None:
        monkeypatch.setattr(bas, "_signed_get", lambda params, referer, cookie="": {
            "code": 0, "message": "OK",
            "data": {"list": {"vlist": [
                {"title": "野鹅敢死队 经典影片", "bvid": "BV1", "created": 100},
                {"title": "", "bvid": "BV2"},                       # 空标题要丢掉
                {"title": "赤橙黄绿青蓝紫 大陆老剧", "bvid": "BV3"}]}}})
        out = bas.fetch_user_titles("650752289")
        assert [t["title"] for t in out] == ["野鹅敢死队 经典影片", "赤橙黄绿青蓝紫 大陆老剧"]
        assert out[0]["url"] == "https://www.bilibili.com/video/BV1"

    def test_空mid不请求(self, monkeypatch) -> None:
        def _boom(*a, **k):
            raise AssertionError("空 mid 不该发请求")
        monkeypatch.setattr(bas, "_signed_get", _boom)
        assert bas.fetch_user_titles("") == []


class TestRateLimitIsNotSilentEmpty:
    """★ **本文件的重点** —— 限流绝不能吞成空列表。"""

    def test_限流码要抛异常(self, monkeypatch) -> None:
        for code in (-352, -412, -509):
            monkeypatch.setattr(bas, "_signed_get",
                                lambda params, referer, cookie="", _c=code: {
                                    "code": _c, "message": "请求过于频繁", "data": None})
            with pytest.raises(bas.BiliScanError):
                bas.fetch_user_titles("650752289")

    def test_非JSON即412拦截页也要抛(self, monkeypatch) -> None:
        """B站风控返回的是 **HTML 拦截页**(HTTP 412),`.json()` 会炸 ——
        ⚠️ 这里**走真的 `_signed_get`**(只把 mixin 与 requests 换掉),
        否则测的只是"我 monkeypatch 的东西会抛",等于没测到那句 `except` 包装。
        """
        import requests

        class _Resp:
            status_code = 412
            text = "<!DOCTYPE html><html>..."

            def json(self):
                raise ValueError("Expecting value: line 1 column 1")

        monkeypatch.setattr("app.services.cross_accounts._bili_mixin", lambda: "mixin")
        monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp())
        with pytest.raises(bas.BiliScanError) as ei:
            bas.fetch_user_titles("650752289")
        assert "非 JSON" in str(ei.value) and "412" in str(ei.value), \
            f"错误信息要能一眼看出是风控拦截,实际:{ei.value}"

    def test_其它非零码也要抛(self, monkeypatch) -> None:
        monkeypatch.setattr(bas, "_signed_get", lambda *a, **k: {
            "code": -404, "message": "啥都没有", "data": None})
        with pytest.raises(bas.BiliScanError):
            bas.fetch_user_titles("650752289")


class TestScanAccounts:
    def test_标题落进热榜表且source正确(self, session, monkeypatch) -> None:
        _mk_accounts(session, 1)
        monkeypatch.setattr(bas, "fetch_user_titles", lambda mid, **k: [
            {"title": f"资源{i}", "bvid": f"BV{i}", "url": f"u{i}", "created": 0}
            for i in range(1, 4)])
        out = bas.scan_accounts(session, 1, settings=_S())
        assert out["status"] == "ok" and out["titles"] == 3
        rows = session.scalars(select(HotSourceItem).order_by(HotSourceItem.rank)).all()
        # `rank` 从 1 开始(第 1 条最新)—— Agent 只取 `rank <= 10`,正好是"最近在推什么"
        assert [r.rank for r in rows] == [1, 2, 3]
        assert {r.source for r in rows} == {"bili-pan"}
        assert rows[0].extra == "网盘号0", "extra 要记账号名,排障时能认出是谁发的"

    def test_轮转先扫没扫过的_空壳排最后(self, session, monkeypatch) -> None:
        """★ 2026-10-05 改:**不再用"游标 % 总数"**,改成按扫描状态排序取队首。

        动机是实测出来的:轮到第 1 个号(uid 650752289)时它**一条投稿都没有**,白烧一轮
        space 额度(而该端点额度极紧)。旧写法还有第二个毛病:中途增删号会让窗口错位、有的号被跳过。
        新顺序:**没扫过的 → 扫过的(最久没扫优先)→ 已知空壳**。
        """
        from datetime import datetime as _dt

        _mk_accounts(session, 3)
        # 0 号扫过且非空、1 号扫过但是**空壳**、2 号**从未扫过**
        a0, a1, a2 = session.scalars(select(CrossPlatformAccount).order_by(
            CrossPlatformAccount.id)).all()
        a0.last_scan_at, a0.video_count = _dt(2026, 10, 1), 30
        a1.last_scan_at, a1.video_count = _dt(2026, 10, 4), 0
        session.commit()

        seen: list[str] = []
        monkeypatch.setattr(bas, "fetch_user_titles",
                            lambda mid, **k: seen.append(mid) or [])
        monkeypatch.setattr(bas.time, "sleep", lambda s: None)

        class _S3(_S):
            bili_scan_accounts_per_run = 1
        for _ in range(3):
            bas.scan_accounts(session, 1, settings=_S3())
        assert seen == ["600002", "600000", "600001"], \
            f"顺序应为'没扫过 → 最久没扫 → 空壳',实际 {seen}"

    def test_扫描后记录投稿数(self, session, monkeypatch) -> None:
        """**"59 个号里有多少空壳"只能靠这个字段量出来** —— space 端点限流紧,
        不可能为了统计专门扫一圈。"""
        _mk_accounts(session, 1)
        monkeypatch.setattr(bas, "fetch_user_titles",
                            lambda mid, **k: [{"title": "资源", "bvid": "B", "url": "u", "created": 0}])
        monkeypatch.setattr(bas.time, "sleep", lambda s: None)
        bas.scan_accounts(session, 1, settings=_S())
        acc = session.scalars(select(CrossPlatformAccount)).one()
        assert acc.video_count == 1 and acc.last_scan_at is not None
        s = bas.empty_account_summary(session, 1)
        # ⚠️ 抽到 1 条 ⇒ 按新定义(thin_below=2)这号算「极少」,不是 0
        assert s == {"total": 1, "scanned": 1, "empty": 0, "thin": 1, "unscanned": 0}

    def test_没有对标号时返回no_accounts(self, session) -> None:
        assert bas.scan_accounts(session, 1, settings=_S())["status"] == "no_accounts"


class TestTickOnFailure:
    def test_限流时不记扫描状态_下一轮重来(self, session, monkeypatch) -> None:
        """⚠️ 被限流就**不能把那个号记成"扫过了"** —— 否则它会带着 `video_count=-1`(或旧值)
        被排到队尾,**而它恰恰是"还没成功采过"的那个**。旧写法盯的是游标,现在盯 `last_scan_at`。"""
        _mk_accounts(session, 3)
        # ⚠️ tick 里是 `from app.db import get_session_local`(**调用时才 import**),
        # 所以要打到 `app.db` 上,打到本模块会 AttributeError。
        monkeypatch.setattr("app.db.get_session_local", lambda: (lambda: session))
        monkeypatch.setattr(bas, "_settings", lambda: _S())
        monkeypatch.setattr(bas, "fetch_user_titles",
                            lambda mid, **k: (_ for _ in ()).throw(
                                bas.BiliScanError("B站限流 code=-352")))

        bas.bili_account_scan_tick(settings=_S())

        for acc in session.scalars(select(CrossPlatformAccount)).all():
            assert acc.last_scan_at is None, "被挡住那轮不该把号记成已扫过"
            assert acc.video_count == -1


class TestBiliCookie:
    """★ space 端点的风控**按 IP 类别**区别对待(受控实测):本机家宽可用、机房 IP 稳定
    `-352 风控校验失败`。登录态能显著放宽风控,所以 cookie 必须真的带上。

    取值口径与 `pan_discovery` 取夸克 cookie 一致:**先 cookie_store,再 `.env` 兜底**
    (后者是给远程用的 —— `user_cookies` 表不在 `remote_sync` 的同步清单里)。
    """

    class _SWithCookie:
        bili_scan_enabled = True
        bili_scan_accounts_per_run = 1
        bili_cookie = "SESSDATA=fromenv; bili_jct=x"

    def test_优先用cookie_store_其次env兜底(self, session) -> None:
        from app.services import cookie_store

        # 库里没有 → 用 .env
        assert bas._bili_cookie(session, 1, self._SWithCookie()) == "SESSDATA=fromenv; bili_jct=x"
        # 库里有 → 库里优先(与夸克那套口径一致)
        cookie_store.set_cookie(session, 1, "bilibili", "SESSDATA=fromdb; bili_jct=y")
        session.commit()
        assert bas._bili_cookie(session, 1, self._SWithCookie()) == "SESSDATA=fromdb; bili_jct=y"

    def test_两处都没有就返回空串而不是报错(self, session) -> None:
        assert bas._bili_cookie(session, 1, _S()) == "", "没配就匿名跑,不该抛"

    def test_扫描时cookie真的传给了抓取函数(self, session, monkeypatch) -> None:
        """防"写了取值函数却忘了传参" —— 那等于没配。"""
        from app.services import cookie_store

        _mk_accounts(session, 1)
        cookie_store.set_cookie(session, 1, "bilibili", "SESSDATA=db; bili_jct=y")
        session.commit()
        seen: list[str] = []
        monkeypatch.setattr(bas, "fetch_user_titles",
                            lambda mid, **k: seen.append(k.get("cookie", "<未传>")) or [])
        monkeypatch.setattr(bas.time, "sleep", lambda s: None)
        bas.scan_accounts(session, 1, settings=_S())
        assert seen == ["SESSDATA=db; bili_jct=y"], f"cookie 没传到抓取函数,实际 {seen}"


class TestCookieWhitespace:
    """★ 2026-10-05 **实测踩到**的坑:`.env` 的值在**行尾**,带 `\r` 是常态,
    而 requests 见到 header 头尾有空白会直接抛
    `Invalid leading whitespace, reserved character(s), or return character(s) in header value`
    —— 报错措辞完全看不出"是 cookie 带了回车"。
    第一次把 cookie 送上远程就是这么炸的。
    """

    def test_cookie带行尾回车不会炸且被strip(self, monkeypatch) -> None:
        import requests

        seen: dict = {}

        class _Resp:
            status_code = 200

            def json(self):
                return {"code": 0, "message": "OK", "data": {"list": {"vlist": []}}}

        def fake_get(url, headers=None, timeout=None):
            seen.update(headers or {})
            return _Resp()

        monkeypatch.setattr("app.services.cross_accounts._bili_mixin", lambda: "mixin")
        monkeypatch.setattr(requests, "get", fake_get)
        bas.fetch_user_titles("123", cookie="SESSDATA=abc; bili_jct=x\r\n  ")
        assert seen.get("Cookie") == "SESSDATA=abc; bili_jct=x", \
            f"cookie 必须 strip 掉行尾回车/空格,实际 {seen.get('Cookie')!r}"

    def test_空cookie不设header(self, monkeypatch) -> None:
        import requests

        seen: dict = {}

        class _Resp:
            status_code = 200

            def json(self):
                return {"code": 0, "data": {"list": {"vlist": []}}}

        monkeypatch.setattr("app.services.cross_accounts._bili_mixin", lambda: "mixin")
        monkeypatch.setattr(requests, "get",
                            lambda url, headers=None, timeout=None: (seen.update(headers or {}), _Resp())[1])
        bas.fetch_user_titles("123", cookie="   ")
        assert "Cookie" not in seen, "空白 cookie 不该设出空 header"


class TestVerifyLogin:
    """★ cookie 会过期,而**过期后的症状是"采集突然全失败"**(space 端点退回匿名风控)。

    ⚠️ B站**失败也回 HTTP 200** —— 判据只能是 `data.isLogin`,按"HTTP 通了"判
    就会把"cookie 已失效"读成"一切正常"。
    """

    def test_未登录要判成失效(self, monkeypatch) -> None:
        import requests

        class _R:
            status_code = 200

            def json(self):
                # ⚠️ **HTTP 200 但 code=-101、isLogin=False** —— 这正是踩点
                return {"code": -101, "message": "账号未登录", "data": {"isLogin": False}}

        monkeypatch.setattr(requests, "get", lambda *a, **k: _R())
        st = bas.verify_login("SESSDATA=expired")
        assert st["is_login"] is False, "HTTP 200 + isLogin=False 必须判成失效"

    def test_已登录要判成有效(self, monkeypatch) -> None:
        import requests

        class _R:
            status_code = 200

            def json(self):
                return {"code": 0, "data": {"isLogin": True, "uname": "bili_xxx", "mid": 123}}

        monkeypatch.setattr(requests, "get", lambda *a, **k: _R())
        st = bas.verify_login("SESSDATA=good")
        assert st["is_login"] is True and st["uname"] == "bili_xxx"

    def test_没配cookie直接判失效(self) -> None:
        assert bas.verify_login("")["is_login"] is False

    def test_网络异常与失效要分开报(self, monkeypatch) -> None:
        """网络抖一下 ≠ cookie 失效 —— 报错里要能区分,否则会误报"去重扫"。"""
        import requests

        def _boom(*a, **k):
            raise ConnectionError("超时")
        monkeypatch.setattr(requests, "get", _boom)
        st = bas.verify_login("SESSDATA=x")
        assert st["is_login"] is False and "ConnectionError" in st["reason"]

    def test_失效时推告警(self, session, monkeypatch) -> None:
        sent: list[tuple] = []
        monkeypatch.setattr("app.services.alert_service.notify_incident",
                            lambda *a, **k: sent.append(a) or True)
        bas._alert_cookie_dead(session, 1, "账号未登录")
        assert sent, "cookie 失效必须推告警(要人重新扫码)"
        assert "重扫码" in str(sent[0]) or "bili_login" in str(sent[0])


class TestThinAccountsAlsoDeprioritized:
    """★ **2026-10-06 首夜实测发现的缺口**:原来**只有 `video_count == 0` 才降权**,
    而真实分布是 —— **空壳只有 1 个,却有 2 个号只有 1 条投稿**
    (`网盘资源分发` / `-网盘资源官-`)。

    它们不是空的,但**实质产出与空壳无异**,每次轮到都白烧一次本就紧张的 space 额度。
    所以轮转扩成**四档**:**没扫过(0) → 正常(1) → 内容极少(2) → 空壳(3)**。
    """

    def _mk4(self, session):
        """造四个号,分别落在四档上。"""
        from datetime import datetime as _dt

        _mk_accounts(session, 4)
        a0, a1, a2, a3 = session.scalars(select(CrossPlatformAccount).order_by(
            CrossPlatformAccount.id)).all()
        # a0 = 从未扫过(最高优先级)
        a1.last_scan_at, a1.video_count = _dt(2026, 10, 1), 30      # 正常
        a2.last_scan_at, a2.video_count = _dt(2026, 10, 2), 1       # 极少
        a3.last_scan_at, a3.video_count = _dt(2026, 10, 3), 0       # 空壳(最低)
        session.commit()
        return a0, a1, a2, a3

    def test_四档顺序_空壳最后_极少在正常之后(self, session, monkeypatch) -> None:
        self._mk4(session)
        seen: list[str] = []
        monkeypatch.setattr(bas, "fetch_user_titles",
                            lambda mid, **k: seen.append(mid) or [])
        monkeypatch.setattr(bas.time, "sleep", lambda s: None)

        class _S4(_S):
            bili_scan_accounts_per_run = 1
        for _ in range(4):        # 每次扫 1 个,连扫 4 轮看顺序
            bas.scan_accounts(session, 1, settings=_S4())
        assert seen == ["600000", "600001", "600002", "600003"], \
            f"顺序应为'没扫过 → 正常 → 极少 → 空壳',实际 {seen}"

    def test_极少号排在空壳之前(self, session) -> None:
        """细化断言:同为'已扫过且内容少',**1 条的应当比 0 条的**先被扫。"""
        _, a1, a2, a3 = self._mk4(session)
        prio = bas._scan_priority(2)
        # 直接比大小:case 表达式的值越小越靠前
        vals = {a.uid: session.scalar(select(prio.label("p")).where(
            CrossPlatformAccount.id == a.id)) for a in (a1, a2, a3)}
        assert vals[a2.uid] < vals[a3.uid], f"极少号应当排在空壳之前:{vals}"
        assert vals[a1.uid] < vals[a2.uid], f"正常号应当排在极少号之前:{vals}"

    def test_配置为0时退回只降权空壳(self, session) -> None:
        """`BILI_SCAN_THIN_BELOW=0` 是**关掉这条**的开关 —— 关掉后 1 条的号不再被降权。"""
        expr = bas._scan_priority(0)
        text = str(expr.compile(compile_kwargs={"literal_binds": True})).replace("\n", " ")
        assert "video_count < 0" not in text, f"tthin_below=0 时不该再有'极少'那一档:{text[:160]}"

    def test_汇总把空壳与极少分开报(self, session) -> None:
        """⚠️ **两者分开报是有意的**:并成一个数就看不出"是没人发内容,还是号本身没内容"。"""
        self._mk4(session)
        s = bas.empty_account_summary(session, 1, _S())
        assert s == {"total": 4, "scanned": 3, "empty": 1, "thin": 1, "unscanned": 1}, s


class TestPublishCutoff:
    """★ **按投稿时间过滤**(2026-10-06 用户口径:「2026年10月份之前的不要再保存进来了」)。

    `fetch_user_titles` **本来就把 `created` 取回来了**(接口一直给),
    只是下游从来没人用 ⇒ 于是"扫到 30 条"里混着好几年前的老视频,全当新内容喂给 Agent。
    **注意区分两个时间**:`created` 是**视频上传时间**(新鲜度的真依据),
    而标题里的"1978年出品"是**影片年份**,跟新鲜度无关 —— 别拿那个当判据。
    """

    def _mk(self, session, created_list):
        _mk_accounts(session, 1)
        acc = session.scalars(select(CrossPlatformAccount)).one()
        titles = [{"title": f"资源{i}", "bvid": f"B{i}", "url": "u", "created": c}
                  for i, c in enumerate(created_list)]
        return acc, titles

    def test_早于下限的不入库(self, session) -> None:
        import datetime as dt
        from app.services.bili_account_scan import _save_titles

        acc, titles = self._mk(session, [
            int(dt.datetime(2026, 10, 5).timestamp()),
            int(dt.datetime(2026, 1, 1).timestamp()),      # 早于下限
        ])

        class _S2(_S):
            content_min_publish_date = "2026-10-01"
            douyin_leads_min_publish_date = ""
        n = _save_titles(session, 1, acc, titles, _S2())
        session.commit()
        rows = session.scalars(select(HotSourceItem)).all()
        assert n == 1 and len(rows) == 1, f"老投稿必须被丢掉,实际入库 {n}"
        assert rows[0].published_at is not None, "要记下发布时间(新鲜度的真依据)"

    def test_缺时间的不入库但不算静默(self, session) -> None:
        from app.services.bili_account_scan import _save_titles

        acc, titles = self._mk(session, [0])
        class _S2(_S):
            content_min_publish_date = "2026-10-01"
            douyin_leads_min_publish_date = ""
        assert _save_titles(session, 1, acc, titles, _S2()) == 0

    def test_投稿数记的是原始条数不是过滤后的(self, session, monkeypatch) -> None:
        """⚠️ `video_count` 必须是**接口返回的原始条数** —— 否则一个投稿全是老视频的号
        会被误判成"空壳"(`video_count == 0`),那是另一套降权逻辑的判据。"""
        import datetime as dt

        _mk_accounts(session, 1)
        monkeypatch.setattr(bas, "fetch_user_titles", lambda mid, **k: [
            {"title": "老的", "bvid": "B", "url": "u", "created": int(dt.datetime(2020, 1, 1).timestamp())}])
        monkeypatch.setattr(bas.time, "sleep", lambda s: None)
        monkeypatch.setattr(bas, "_settings", lambda: _S_withcut())
        bas.scan_accounts(session, 1, settings=_S_withcut())
        acc = session.scalars(select(CrossPlatformAccount)).one()
        assert acc.video_count == 1, f"原始投稿数应为 1,实际 {acc.video_count}"
        assert session.scalars(select(HotSourceItem)).all() == [], "老投稿不该入库"


def _S_withcut():
    class _C(_S):
        content_min_publish_date = "2026-10-01"
        douyin_leads_min_publish_date = ""
    return _C()


class TestRateLimitKeepsWorkDone:
    """★ **被挡之前扫到的号必须留在库里**(2026-10-06 修)。

    `scan_accounts` 里 `fetch_user_titles` 一旦抛(限流/风控),原来那次 `commit()`
    **根本走不到**(它在循环之后)⇒ 前面已经成功、**已经花掉本就紧张的 space 额度**
    的号,一起被回滚。与"转存成功了但记录随那一轮丢掉"是**同一个病**:**额度花了,账没了**。

    而它正是**提速的前置条件**:每轮从 1 提到 6,一次限流丢的就成倍放大。
    """

    class _S3:
        """⚠️ 必须把 `per_run` 调大于 1 —— 否则本轮只挑得到 1 个号,
        "第二个号被挡"这个场景根本构造不出来(第一版就栽在这)。"""
        bili_scan_enabled = True
        bili_scan_accounts_per_run = 3
        bili_scan_thin_below = 2

    def _patch(self, monkeypatch, how) -> None:
        monkeypatch.setattr(bas, "fetch_user_titles", how)
        monkeypatch.setattr(bas.time, "sleep", lambda s: None)   # 别真睡 2 秒

    def test_第二个号被挡_第一个号的战果要留下(self, session, monkeypatch) -> None:
        _mk_accounts(session, 3)
        n = {"i": 0}

        def _fetch(uid, **k):
            n["i"] += 1
            if n["i"] >= 2:
                raise bas.BiliScanError("B站限流 code=-352 风控校验失败")
            return [{"title": "某资源分享", "url": "https://www.bilibili.com/video/BV1",
                     "publish_at": None}]
        self._patch(monkeypatch, _fetch)

        with pytest.raises(bas.BiliScanError):
            bas.scan_accounts(session, 1, self._S3())

        done = session.scalars(select(CrossPlatformAccount).where(
            CrossPlatformAccount.last_scan_at.isnot(None))).all()
        assert done, ("被挡**之前**已经扫到的号被整轮回滚了 —— "
                      "额度花了、账没了;而且这正是'每轮多扫几个'的前提")

    def test_每号都落盘_不是攒到轮末(self, session, monkeypatch) -> None:
        """3 个号全成功时,每扫完一个就该能看到一个(而不是轮末一次性)。"""
        _mk_accounts(session, 3)
        seen: list[int] = []
        real_commit = session.commit

        def _commit():
            real_commit()
            seen.append(len(session.scalars(select(CrossPlatformAccount).where(
                CrossPlatformAccount.last_scan_at.isnot(None))).all()))
        monkeypatch.setattr(session, "commit", _commit)
        self._patch(monkeypatch, lambda uid, **k: [
            {"title": "某资源", "url": "https://www.bilibili.com/video/BV1", "publish_at": None}])

        bas.scan_accounts(session, 1, self._S3())
        # 每号一次 commit ⇒ 进度应当是逐个递增,不该只有一个"3"
        assert len([x for x in seen if x > 0]) >= 3, f"应当每号落盘,实际 commit 后进度:{seen}"


class TestOfftopicFilter:
    """★ 滤掉**涨粉/互动**标题,但**绝不许误杀资源标题**。

    ⚠️ 这个类里两条用例的数据**全部取自生产库**(2026-10-07 抓的 121 条 bili-pan 标题)。
    为什么要拿真实数据钉:我自己在这一步**差点用错判据** —— 一开始拿"含不含
    `资源/网盘/链接` 字样"当判据,于是把「野鹅敢死队 经典影片 国语配音」这种
    **真资源标题**判成了"纯互动内容",差点得出"B站这条路没价值"的错结论。
    资源名本来就长那样,**不带"资源"两个字**。
    """

    #: 生产库里真实采到的**涨粉/互动**标题(该滤)
    涨粉 = [
        "盘盘大型“追粉”现场！路过的宝宝～请别划走🙏成为盘粉好吗？盘盘宠你！！！",
        "难度升级！只亮一个看谁能成功？",
        "截图挑战！下次和好朋友就这样拍～",
        "等一下！盘盘有话对你说...",
    ]

    #: **已知漏网**:这条也是涨粉号发的内容,但它**一个涨粉特征词都不含**。
    #: ⚠️ 我**不给它加规则** —— 为一条样本加规则就是过拟合,而且会拉高误杀风险
    #: (本仓的教训:判据选错会得出自信的错结论)。这里把它显式记下来,
    #: 是为了让下一个人知道**这条过滤器的边界在哪**,而不是以为它滴水不漏。
    已知漏网 = ["换装变戏法~"]

    #: 生产库里真实采到的**资源**标题(**一条都不许滤**)
    资源 = [
        "野鹅敢死队  经典影片  国语配音",
        "别墅阴影  大陆老剧  经典电视剧  怀旧 陈道明主演",
        "泰星来客 经典美剧 国语配音  第一季",
        "霍桑探案  大陆老剧 经典电视剧 怀旧",
        "【车载U盘】45分钟近期爆火音乐合集，开听！",
        "孩子最爱动画版《秦王统一六国》",
        "微信短剧机器人详细搭建教程，半小时内搭建属于自己的机器人",
        "短剧推荐《试用夫人是大佬（80集）》",
        "《最奇妙的蛋》绘本故事",
    ]

    def test_涨粉标题被滤掉(self) -> None:
        from app.services.bili_account_scan import is_offtopic_title

        missed = [t for t in self.涨粉 if not is_offtopic_title(t)]
        assert not missed, f"这些涨粉标题没被滤掉:{missed}"

    def test_资源标题一条都不许误杀(self) -> None:
        """★ 误杀的代价比漏滤大得多 —— 那是**真资源**,漏了就永久丢了。"""
        from app.services.bili_account_scan import is_offtopic_title

        killed = [t for t in self.资源 if is_offtopic_title(t)]
        assert not killed, f"**资源标题被误杀了**:{killed}"

    def test_判据要两边都看(self) -> None:
        """只判一半都会坏:只看涨粉词会误杀正经标题,只看内容词会漏掉大量涨粉号。"""
        from app.services.bili_account_scan import is_offtopic_title

        # 含涨粉词 + 含内容特征 ⇒ **不滤**(保守:宁漏滤不误杀)
        assert not is_offtopic_title("【教程】三连关注我就能看全集")
        # 含内容特征但没涨粉词 ⇒ 不滤
        assert not is_offtopic_title("经典老电影 国语配音")
        # 空标题不滤(交给别的环节处理)
        assert not is_offtopic_title("")

    def test_滤掉的条数要报出来(self, session) -> None:
        """⚠️ **静默丢数据和静默失败是同一个病** —— 必须能看见滤了多少。"""
        from app.db.models import CrossPlatformAccount
        from app.services.bili_account_scan import _save_titles

        acc = CrossPlatformAccount(user_id=1, platform="bilibili", uid="u1", name="号")
        session.add(acc)
        session.commit()
        from datetime import datetime as _dt

        titles = [{"title": t, "created": int(_dt.now().timestamp())}
                  for t in self.涨粉[:3] + self.资源[:2]]
        stats: dict = {}
        n = _save_titles(session, 1, acc, titles, _S(), stats=stats)
        assert n == 2, f"应只写入 2 条资源标题,实际 {n}"
        assert stats["offtopic"] == 3, stats

    def test_已知漏网要被显式记着_而不是假装没有(self) -> None:
        """★ 过滤器的**边界**也要有测试:哪天它变强了,这条会红,提醒去更新认知。"""
        from app.services.bili_account_scan import is_offtopic_title

        assert not any(is_offtopic_title(t) for t in self.已知漏网), (
            "这条已经能被滤掉了 —— 把 `已知漏网` 里的条目挪进 `涨粉`")


class TestAdaptiveCooldown:
    """★ **内容没变就给冷却**(2026-10-07,用户口径「B站每轮采的都是相同的」)。

    实测:59 个号里能出内容的只有 ~14 个,9 个/小时 ⇒ 每个号 1.5 小时被重扫一次,
    而它们**发帖很慢** ⇒ 多数轮次采回同一批标题,白烧 space 额度。
    规则:这次指纹和上次一样 ⇒ 冷却一段时间;**变了就不冷却**(发得勤就扫得勤)。
    """

    def _acc(self, session, uid_: str, name: str, **kw):
        from app.db.models import CrossPlatformAccount

        a = CrossPlatformAccount(user_id=1, platform="bilibili", uid=uid_, name=name, **kw)
        session.add(a)
        session.commit()
        return a

    def test_指纹与顺序无关(self) -> None:
        from app.services.bili_account_scan import _titles_fp

        a = [{"title": "甲"}, {"title": "乙"}]
        assert _titles_fp(a) == _titles_fp(list(reversed(a)))
        assert _titles_fp(a) != _titles_fp([{"title": "甲"}])

    def test_内容没变就设冷却(self, session, monkeypatch) -> None:
        """★ 核心行为:**指纹没变 ⇒ 给这个号设冷却**;变了 ⇒ 清掉冷却。

        ⚠️ 测法说明:冷却是在**那一轮扫描时**才写的,所以"下轮跳过"要**预先**把
        指纹设成与本次采到的一致(模拟"它上轮已经扫过、内容没变")。
        """
        from app.services import bili_account_scan as bas

        class _S:
            bili_scan_accounts_per_run = 1
            bili_scan_thin_below = 2
            bili_scan_cooldown_hours = 4

        titles = [{"title": "永远一样的标题",
                   "created": int(datetime.now().timestamp())}]
        monkeypatch.setattr(bas, "fetch_user_titles", lambda uid, **k: list(titles))

        a = self._acc(session, "u1", "甲", last_scan_at=datetime.now(),
                      last_titles_fp=bas._titles_fp(titles))   # ← 指纹与本次相同
        bas.scan_accounts(session, 1, settings=_S())
        session.refresh(a)
        assert a.next_scan_after is not None, "内容没变却没设冷却 ⇒ 下轮还会重扫同一个号"
        assert a.next_scan_after > datetime.now()

    def test_内容变了就清冷却(self, session, monkeypatch) -> None:
        """反例:号发了新东西 ⇒ **必须立刻清掉冷却**,别把它按在冷板凳上。"""
        from app.services import bili_account_scan as bas

        class _S:
            bili_scan_accounts_per_run = 1
            bili_scan_thin_below = 2
            bili_scan_cooldown_hours = 4

        monkeypatch.setattr(bas, "fetch_user_titles", lambda uid, **k: [
            {"title": "新投稿！", "created": int(datetime.now().timestamp())}])
        a = self._acc(session, "u1", "甲", last_scan_at=datetime.now(),
                      last_titles_fp="老指纹不一样")
        bas.scan_accounts(session, 1, settings=_S())
        session.refresh(a)
        assert a.next_scan_after is None
        assert a.last_titles_fp == bas._titles_fp([{"title": "新投稿！"}])

    def test_冷却中的号会被跳过(self, session, monkeypatch) -> None:
        from app.services import bili_account_scan as bas

        class _S:
            bili_scan_accounts_per_run = 1
            bili_scan_thin_below = 2
            bili_scan_cooldown_hours = 4

        calls: list[str] = []
        monkeypatch.setattr(bas, "fetch_user_titles", lambda uid, **k: (
            calls.append(uid) or [{"title": "x", "created": int(datetime.now().timestamp())}]))
        sticky = self._acc(session, "u1", "冷却中的", last_scan_at=datetime.now(),
                           next_scan_after=datetime.now() + timedelta(hours=3))
        self._acc(session, "u2", "可扫的", last_scan_at=datetime.now())
        out = bas.scan_accounts(session, 1, settings=_S())
        assert out["cooling"] >= 1, "冷却没进运行记录 ⇒ '这轮怎么没扫那个号'答不出来"
        assert sticky.uid not in calls, "冷却中的号还是被扫了"

    def test_全在冷却里也不停摆(self, session, monkeypatch) -> None:
        """⚠️ 池子一起进冷却时必须**忽略冷却照常扫** —— 否则这个链会安静地什么都不做。"""
        from app.services import bili_account_scan as bas

        class _S3:
            bili_scan_accounts_per_run = 1
            bili_scan_thin_below = 2
            bili_scan_cooldown_hours = 4

        calls: list[str] = []
        monkeypatch.setattr(bas, "fetch_user_titles", lambda uid, **k: (
            calls.append(uid) or [{"title": "x", "created": int(datetime.now().timestamp())}]))
        far = datetime.now() + timedelta(hours=10)
        self._acc(session, "u1", "甲", last_scan_at=datetime.now(), next_scan_after=far)
        self._acc(session, "u2", "乙", last_scan_at=datetime.now(), next_scan_after=far)
        out = bas.scan_accounts(session, 1, settings=_S3())
        assert calls, "全都冷却中却一个都没扫 —— 停摆了"
        assert out["scanned"] >= 1
