"""闲鱼热榜采集:抽取/价格/去重排名单测(纯逻辑,无网络)。"""
from datetime import datetime

from config.settings import Settings
from app.services.notifier import NullNotifier
from app.services.xianyu import _extract_items, collect_hot, item_price, run_xianyu_daily, summarize_today
from app.storage import ArchiveRepository


def _item(iid: int, title: str, price: object = None) -> dict:
    return {
        "itemId": str(iid),
        "title": title,
        "price": price if price is not None else [{"text": "¥"}, {"text": "1"}],
        "userNickName": "卖家甲",
        "picUrl": f"p{iid}.png",
    }


class FakeClient:
    def __init__(self, per_kw: dict[str, list[dict]]) -> None:
        self.per_kw = per_kw

    def search(self, keyword: str) -> list[dict]:
        return self.per_kw[keyword]


def test_extract_items_from_nested() -> None:
    obj = {"data": {"resultInfo": {"items": [_item(1, "A"), _item(2, "B")]}}}
    items = _extract_items(obj)
    assert len(items) == 2
    assert items[0]["title"] == "A"


def test_price_normalizes_repeated_yen() -> None:
    assert item_price({"price": [{"text": "¥"}, {"text": "¥"}, {"text": "1.5"}]}) == "¥1.5"


def test_collect_hot_dedupe_and_rank(settings: Settings) -> None:
    # kw1 命中 item1(综合#1)、item2(综合#2);kw2 命中 item1(综合#1)
    fake = FakeClient({"kw1": [_item(1, "A"), _item(2, "B")], "kw2": [_item(1, "A")]})
    settings.xianyu_keywords = "kw1,kw2"
    settings.xianyu_top_n = 10
    out = collect_hot(settings, client=fake)
    assert len(out) == 2
    assert out[0]["item_id"] == "1"
    assert out[0]["hit_keywords"] == 2
    assert out[0]["best_rank"] == 1
    assert out[1]["item_id"] == "2"
    assert out[1]["hit_keywords"] == 1


def test_collect_hot_respects_top_n(settings: Settings) -> None:
    fake = FakeClient({"kw": [_item(i, f"商品{i}") for i in range(50)]})
    settings.xianyu_keywords = "kw"
    settings.xianyu_top_n = 3
    out = collect_hot(settings, client=fake)
    assert len(out) == 3


def _today_item(item_id: str, title: str, best_rank: int, keywords: str, price: str = "¥1") -> dict:
    return {
        "item_id": item_id,
        "title": title,
        "price": price,
        "seller": "",
        "pic": "",
        "hit_keywords": 1,
        "best_rank": best_rank,
        "keywords": keywords,
        "created_at": datetime.now().isoformat(),
    }


def test_summarize_today_aggregates_and_ranks() -> None:
    items = [
        _today_item("1", "商品A", 1, "kw1"),
        _today_item("1", "商品A", 2, "kw2", "¥2"),
        _today_item("2", "商品B", 4, "kw1"),
    ]
    out = summarize_today(items)
    assert out[0]["item_id"] == "1"
    assert out[0]["occurrences"] == 2
    assert out[0]["best_rank"] == 1
    assert out[0]["price"] == "¥2"
    assert "kw1" in out[0]["keywords"] and "kw2" in out[0]["keywords"]
    assert out[1]["item_id"] == "2"


def test_run_xianyu_daily(settings: Settings, tmp_path) -> None:
    repo = ArchiveRepository(tmp_path / "data")
    repo.save_xianyu_top("run_1", [_today_item("1", "商品A", 1, "kw1"), _today_item("2", "商品B", 2, "kw2")])
    notifier = NullNotifier()
    out = run_xianyu_daily(settings=settings, repo=repo, notifier=notifier)
    assert out["count"] == 2
    assert out["items"][0]["item_id"] == "1"
    stored = repo.latest_xianyu_summary()
    assert stored["items"][0]["item_id"] == "1"
