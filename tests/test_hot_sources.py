"""热榜源契约单测(v2.2.0 命门自持架构):解析/契约/错误路径。"""
import pytest


@pytest.fixture
def session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db.database import Base
    from app.db import models  # noqa: F401 - 注册全部表

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    yield db
    db.close()


def test_bilibili_douban_direct_parse(monkeypatch) -> None:
    from app.services import hot_sources as hs

    class _R:
        def __init__(self, payload):
            self._p = payload
        def json(self):
            return self._p

    # B站:官方排行 JSON 结构
    monkeypatch.setattr(hs.creq, "get", lambda *a, **kw: _R({"data": {"list": [
        {"title": "《原神》主题曲", "bvid": "BV1x", "tname": "手机游戏",
         "stat": {"view": 12345}},
        {"title": "", "bvid": "BV2"},  # 无标题丢弃
    ]}}))
    items = hs.BilibiliSource().fetch(limit=10)
    assert len(items) == 1 and items[0]["title"] == "《原神》主题曲"
    assert "bilibili.com/video/BV1x" in items[0]["url"]

    # 豆瓣:公开 JSON 结构
    monkeypatch.setattr(hs.creq, "get", lambda *a, **kw: _R({"subjects": [
        {"title": "年会不能停！2", "url": "https://movie.douban.com/subject/1/", "rate": "6.4"}]}))
    items = hs.DoubanSource().fetch()
    assert items[0]["title"] == "年会不能停！2" and "6.4" in items[0]["extra"]


def test_newsnow_source_contract(monkeypatch) -> None:
    from app.services import hot_sources as hs

    class _R:
        def __init__(self, payload):
            self._p = payload
        def json(self):
            return self._p

    monkeypatch.setattr(hs.creq, "get", lambda *a, **kw: _R({
        "status": "success", "id": "zhihu",
        "items": [{"id": "1", "title": "知乎热榜条目", "url": "https://zhihu.com/q/1",
                   "extra": {"info": "1310 万热度"}}]}))
    items = hs.NewsnowSource("zhihu").fetch()
    assert items[0]["title"] == "知乎热榜条目" and "1310 万热度" in items[0]["extra"]

    # 容器返回异常 → HotSourceError(调用方可降级)
    monkeypatch.setattr(hs.creq, "get", lambda *a, **kw: _R({"error": True, "statusCode": 500}))
    with pytest.raises(hs.HotSourceError):
        hs.NewsnowSource("zhihu").fetch()


def test_fetch_hot_registry_and_unknown() -> None:
    from app.services import hot_sources as hs

    # 自研源注册在位(命门自持的核心两个)
    assert "bilibili" in hs.SOURCES and "douban" in hs.SOURCES
    assert isinstance(hs.SOURCES["bilibili"], hs.BilibiliSource)
    with pytest.raises(hs.HotSourceError):
        hs.fetch_hot("not_exist")


def test_platform_hot_candidates_aggregates_by_platform(session) -> None:
    """多平台候选(全进 Agent 选题):跨平台同现聚合、平台数与名次排序。"""
    from datetime import datetime, timedelta

    from app.db.models import HotSourceItem
    from app.services.hotspot_agent import _platform_hot_candidates

    now = datetime.now()
    rows = [
        # 跨 2 平台同现(归一化后相同)
        HotSourceItem(user_id=1, source="bilibili", rank=1, title="迪拜航空客机事故", captured_at=now),
        HotSourceItem(user_id=1, source="zhihu", rank=2, title="迪拜航空客机事故", captured_at=now),
        # 单平台
        HotSourceItem(user_id=1, source="douban", rank=1, title="年会不能停2", captured_at=now),
        # 名次超界(>10)不进候选
        HotSourceItem(user_id=1, source="hupu", rank=11, title="低名次条目", captured_at=now),
        # 过期(24h 外)
        HotSourceItem(user_id=1, source="weibo", rank=1, title="过期条目",
                      captured_at=now - timedelta(hours=30)),
    ]
    session.add_all(rows)
    session.commit()
    cands = _platform_hot_candidates(session, 1)
    titles = [c["keyword"] for c in cands]
    assert "迪拜航空客机事故" in titles and "年会不能停2" in titles
    assert "低名次条目" not in titles and "过期条目" not in titles
    # 跨平台同现排在单平台之前(平台数降序)
    assert titles[0] == "迪拜航空客机事故"
    assert cands[0]["platforms"] in ("bilibili+zhihu", "zhihu+bilibili")


