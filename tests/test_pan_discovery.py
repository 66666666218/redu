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
    pan_discovery_transfer_limit = 2
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

    out = pd.sync(session, 1, settings=_S())            # 额度 = 2
    assert out["status"] == "ok" and out["ok"] == 2
    rows = session.scalars(select(DiscoveredPanLink).order_by(DiscoveredPanLink.id)).all()
    assert len(rows) == 3
    assert [r.status for r in rows] == ["ok", "ok", "pending"]     # 第三条超额度
    assert rows[0].our_url == "OUR-0" and rows[2].message == "本轮转存额度用完"


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
