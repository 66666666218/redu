"""网盘资源发现单测(2026-10-02):**直链型**那条链 —— 搜知乎 → 抽盘链 → 按链分发转存。

与抖音那条(口令型)互补:抖音的线索是《群名》,知乎的线索是**现成的夸克/百度链**。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db import models  # noqa: F401
from app.db.models import DiscoveredPanLink, User
from app.services import cookie_store, pan_discovery as pd


@pytest.fixture(autouse=True)
def _browser_path_for_tieba(monkeypatch):
    """本文件验的是**接线与失败语义**,不是"从哪取数" ⇒ 把贴吧钉死走浏览器那条。

    ⚠️ 不加这个会出事:贴吧从 2026-10-08 起**纯协议优先**,而这些用例只打了
    `mediacrawler_source.crawl` 的桩 ⇒ `_candidates_from_tieba` 会**真的往贴吧发请求**,
    而且协议一旦成功,那些"crawl 抛错"的用例就永远走不到,断言全错
    (实测:3 条用例红在这里)。
    纯协议那条路由 `tests/test_tieba_search.py` 专门验。
    """
    from config.settings import get_settings

    monkeypatch.setattr(get_settings(), "tieba_prefer_protocol", False, raising=False)


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
    brand_name = "念飞思雪"
    feishu_webhook_zhihu = "https://example.com/zhihu"
    feishu_webhook = ""
    feishu_secret = ""
    pan_discovery_keywords = 3
    # 两份额度(2026-10-05 拆开):**积压**与**新发现**各算各的,见 `sync()`。
    # 单测里都设 2,好让"额度"这件事一眼可算。
    pan_discovery_transfer_limit = 2          # 积压额度
    pan_discovery_fresh_limit = 2             # 新发现额度
    # ⚠️ **单测里必须关掉贴吧源**:它走 MediaCrawler,一跑就是 30s+ 真浏览器(实测一次
    # 让本文件从 9s 涨到 259s)。要测它的分支请单独 patch `crawl`,别让它出网。
    pan_discovery_tieba = False


class TestSearchWords:
    """知乎搜索词必须**带限定后缀**(2026-10-03 实测,同账号同时段对照)。

    裸词命中 **0%**(鞠婧祎/苏超/兰香如故 48 条结果 → 0 条盘链);
    补上"网盘/全集/教程/资料"后 **17.8%**(107 条 → 19 条)。
    原因是知乎的盘链回答集中在**资料/合集**类问题,裸剧名搜到的全是剧情讨论。
    所以 `_search_words` 给每个动态词补后缀 —— 这条测试防的就是"有人把后缀优化没了"。
    """

    def _st(self, **kw):
        base = {"pan_discovery_suffix": " 网盘", "pan_discovery_terms": ""}
        base.update(kw)
        return type("S", (), base)()

    def test_dynamic_words_get_the_suffix(self, session, monkeypatch) -> None:
        monkeypatch.setattr("app.services.douyin_leads.search_keywords",
                            lambda s, u, t, st: ["兰香如故", "四级真题"])
        out = pd._search_words(session, 1, 5, self._st())
        assert out == ["兰香如故 网盘", "四级真题 网盘"]

    def test_terms_take_priority_but_respect_top(self, session, monkeypatch) -> None:
        """资料词表**排在前面**且吃掉名额 —— 它命中率最高,该优先。`top` 是上限不是目标。"""
        monkeypatch.setattr("app.services.douyin_leads.search_keywords",
                            lambda s, u, t, st: ["动态甲", "动态乙"])
        out = pd._search_words(session, 1, 2, self._st(
            pan_discovery_terms="四级真题 网盘, PS教程 全套 网盘, 考公资料 网盘"))
        assert out == ["四级真题 网盘", "PS教程 全套 网盘"]      # 截到 top=2,动态词没位置

    def test_no_duplicate_when_term_matches_dynamic_plus_suffix(self, session, monkeypatch) -> None:
        monkeypatch.setattr("app.services.douyin_leads.search_keywords", lambda s, u, t, st: ["甲"])
        out = pd._search_words(session, 1, 5, self._st(pan_discovery_terms="甲 网盘"))
        assert out == ["甲 网盘"]

    def test_empty_suffix_leaves_words_untouched(self, session, monkeypatch) -> None:
        """后缀可关(留空即不补)—— 给"想原样搜"的场合留个口子。"""
        monkeypatch.setattr("app.services.douyin_leads.search_keywords", lambda s, u, t, st: ["甲"])
        assert pd._search_words(session, 1, 5, self._st(pan_discovery_suffix="")) == ["甲"]


class TestTiebaSource:
    """贴吧源(2026-10-03 加):实测命中 **56%**,是知乎(17.8%)的 3 倍,故为**主源**。

    它走 MediaCrawler(真浏览器),所以这些用例一律 patch 掉,只验**接线与失败语义**。
    """

    def _st(self, **kw):
        base = {"pan_discovery_tieba": True}
        base.update(kw)
        return type("S", (), base)()

    def test_candidates_from_tieba_drops_rows_without_pan_link(self, monkeypatch) -> None:
        from app.services import mediacrawler_source
        monkeypatch.setattr(mediacrawler_source, "crawl", lambda p, kws, timeout=600: [
            {"uid": "1", "name": "甲", "url": "u1", "snippet": "有链",
             "pan_link": "https://pan.baidu.com/s/A"},
            {"uid": "2", "name": "乙", "url": "u2", "snippet": "没链", "pan_link": ""}])
        out = pd._candidates_from_tieba(["词"])
        assert len(out) == 1 and out[0]["platform"] == "tieba"
        assert out[0]["origin_url"] == "https://pan.baidu.com/s/A"

    def test_tieba_alone_can_produce_candidates_without_zhihu_cookie(self, session, monkeypatch) -> None:
        """**没配知乎 Cookie 也要能用** —— 贴吧是主源,不该被知乎的缺失连累。"""
        monkeypatch.setattr("app.services.cookie_store.get_cookie", lambda s, u, p: "")
        monkeypatch.setattr(pd, "_candidates_from_tieba", lambda kws: [
            {"platform": "tieba", "origin_url": "https://pan.baidu.com/s/X",
             "title": "某剧", "author": "a", "source_url": "u"}])
        out = pd.find_candidates(session, 1, ["甲"], settings=self._st())
        assert len(out) == 1 and out[0]["platform"] == "tieba"

    def test_one_source_failing_keeps_the_other(self, session, monkeypatch) -> None:
        """贴吧挂了(MediaCrawler 超时/未登录)**不能丢掉知乎已经拿到的那条**。"""
        from app.services import mediacrawler_source
        monkeypatch.setattr("app.services.cookie_store.get_cookie", lambda s, u, p: "ck")
        monkeypatch.setattr(pd, "_candidates_from_zhihu", lambda ck, kws, lim: [
            {"platform": "zhihu", "origin_url": "https://pan.quark.cn/s/Z",
             "title": "知乎的", "author": "", "source_url": ""}])
        def _boom(platform, kws, timeout=600):
            raise mediacrawler_source.MediaCrawlerError("贴吧超时")
        monkeypatch.setattr(mediacrawler_source, "crawl", _boom)
        out = pd.find_candidates(session, 1, ["甲"], settings=self._st())
        assert [c["platform"] for c in out] == ["zhihu"]

    def test_all_sources_failing_raises(self, session, monkeypatch) -> None:
        """**全都失败**要抛出来 —— 否则"两边都挂了"会被下游读成"今天真没资源"。"""
        from app.services import mediacrawler_source
        from app.services.cross_accounts import SearchSourceError
        monkeypatch.setattr("app.services.cookie_store.get_cookie", lambda s, u, p: "ck")
        monkeypatch.setattr(pd, "_candidates_from_zhihu",
                            lambda ck, kws, lim: (_ for _ in ()).throw(SearchSourceError("知乎限流")))
        def _boom(platform, kws, timeout=600):
            raise mediacrawler_source.MediaCrawlerError("贴吧超时")
        monkeypatch.setattr(mediacrawler_source, "crawl", _boom)
        with pytest.raises(SearchSourceError):
            pd.find_candidates(session, 1, ["甲"], settings=self._st())

    def test_source_can_be_switched_off(self, session, monkeypatch) -> None:
        monkeypatch.setattr("app.services.cookie_store.get_cookie", lambda s, u, p: "")
        called = []
        monkeypatch.setattr(pd, "_candidates_from_tieba", lambda kws: called.append(1) or [])
        assert pd.find_candidates(session, 1, ["甲"], settings=self._st(pan_discovery_tieba=False)) == []
        assert called == [], "关掉了还在跑贴吧源"


class TestDeadLinkIsTerminal:
    """死链判终态(`skipped`),其余留 `failed` 重试(2026-10-04)。

    背景:贴吧/知乎发现的影视盘链**大批已失效**(实测一轮 17 条里 15 条死链),
    而旧实现一律记 `failed` → 这些死链**每天被重试一遍、永远重试不完**,
    `failed` 越堆越多还看着像"链路故障"。
    """

    def test_explicit_dead_wording_is_terminal(self) -> None:
        for msg in ("夸克接口失败(41011): 分享地址已失效",
                    "分享页解析失败(链接失效或需提取码): uk=True files=0",
                    "转存失败(errno=-6 分享文件已被删除)",
                    "分享不存在", "get_share_user_banned"):
            assert pd._fail(msg)["status"] == "skipped", msg

    def test_rate_limit_stays_retryable(self) -> None:
        """限流是"等一会就好",不是终态 —— 判成 skipped 会把这条链永久钉死。"""
        for msg in ("转存被限制(errno=105 转存过于频繁),稍后重试",
                    "HTTP 403 请求过于频繁", "网络超时"):
            assert pd._fail(msg)["status"] == "failed", msg

    def test_ambiguous_errno_without_show_msg_stays_retryable(self) -> None:
        """⚠️ **判据从紧**:`errno=-6` 但**没有 show_msg** 时判不出是"失效"还是"限流" ——
        宁可多试一次,也不能误判终态(把能搬的链永久钉死这个反向的坑,2026-10-02 踩过)。"""
        assert pd._fail("转存失败(errno=-6)")["status"] == "failed"
        assert pd._is_dead_link("转存失败(errno=-6)") is False

    def test_transfer_maps_dead_error_to_skipped(self, session, monkeypatch) -> None:
        """接上调用方:客户端抛的异常串里带死链措辞 → 整条记 `skipped`(终态)。"""
        from app.services import baidupan_transfer

        class _C:
            def __init__(self, ck) -> None: ...
            def transfer_and_share(self, url, password=""):
                raise baidupan_transfer.BaiduPanError("转存失败(errno=-6 分享文件已被删除)")

        monkeypatch.setattr(baidupan_transfer, "BaiduPanClient", _C)
        monkeypatch.setattr("app.services.cookie_store.get_cookie", lambda s, u, p: "ck")
        out = pd.transfer_pan_url(session, 1, "https://pan.baidu.com/s/1abc")
        assert out["status"] == "skipped"


class TestAuthExpiryIsRetryableAndAlerted:
    """登录态失效 → 记 `pending`(可重试)**且推管理员群**(2026-10-04)。

    实测:一轮 15 条失败里 **10 条**是 `errno=-6 账户已过期,重新登陆` ——
    也就是说一批**本来能搬**的资源全卡在"没人去重粘百度 Cookie"上。
    它们既不是死链(不该判终态),也不该**一声不响**(公众号链早就有这个告警,这条链漏了)。
    """

    def _st(self):
        return type("S", (), {})()

    def test_baidu_auth_error_is_pending_and_alerts(self, session, monkeypatch) -> None:
        from app.services import baidupan_transfer

        class _C:
            def __init__(self, ck) -> None: ...
            def transfer_and_share(self, url, password=""):
                raise baidupan_transfer.BaiduPanAuthError("百度网盘登录态失效(账户已过期，重新登陆)")

        monkeypatch.setattr(baidupan_transfer, "BaiduPanClient", _C)
        monkeypatch.setattr("app.services.cookie_store.get_cookie", lambda s, u, p: "ck")
        alerts = []
        monkeypatch.setattr("app.services.alert_service.notify_incident",
                            lambda *a, **k: alerts.append(a[3]) or True)

        out = pd.transfer_pan_url(session, 1, "https://pan.baidu.com/s/1abc")
        assert out["status"] == "pending", "登录态失效是可重试的,判终态会把恢复后的重试也掐掉"
        assert alerts and "百度网盘" in alerts[0], f"没告警:{alerts}"

    def test_quark_auth_error_is_pending_too(self, session, monkeypatch) -> None:
        from app.services import quark_transfer

        class _Q:
            def __init__(self, ck, fid_store=None) -> None: ...
            def transfer_and_share(self, url, **k):
                raise quark_transfer.QuarkAuthError("夸克 Cookie 已失效")

        monkeypatch.setattr(quark_transfer, "QuarkTransfer", _Q)
        monkeypatch.setattr("app.services.cookie_store.get_cookie", lambda s, u, p: "ck")
        alerts = []
        monkeypatch.setattr("app.services.alert_service.notify_incident",
                            lambda *a, **k: alerts.append(a[3]) or True)

        out = pd.transfer_pan_url(session, 1, "https://pan.quark.cn/s/abc")
        assert out["status"] == "pending"
        assert alerts and "夸克" in alerts[0]

    def test_alert_failure_does_not_break_the_result(self, session, monkeypatch) -> None:
        """告警自己挂了,不能把转存结果也带崩(它本来只是"顺带通知")。"""
        from app.services import baidupan_transfer

        class _C:
            def __init__(self, ck) -> None: ...
            def transfer_and_share(self, url, password=""):
                raise baidupan_transfer.BaiduPanAuthError("登录态失效")

        monkeypatch.setattr(baidupan_transfer, "BaiduPanClient", _C)
        monkeypatch.setattr("app.services.cookie_store.get_cookie", lambda s, u, p: "ck")

        def _boom(*a, **k):
            raise RuntimeError("飞书挂了")

        monkeypatch.setattr("app.services.alert_service.notify_incident", _boom)
        assert pd.transfer_pan_url(session, 1, "https://pan.baidu.com/s/1abc")["status"] == "pending"


def test_kind_of_maps_hosts() -> None:
    assert pd._kind_of("https://pan.quark.cn/s/abc") == "quark"
    assert pd._kind_of("https://pan.baidu.com/s/1abc") == "baidu"
    assert pd._kind_of("https://pan.xunlei.com/s/abc") == "xunlei"
    assert pd._kind_of("https://example.com/x") == ""


def test_transfer_unknown_host_is_terminal_but_missing_cookie_is_retryable(session, monkeypatch) -> None:
    """两种"没搬成"要分开:不认识的链是**终态**;缺 Cookie 是**可重试**
    (配上就能搬,标终态会把这条资源永久钉死)。"""
    out = pd.transfer_pan_url(session, 1, "https://example.com/x")
    assert out["status"] == "skipped" and "不认识" in out["message"]

    monkeypatch.setattr(cookie_store, "get_cookie", lambda s, u, p: "")
    out = pd.transfer_pan_url(session, 1, "https://pan.quark.cn/s/abc")
    assert out["status"] == "pending" and "夸克" in out["message"]


def test_transfer_dispatches_quark_and_reads_password_key(session, monkeypatch) -> None:
    """夸克链走 `QuarkTransfer`;⚠️ 它返回的提取码键是 **`password`** 不是 `code`。"""
    from app.services import quark_transfer as qt

    seen = {}

    class _Q:
        def __init__(self, ck, **kw):
            seen["ck"] = ck

        def transfer_and_share(self, url, **kw):
            seen["url"] = url
            return {"share_url": "https://pan.quark.cn/s/OUR", "password": "1a2b"}

    monkeypatch.setattr(qt, "QuarkTransfer", _Q)
    monkeypatch.setattr(cookie_store, "get_cookie",
                        lambda s, u, p: "QUARK-CK" if p == "quark" else "")
    out = pd.transfer_pan_url(session, 1, "https://pan.quark.cn/s/xyz", _S())
    assert out["status"] == "ok" and out["our_url"].endswith("/OUR") and out["code"] == "1a2b"
    assert seen["ck"] == "QUARK-CK"


def test_transfer_baidu_extracts_pwd_from_snippet(session, monkeypatch) -> None:
    """百度链转存要**提取码**,而码常写在回答正文里 → 从 snippet 里抠出来一起传进去。"""
    from app.services import baidupan_transfer as bt

    seen = {}

    class _B:
        def __init__(self, ck):
            pass

        def transfer_and_share(self, url, password=""):
            seen["pwd"] = password
            return {"share_url": "https://pan.baidu.com/s/OUR", "password": "abcd"}

    monkeypatch.setattr(bt, "BaiduPanClient", _B)
    monkeypatch.setattr(cookie_store, "get_cookie",
                        lambda s, u, p: "BD-CK" if p == "baidupan" else "")
    out = pd.transfer_pan_url(session, 1, "https://pan.baidu.com/s/1xyz", _S(),
                              snippet="某资源 提取码:abcd 速存")
    assert seen["pwd"] == "abcd" and out["code"] == "abcd"


def test_find_candidates_needs_zhihu_cookie(session, monkeypatch) -> None:
    """这条路靠知乎搜索,**没配 Cookie 就没有候选**(不报错)。"""
    monkeypatch.setattr(cookie_store, "get_cookie", lambda s, u, p: "")
    assert pd.find_candidates(session, 1, ["词"], settings=_S()) == []


def test_find_candidates_dedupes_and_strips_tags(session, monkeypatch) -> None:
    """同一链被多条回答贴出只算一条;snippet 里的 `<em>` 高亮要清掉。"""
    from app.services import cross_accounts as ca

    monkeypatch.setattr(cookie_store, "get_cookie", lambda s, u, p: "ZH-CK")
    monkeypatch.setattr(pd, "_REQ_GAP", 0)
    calls = {"n": 0}

    def fake_search(ck, kw, limit=20):
        calls["n"] += 1
        return [{"uid": "a", "name": "作者甲", "url": "https://zhihu.com/a",
                 "snippet": "最近爆火的<em>花少2人格测试</em>", "pan_link": "https://pan.quark.cn/s/X"},
                {"uid": "b", "name": "作者乙", "url": "https://zhihu.com/b",
                 "snippet": "同样资源", "pan_link": "https://pan.quark.cn/s/X"},   # 重复链
                {"uid": "c", "name": "作者丙", "url": "https://zhihu.com/c",
                 "snippet": "另一个", "pan_link": ""}]                              # 无链

    monkeypatch.setattr(ca, "_search_zhihu", fake_search)
    out = pd.find_candidates(session, 1, ["甲", "乙"], settings=_S())
    assert calls["n"] == 2                                  # 两个词各搜一次
    assert len(out) == 1 and out[0]["origin_url"] == "https://pan.quark.cn/s/X"
    assert out[0]["author"] == "作者甲" and "<em>" not in out[0]["title"]
    assert out[0]["title"] == "最近爆火的花少2人格测试"


def test_sync_transfers_within_budget_and_persists(session, monkeypatch) -> None:
    """端到端:候选 → 转存(受**每轮额度**限制)→ 入库;超额度的那条留 pending。"""
    monkeypatch.setattr("app.services.douyin_leads.search_keywords",
                        lambda s, u, t, st: ["甲"])
    monkeypatch.setattr(pd, "find_candidates", lambda *a, **k: [
        {"platform": "zhihu", "origin_url": f"https://pan.quark.cn/s/{i}",
         "title": f"资源{i}", "author": "作者", "source_url": "u"} for i in range(3)])
    monkeypatch.setattr(pd, "transfer_pan_url",
                        lambda s, u, url, st=None, snip="": {
                            "status": "ok", "our_url": f"OUR-{url[-1]}",
                            "code": "1", "message": ""})

    out = pd.sync(session, 1, settings=_S())            # 新发现额度 = 2
    assert out["status"] == "ok" and out["ok"] == 2
    rows = session.scalars(select(DiscoveredPanLink).order_by(DiscoveredPanLink.id)).all()
    assert len(rows) == 3
    assert [r.status for r in rows] == ["ok", "ok", "pending"]     # 第三条超额度
    # ⚠️ 消息里**要写清是哪份额度**用完(积压 / 新发现)—— 否则看日志分不出
    # "积压没清完"和"新发现被挡",而这两件事的处置完全不同。
    assert rows[0].our_url == "OUR-0" and rows[2].message == "本轮新发现额度用完"
    assert out["backlog_left"] == 1, "本轮结束时还剩 1 条在排队,必须报出来"


def test_积压与新发现各吃各的额度_互不饿死(session, monkeypatch) -> None:
    """★ **这条测试防的就是本功能要解决的那个问题本身**。

    2026-10-05 实测:一轮 **候选 44 条、只转存 3 条**,库里堆了 41 条 healthy pending,
    理由清一色「额度用完」。而积压在 `cands` 里**排在最前**,单额度下它会把额度吃满
    ⇒ **本轮真正新搜到的资源一条都轮不上**,可新资源的时效性恰恰最强(热点过了就没意义)。

    所以额度拆成两份,这里钉住"两份额度都真的被用上" —— 否则改回单额度、或把
    两份算成同一份,这条会立刻红。
    """
    class _S2(_S):
        pan_discovery_transfer_limit = 1          # 积压额度 1
        pan_discovery_fresh_limit = 1             # 新发现额度 1

    # 库里先有 2 条**积压**(等得最久)
    session.add_all([
        DiscoveredPanLink(user_id=1, platform="zhihu", title="旧1", status="pending",
                          origin_url="https://pan.quark.cn/s/OLD1"),
        DiscoveredPanLink(user_id=1, platform="zhihu", title="旧2", status="pending",
                          origin_url="https://pan.quark.cn/s/OLD2"),
    ])
    session.commit()

    monkeypatch.setattr("app.services.douyin_leads.search_keywords",
                        lambda s, u, t, st: ["甲"])
    # ⚠️ 关掉"三盘互通":否则命中的候选会被判"库里已有"而不走转存,数不出额度
    monkeypatch.setattr(pd, "already_have", lambda s, u, title: None)
    monkeypatch.setattr(pd, "find_candidates", lambda *a, **k: [
        {"platform": "zhihu", "origin_url": "https://pan.quark.cn/s/NEW1",
         "title": "新1", "author": "a", "source_url": "u"},
        {"platform": "zhihu", "origin_url": "https://pan.quark.cn/s/NEW2",
         "title": "新2", "author": "a", "source_url": "u"}])
    calls: list[str] = []
    monkeypatch.setattr(pd, "transfer_pan_url",
                        lambda s, u, url, st=None, snip="": (
                            calls.append(url) or
                            {"status": "ok", "our_url": "OUR", "code": "", "message": ""}))

    out = pd.sync(session, 1, settings=_S2())

    assert len(calls) == 2, f"积压与新发现各留 1 份额度,应转 2 条,实际 {calls}"
    assert any("OLD" in u for u in calls), f"积压那条没被搬:{calls}"
    assert any("NEW" in u for u in calls), f"**新发现被积压饿死了** —— 只搬了 {calls}"
    assert out["ok"] == 2 and out["backlog_size"] == 2, "积压 2 条、共成功 2 条"
    # ⚠️ `backlog_left` 是**队列深度**(所有 pending/failed),**不只是原来那批积压** ——
    # 本轮没轮上的新发现也仍在队里,所以这里是 2(OLD2 + NEW2),不是 1。
    # 用"队列深度"而不是"原积压剩几条"是有意的:告警要问的是"**还有多少没搬完**"。
    assert out["backlog_left"] == 2, "OLD2 + NEW2 仍排队 ⇒ 队列深度 2"


def test_sync_ignores_already_known_urls(session, monkeypatch) -> None:
    """已经发现过的链**不再重复找/转存**(按 `origin_url` 去重)。"""
    session.add(DiscoveredPanLink(user_id=1, platform="zhihu",
                                  origin_url="https://pan.quark.cn/s/OLD", title="旧的",
                                  status="ok"))
    session.commit()
    monkeypatch.setattr("app.services.douyin_leads.search_keywords", lambda s, u, t, st: ["甲"])
    monkeypatch.setattr(pd, "find_candidates", lambda *a, **k: [
        {"platform": "zhihu", "origin_url": "https://pan.quark.cn/s/OLD",
         "title": "旧的", "author": "", "source_url": ""}])
    called = []
    monkeypatch.setattr(pd, "transfer_pan_url",
                        lambda *a, **k: called.append(1) or {"status": "ok", "our_url": "x",
                                                             "code": "", "message": ""})
    out = pd.sync(session, 1, settings=_S())
    assert out["found"] == 0 and called == []


def test_push_items_routes_to_zhihu_group(monkeypatch) -> None:
    """推**知乎专属群**;版式与其他推送一致(四列网格),署名用我们的品牌词。"""
    from app.services import feishu_client

    sent = {}

    class _C:
        def __init__(self, webhook, secret):
            sent["webhook"] = webhook

        def send_card(self, card):
            sent["card"] = card
            return True

    monkeypatch.setattr(feishu_client, "FeishuClient", _C)
    ok = pd.push_items([{"title": "花少2人格测试", "author": "作者甲",
                         "source_url": "u", "share_url": "https://pan.quark.cn/s/OUR",
                         "code": "1a2b"}], _S())
    assert ok is True and sent["webhook"] == "https://example.com/zhihu"
    assert "https://pan.quark.cn/s/OUR" in str(sent["card"])
    assert "念飞思雪" in str(sent["card"]["header"])


def test_sync_retries_pending_but_not_ok_or_skipped(session, monkeypatch) -> None:
    """⚠️ 去重只认**已成功/终态**的:`pending`(缺 Cookie、超额度)**要能重来** ——

    把 pending 也算作"已知"的话,那条链会被永久钉死(实测踩过:缺 Cookie 跳过的那条,
    明明配上 Cookie 就能搬,却因为"发现过了"再也不动)。
    """
    session.add_all([
        DiscoveredPanLink(user_id=1, platform="zhihu", origin_url="https://pan.quark.cn/s/PEND",
                          title="待办", status="pending"),
        DiscoveredPanLink(user_id=1, platform="zhihu", origin_url="https://pan.quark.cn/s/DONE",
                          title="已搬", status="ok"),
        DiscoveredPanLink(user_id=1, platform="zhihu", origin_url="https://pan.quark.cn/s/TERM",
                          title="终态", status="skipped")])
    session.commit()
    monkeypatch.setattr("app.services.douyin_leads.search_keywords", lambda s, u, t, st: ["甲"])
    monkeypatch.setattr(pd, "find_candidates", lambda *a, **k: [
        {"platform": "zhihu", "origin_url": u, "title": t, "author": "", "source_url": ""}
        for u, t in (("https://pan.quark.cn/s/PEND", "待办"),
                     ("https://pan.quark.cn/s/DONE", "已搬"),
                     ("https://pan.quark.cn/s/TERM", "终态"))])
    monkeypatch.setattr(pd, "transfer_pan_url",
                        lambda *a, **k: {"status": "ok", "our_url": "OUR", "code": "", "message": ""})
    out = pd.sync(session, 1, settings=_S())
    assert out["found"] == 1, "只有 pending 那条该被重新处理"
    assert session.scalars(select(DiscoveredPanLink).where(
        DiscoveredPanLink.origin_url.like("%PEND"))).one().status == "ok"


def test_clean_strips_tags_and_structured_leftovers() -> None:
    """知乎 snippet 实测长这样:`最近爆火的花少2人格测试 [{'content': '这个太好玩了…` ——
    要清掉 `<em>` 高亮,并在**结构化残留**(`[{`/`{'`)处截断,否则标题会拖一大坨噪音。"""
    assert pd._clean("最近爆火的<em>花少2人格测试</em>") == "最近爆火的花少2人格测试"
    assert pd._clean("最近爆火的花少2人格测试 [{'content': '这个太好玩了") == "最近爆火的花少2人格测试"
    assert pd._clean("") == ""


def test_find_candidates_raises_when_all_keywords_fail(session, monkeypatch) -> None:
    """**全部词都硬失败** → 抛错,让 sync 记 `failed` 而不是 `success(候选0)`。

    这是"假成功"的防线:被限流时链路必须看起来是坏的,而不是安静地什么都不做。
    """
    from app.services import cross_accounts as ca

    def boom(ck, kw, limit=20):
        raise ca.SearchSourceError("HTTP 403(登录态失效或被限流)")

    monkeypatch.setattr(ca, "_search_zhihu", boom)
    monkeypatch.setattr("app.services.cookie_store.get_cookie", lambda s, u, p: "ck")
    with pytest.raises(ca.SearchSourceError) as ei:
        pd.find_candidates(session, 1, ["甲", "乙"], settings=_S())
    assert "全部搜索失败" in str(ei.value)


def test_find_candidates_keeps_going_when_one_keyword_fails(session, monkeypatch) -> None:
    """**部分**词失败要保住其余词的产出(限速是常态,不能一失败就整轮白跑)。"""
    from app.services import cross_accounts as ca

    def flaky(ck, kw, limit=20):
        if kw == "甲":
            raise ca.SearchSourceError("超时")
        return [{"uid": "a", "name": "作者", "url": "",
                 "snippet": "花少2人格测试", "pan_link": "https://pan.quark.cn/s/Y"}]

    monkeypatch.setattr(ca, "_search_zhihu", flaky)
    monkeypatch.setattr("app.services.cookie_store.get_cookie", lambda s, u, p: "ck")
    out = pd.find_candidates(session, 1, ["甲", "乙"], settings=_S())
    assert len(out) == 1 and out[0]["origin_url"] == "https://pan.quark.cn/s/Y"


def test_sync_retries_backlog_without_refinding_it(session, monkeypatch) -> None:
    """⚠️ **存量待办必须优先重试,不能只等"再次被搜到"**(2026-10-04 加)。

    用户遇到的场景:抖音那轮 5 条卡在**盘满**,而盘一清出来 ——
    旧实现**没有任何机制会去重搬它们**(`sync` 的候选只来自"本轮新搜到的",
    pending 行只有在被重新搜到时才会更新,纯属碰运气)。

    现在 `sync` 会把库里 `pending`/`failed` 的行**排在新发现的前面**先处理。
    这条测试:**本轮一个新候选都搜不到**,但存量待办仍然被搬了。
    """
    session.add(DiscoveredPanLink(user_id=1, platform="douyin",
                                  origin_url="https://pan.quark.cn/s/BACKLOG",
                                  title="攒着的", status="pending"))
    session.commit()
    monkeypatch.setattr(pd, "_search_words", lambda *a, **k: ["某词"])
    monkeypatch.setattr(pd, "find_candidates", lambda *a, **k: [])       # 本轮没搜到新的
    monkeypatch.setattr(pd, "transfer_pan_url",
                        lambda *a, **k: {"status": "ok", "our_url": "OUR-BACKLOG",
                                         "code": "", "message": ""})

    out = pd.sync(session, 1, settings=_S())
    assert out["ok"] == 1, f"存量待办没被重试:{out}"
    row = session.scalar(select(DiscoveredPanLink).where(
        DiscoveredPanLink.origin_url == "https://pan.quark.cn/s/BACKLOG"))
    assert row.status == "ok" and row.our_url == "OUR-BACKLOG"


# ------------------------- 三个网盘互通:别的盘已有就别再搬(2026-10-05)
def test_already_have_matches_by_name_not_by_link(session) -> None:
    """★★ **三盘互通**(用户口径:「如果同一资源单个网盘已经有了,就不需要多个网盘进行转存了,
    只需要从已有的里面推这个资源就行」)。

    ⚠️ **必须按名字匹配,不能按链接** —— 同一份资源在夸克和百度上是**不同的 share_id**,
    按链接去重挡不住"同一资源被搬到第二个盘"。这里造一份**库里已有的公众号资源**,
    再去问一个新发现的候选(名字相同、链接完全不同)—— 它必须认得出来。
    """
    from app.db.models import WechatArticle, WechatPanLink

    art = WechatArticle(user_id=1, author="某号", title="【齐民要术】完整版资源合集",
                        url="https://mp.weixin.qq.com/s/x", my_pan_urls="https://pan.quark.cn/s/OUR-OWN-LINK-abc123")
    session.add(art)
    session.flush()
    session.add(WechatPanLink(user_id=1, article_id=art.id,
                              pan_url="https://pan.quark.cn/s/ALREADY-HAVE"))
    session.commit()

    # 候选的链接是**另一个盘**的,但名字对得上
    hit = pd.already_have(session, 1, "【齐民要术】完整版资源合集 速存 https://pan.baidu.com/s/NEW")
    assert hit is not None, "名字相同、不同盘 —— 必须认出来,否则会白搬一份"
    assert hit["my_link"] == "https://pan.quark.cn/s/OUR-OWN-LINK-abc123"


def test_already_have_returns_none_when_unknown(session) -> None:
    """⚠️ **宁可漏判不要误判**:匹配不上就返回 None(照常转存)。
    误判的代价是"把 A 的链当成 B 推出去"(**内容事故**);
    漏判只是"多搬一份"(**空间浪费**)—— 两者不是一个量级。"""
    assert pd.already_have(session, 1, "一个谁也没提过的资源") is None


def test_already_have_requires_our_link(session) -> None:
    """库里**只有原始链、没有我方链**时**不算**"已有" —— 那还是得搬一次。"""
    from app.db.models import WechatArticle, WechatPanLink

    art = WechatArticle(user_id=1, author="某号", title="【独一份】资源包",
                        url="https://mp.weixin.qq.com/s/y")      # my_pan_urls 为空
    session.add(art)
    session.flush()
    session.add(WechatPanLink(user_id=1, article_id=art.id,
                              pan_url="https://pan.baidu.com/s/NO-OUR-LINK"))
    session.commit()
    assert pd.already_have(session, 1, "【独一份】资源包") is None, "没我方链就得自己搬"


def test_resource_name_hints_prefers_bracket_entities() -> None:
    """取词按精度排:《》实体优先,其次连续中文片段;链接和话题标签要剔掉(它们是噪音)。"""
    hints = pd._resource_name_hints("《齐民要术》完整版 #资源分享# https://pan.quark.cn/s/abc")
    assert hints[0] == "齐民要术", f"《》实体该排第一:{hints}"
    assert all("pan.quark" not in h for h in hints), "链接不该进候选词"
    assert all("资源分享" != h for h in hints), "话题标签不该进候选词"


# ------------------------- 集成:三盘互通必须真的发生在**转存循环里**(2026-10-05)
def test_sync_reuses_existing_link_instead_of_transferring(session, monkeypatch) -> None:
    """★★ **集成测试**(反向验证逼出来的):候选的资源**已经在别的盘**时,
    `sync` 的转存循环**不该再调 `transfer_pan_url`**,而是直接复用已有那条链。

    ⚠️ 为什么要单写这条:`already_have` 自己的单测全绿,**但"循环里有没有真的用它"没被钉住** ——
    我做完反向验证(把那一行换成 `have = None`)时,测试**竟然还是全绿**,说明那一层是裸的。
    """
    from app.db.models import WechatArticle, WechatPanLink

    # ① 库里先有一条**已有我方链**的资源(在夸克)
    art = WechatArticle(user_id=1, author="某号", title="【齐民要术】完整版资源合集",
                        url="https://mp.weixin.qq.com/s/x",
                        my_pan_urls="https://pan.quark.cn/s/OUR-OWN-LINK-abc123")
    session.add(art)
    session.flush()
    session.add(WechatPanLink(user_id=1, article_id=art.id,
                              pan_url="https://pan.quark.cn/s/ALREADY-HAVE"))
    session.commit()

    # ② 发现链给出一个**同名、但链接在另一个盘**的候选
    monkeypatch.setattr(pd, "find_candidates", lambda *a, **k: [
        {"platform": "weibo", "origin_url": "https://pan.baidu.com/s/NEW-ONE",
         "title": "【齐民要术】完整版资源合集 速存", "author": "资源铺",
         "source_url": "https://weibo.com/1/x"}])
    # ③ 转存被调用就要记下来(它不该被调)
    calls: list[str] = []
    monkeypatch.setattr(pd, "transfer_pan_url",
                        lambda *a, **k: calls.append(a[2]) or {
                            "status": "ok", "our_url": "SHOULD-NOT-HAPPEN",
                            "code": "", "message": ""})

    out = pd.sync(session, 1, settings=_S())
    assert calls == [], f"库里已有却还去转存了:{calls}"
    assert out["reused"] == 1, out
    # ⚠️ **仍然要推出去** —— 只是用**已有那条链**,这正是用户要的"从已有的里面推这个资源"
    assert out["items"] and out["items"][0]["share_url"] \
        == "https://pan.quark.cn/s/OUR-OWN-LINK-abc123"


class TestPublishCutoff:
    """★ **公开平台发现也要按发布时间过滤**(2026-10-06)。

    知乎/微博的原始响应里**一直有时间**(实测 `object.created_time` /
    `mblog.created_at`),只是解析时没读 ⇒ 下游只能拿"我们发现的时刻"当新鲜度,
    那是**假的新鲜度**(老帖被反复搜到时照样"刚发现")。

    ⚠️ **拿不到时间的也跳过** —— 但那种情况**必须报数**(整条链静默归零是本仓最忌讳的)。
    """

    def _cands(self, monkeypatch, rows):
        from app.services import pan_discovery as pd

        monkeypatch.setattr(pd, "_candidates_from_zhihu",
                            lambda ck, kws, limit: list(rows))
        monkeypatch.setattr(pd, "_candidates_from_weibo", lambda ck, kws, limit: [])
        monkeypatch.setattr(pd, "_candidates_from_tieba", lambda kws, limit: [])
        monkeypatch.setattr("app.services.cookie_store.get_cookie", lambda s, u, p: "CK")
        monkeypatch.setattr(pd, "_search_words", lambda *a, **k: ["网盘"])
        return pd

    def _row(self, url, pub):
        return {"platform": "zhihu", "origin_url": url, "title": "某资源", "author": "甲",
                "source_url": "u", "publish_at": pub}

    def test_早于下限的被丢掉(self, monkeypatch, session) -> None:
        import datetime as dt
        pd = self._cands(monkeypatch, [
            self._row("https://pan.quark.cn/s/NEW", int(dt.datetime(2026, 10, 5).timestamp())),
            self._row("https://pan.quark.cn/s/OLD", int(dt.datetime(2026, 8, 1).timestamp())),
        ])

        class _S2(_S):
            content_min_publish_date = "2026-10-01"
            douyin_leads_min_publish_date = ""
        out = pd.find_candidates(session, 1, ["网盘"], settings=_S2())
        assert [c["origin_url"] for c in out] == ["https://pan.quark.cn/s/NEW"], out

    def test_没有时间的也丢掉(self, monkeypatch, session) -> None:
        pd = self._cands(monkeypatch, [self._row("https://pan.quark.cn/s/NO", 0)])

        class _S2(_S):
            content_min_publish_date = "2026-10-01"
            douyin_leads_min_publish_date = ""
        assert pd.find_candidates(session, 1, ["网盘"], settings=_S2()) == []