# ------------------------------- 2026-10-04:从 newsnow 搬过来的 4 个自研源
def test_dig_titled_finds_the_longest_titled_list_and_rejects_tiny_ones() -> None:
    """★ **别按记忆写路径**:上游随手加一层 `data`/`result` 就全空,
    而空会被读成"今天没热点"(本仓的老毛病)。这里用"找最长的带标题列表"的启发式。

    ⚠️ 但要**要求至少 5 条** —— 太少说明找错层了,宁可返回空(让调用方报错),
    也不要把一个 2 条的边角列表当成整张榜。
    """
    from app.services.hot_sources import _dig_titled

    payload = {"result": {"data": {"list": [{"title": f"t{i}"} for i in range(50)]}},
               "small": [{"title": "x"}, {"title": "y"}]}
    assert len(_dig_titled(payload, ("title",))) == 50
    # 只有小列表 → 不当真
    assert _dig_titled({"a": [{"title": "x"}, {"title": "y"}]}, ("title",)) == []
    # 键名不匹配 → 空
    assert _dig_titled({"list": [{"nope": 1} for _ in range(20)]}, ("title",)) == []


def test_tencent_skips_the_ranking_placeholder(monkeypatch) -> None:
    """★ 返回体**第 0 条是榜单说明**(`id=TIP…`、**没有 surl**),不是热点条目。

    判据用"**有没有可点的链接**"而不是硬编那句标题 —— 标题会变,结构不会。
    """
    from app.services import hot_sources as hs

    class _R:
        status_code = 200

        def json(self):
            return {"idlist": [
                {"id": "TIP2022", "title": "腾讯新闻用户最关注的热点，每10分钟更新一次",
                 "articletype": 560},                       # 占位符:没有 url
                *[{"id": f"a{i}", "title": f"真热点{i}",
                   "surl": f"https://view.inews.qq.com/a/{i}"} for i in range(8)],
            ]}

    monkeypatch.setattr(hs.creq, "get", lambda *a, **k: _R())
    rows = hs.TencentNewsSource().fetch(limit=30)
    assert len(rows) == 8, f"占位符该被滤掉,实际 {len(rows)} 条"
    assert all("每10分钟更新一次" not in r["title"] for r in rows)
    assert all(r["url"] for r in rows)
    assert [r["rank"] for r in rows] == list(range(1, 9))     # 名次连续(滤掉后重排)


def test_zhihu_falls_back_to_newsnow_without_a_cookie(monkeypatch) -> None:
    """★★ **没有 Cookie 时回落 newsnow,而不是报错**(2026-10-04 远程实测踩到的回归)。

    经过:我给知乎写了自研版、并**把它从 newsnow 名单摘掉** —— 但**知乎 Cookie 只在本机**
    (家宽那条盘链搜索链在用),于是**远程实例的知乎热榜从"能用"变成"不能用"**。
    **两实例的库独立,凭据不跟着代码走。**
    ⇒ 正确姿势:有 Cookie 用自研(命门自持),没有就回落容器那份。
    """
    from app.services import hot_sources as hs

    monkeypatch.setattr(hs.ZhihuHotSource, "_cookie", lambda self: "")
    monkeypatch.setattr(hs.NewsnowSource, "fetch",
                        lambda self, limit=30: [{"rank": 1, "title": "来自容器", "url": "", "extra": ""}])
    rows = hs.ZhihuHotSource().fetch()
    assert rows and rows[0]["title"] == "来自容器"


def test_zhihu_raises_only_when_both_paths_fail(monkeypatch) -> None:
    """★★ **两条路都不通才抛错** —— 那时才是"真的拿不到";
    而任何情况下**都不许返回空列表冒充"今天没热点"**(本仓反复踩的「静默失败=假成功」)。"""
    from app.services import hot_sources as hs

    monkeypatch.setattr(hs.ZhihuHotSource, "_cookie", lambda self: "")

    def _boom(self, limit=30):
        raise hs.HotSourceError("容器连不上")

    monkeypatch.setattr(hs.NewsnowSource, "fetch", _boom)
    try:
        hs.ZhihuHotSource().fetch()
    except hs.HotSourceError as exc:
        assert "两条路都不通" in str(exc)
    else:
        raise AssertionError("两条路都不通时必须抛错,不许静默返回空")


def test_moved_sources_are_self_built_not_newsnow() -> None:
    """★ 搬过来的这几个**别再注册回 `NewsnowSource`** —— 同一个源两条链会重复入库,
    而且搬的意义就在于"不再依赖容器"。"""
    from app.services.hot_sources import SOURCES

    for sid in ("zhihu", "toutiao", "tencent-hot", "bilibili-hotsearch"):
        assert sid in SOURCES, f"{sid} 没注册"
        assert type(SOURCES[sid]).__name__ != "NewsnowSource", f"{sid} 又走回 newsnow 了"
    # 自研源数量只增不减(2026-10-04:2 → 6)
    own = [k for k, v in SOURCES.items() if type(v).__name__ != "NewsnowSource"]
    assert len(own) >= 6, own


