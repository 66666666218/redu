"""清洗模块单测(纯逻辑,无网络)。"""
from app.models import HotItem
from app.services.cleaner import clean


def _item(title: str, heat: int) -> HotItem:
    return HotItem(title=title, heat=heat)


def test_filters_low_heat(settings) -> None:
    items = [_item("普通词", 50), _item("热搜词", 500)]
    result = clean(items, settings)
    titles = [it.title for it in result]
    assert "热搜词" in titles
    assert "普通词" not in titles


def test_filters_blacklist(settings) -> None:
    items = [_item("置顶", 99999), _item("广告", 88888), _item("真实热点", 6000)]
    result = clean(items, settings)
    titles = [it.title for it in result]
    assert titles == ["真实热点"]


def test_deduplicate_by_title(settings) -> None:
    items = [_item("重复词", 300), _item("重复词", 200)]
    result = clean(items, settings)
    assert len(result) == 1


def test_respects_top_n(settings) -> None:
    # top_n=5,给出 6 个满足热度阈值的词
    items = [_item(f"词{i}", 1000 + i) for i in range(6)]
    result = clean(items, settings)
    assert len(result) == 5
    # 结果按热度降序
    heats = [it.heat for it in result]
    assert heats == sorted(heats, reverse=True)
