"""热榜源契约单测(v2.2.0 命门自持架构):解析/契约/错误路径。"""
import pytest


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