def test_bilibili_business_code_is_treated_as_an_error(monkeypatch) -> None:
    """★★ **HTTP 200 不等于成功** —— B站被风控时返回的是 **200 + `code:-352` + 空 list**。

    ⚠️ 只 catch 异常的话,这会**悄悄变成"今天榜单是空的"**,还被记成 `ok`
    —— 远程实测就这么瞒了不知道多久(`ok=42 failed=1 items=959`,那 0 条的就是它)。
    这正是本仓反复踩的「静默失败=假成功」。
    """
    from app.services import hot_sources as hs

    class _R:
        status_code = 200

        def __init__(self, payload):
            self._p = payload

        def json(self):
            return self._p

    # 风控:-352
    monkeypatch.setattr(hs.creq, "get",
                        lambda *a, **k: _R({"code": -352, "message": "risk control",
                                            "data": {"list": []}}))
    for src in (hs.BilibiliSource(), hs.BilibiliHotSearchSource()):
        try:
            rows = src.fetch()
        except hs.HotSourceError as exc:
            assert "-352" in str(exc), str(exc)
        else:
            raise AssertionError(f"{type(src).__name__} 把风控当成了空榜单: {rows}")

    # 正常 code=0 照常返回
    monkeypatch.setattr(hs.creq, "get",
                        lambda *a, **k: _R({"code": 0, "data": {"list": [
                            {"title": f"t{i}", "bvid": f"B{i}", "tname": "动画",
                             "stat": {"view": 1}} for i in range(8)]}}))
    assert len(hs.BilibiliSource().fetch(limit=30)) == 8


class TestSinglePlatformNotStarved:
    """★ **2026-10-05 实测出来的结构问题**:候选排序把"跨平台同现"放最前,于是当
    **跨平台条数 ≥ cap** 时,`out[:cap]` **全是跨平台条目** —— 平台权重(`SOURCE_FIT`)
    那段排序**等于白做**,**所有单平台源一条都进不了 LLM**。

    远程实测 24h:去重 **2027** 条候选里 **跨平台只有 81 条** ⇒ 81 > 60,
    **1946 条单平台条目全被埋在 LLM 之前**(B站对标号 `source="bili-pan"` 那条链
    正是这样一条都进不去,权重 0.60 全场最高也白搭)。
    """

    def _mk(self, session, n_multi: int, singles: list[tuple[str, str]]) -> None:
        from datetime import datetime

        from app.db.models import HotSourceItem

        now = datetime.now()
        rows = []
        for i in range(n_multi):                      # 每条挂 2 个源 ⇒ 真·跨平台
            for src in ("weibo", "baidu"):
                rows.append(HotSourceItem(user_id=1, source=src, rank=1,
                                          title=f"跨平台热点{i}", captured_at=now))
        for src, title in singles:
            rows.append(HotSourceItem(user_id=1, source=src, rank=1,
                                      title=title, captured_at=now))
        session.add_all(rows)
        session.commit()

    def test_跨平台占满cap时单平台仍有保底(self, session) -> None:
        from app.services.hotspot_agent import _platform_hot_candidates

        self._mk(session, 70, [("bili-pan", "野鹅敢死队 经典影片")])
        cands = _platform_hot_candidates(session, 1, cap=60, cap_single=20)
        assert any("bili-pan" in c["platforms"] for c in cands), \
            "跨平台条目占满 cap 后,单平台条目必须仍有保底名额(否则这条链白采)"
        assert len(cands) == 61, f"60 条跨平台 + 1 条单平台 ⇒ 61,实际 {len(cands)}"

    def test_保底名额按平台权重取最好的(self, session) -> None:
        """单平台名额有限时,要按 `SOURCE_FIT` 取**权重最高**的 —— 否则保底也没意义。"""
        from app.services.hotspot_agent import _platform_hot_candidates

        self._mk(session, 70, [("weibo", "低权重条目"), ("bili-pan", "高权重条目")])
        cands = _platform_hot_candidates(session, 1, cap=60, cap_single=1)
        singles = [c for c in cands if "+" not in c["platforms"]]
        assert [c["keyword"] for c in singles] == ["高权重条目"], \
            "只剩 1 个单平台名额时应当给权重最高的 bili-pan(0.60),而不是 weibo(0.15)"

    def test_不给保底时退回原行为(self, session) -> None:
        """`cap_single=0` 时行为与改动前一致(便于线上把它关掉)。"""
        from app.services.hotspot_agent import _platform_hot_candidates

        self._mk(session, 70, [("bili-pan", "野鹅敢死队 经典影片")])
        cands = _platform_hot_candidates(session, 1, cap=60, cap_single=0)
        assert len(cands) == 60
        assert not any("bili-pan" in c["platforms"] for c in cands)
