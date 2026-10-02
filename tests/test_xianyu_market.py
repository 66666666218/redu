"""闲鱼**行情**单测(2026-10-03):想要数免费到手 + 价位行情 / 供需比。

**背景**:需求热度(想要数)此前唯一来源是详情接口,而它被阿里无痕验证挡着(要人工拖滑块)。
今天发现搜索响应里的 `fishTags` 标签就写着「6770人想要」(90 条命中 86 条 = 95%),
于是行情**不必打详情接口**。这里钉住:解析、按 source 的优先级、供需比聚合。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db import models  # noqa: F401
from app.db.models import User, XianyuDaily, XianyuItem
from app.services import xianyu
from app.services import xianyu_analytics as xa


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    db.add(User(id=1, username="u1", email="u1@b.c", password_hash="x", enabled=True))
    db.commit()
    yield db
    db.close()


def _card(tags: list[str], detail: dict | None = None) -> dict:
    return {
        "itemId": "1",
        "title": "剪映会员vip",
        "price": [{"text": "¥1.28"}],
        "userNickName": "倾歌佳佳",
        "fishTags": {"r3": {"tagList": [{"data": {"content": t}} for t in tags]}},
        "detailParams": detail or {},
    }


# ---------------------------------------------------------------- 解析

def test_item_want_reads_the_rendered_tag_not_the_empty_field() -> None:
    """想要数在 `fishTags` 的**渲染文案**里,而正经的 `want` 字段恒为空串。

    这条是本次修复的核心:只看 `want` 字段会得到 0(实测 89 条全空),看标签才拿到真值。
    """
    it = _card(["6770人想要", "回复超快"])
    it["want"] = ""                       # 搜索接口原样返回的空字段
    assert xianyu.item_want(it) == 6770


def test_item_want_handles_wan_unit() -> None:
    """「1.2万人想要」要按 12000 算,不能当 1。"""
    assert xianyu.item_want(_card(["1.2万人想要"])) == 12000


def test_item_want_zero_when_absent() -> None:
    """没有该标签时返回 0(而不是抛错),让调用方按"未知"处理。"""
    assert xianyu.item_want(_card(["回复超快", "N分钟内发货"])) == 0


def test_item_tags_excludes_want_to_avoid_duplication() -> None:
    """「人想要」已单列成 want_count,标签串里要排除它,免得白占列宽/重复。"""
    it = _card(["6770人想要", "累计降价5%", "百分百好评"])
    assert "人想要" not in xianyu.item_tags(it)
    assert "累计降价5%" in xianyu.item_tags(it)


def test_item_sold_price_prefers_detail_params() -> None:
    """到手价取 `detailParams.soldPrice`,比 `price` 那段富文本好解析。"""
    assert xianyu.item_sold_price(_card(["1人想要"], detail={"soldPrice": "1.28"})) == "1.28"
    assert xianyu.item_sold_price(_card([])) == ""


# ---------------------------------------------------------------- 快照落库 / 优先级

def test_record_search_snapshot_writes_source_search(session) -> None:
    n = xa.record_search_snapshot(session, 1, [
        {"item_id": "1", "title": "剪映会员", "want_count": 6770, "sold_price": "1.28", "tags": "回复超快"},
    ])
    assert n == 1
    row = session.query(XianyuDaily).one()
    assert row.want_count == 6770 and row.source == "search" and row.price == "1.28"


def test_record_search_snapshot_never_overwrites_detail_row(session) -> None:
    """深采行含搜索给不出的收藏/出单/浏览量 —— 搜索绝不能盖掉它(会把这些抹成 0)。"""
    session.add(XianyuDaily(user_id=1, item_id="1", snap_date="2026-10-03", title="t",
                            source="detail", want_count=99, collect_count=12, sold_count=3))
    session.commit()
    today = "2026-10-03"
    n = xa.record_search_snapshot(session, 1, [
        {"item_id": "1", "title": "t", "want_count": 5, "sold_price": "1"},
    ], today=today)
    assert n == 0
    row = session.query(XianyuDaily).one()
    assert (row.want_count, row.collect_count, row.sold_count, row.source) == (99, 12, 3, "detail")


def test_deep_done_today_ignores_search_rows(session) -> None:
    """搜索行不算"已深采" —— 否则深采永远没活干,收藏/出单/浏览量永久为空。"""
    xa.record_search_snapshot(session, 1, [{"item_id": "1", "title": "t", "want_count": 5}],
                              today="2026-10-03")
    session.add(XianyuDaily(user_id=1, item_id="2", snap_date="2026-10-03", title="t2", source="detail"))
    session.commit()
    assert xa._deep_done_today(session, 1, "2026-10-03") == {"2"}


# ---------------------------------------------------------------- 价位行情

def _item(iid: str, kw: str, price: str, want: int, tags: str = "") -> XianyuItem:
    return XianyuItem(user_id=1, item_id=iid, title=f"{kw}-{iid}", price=price,
                      sold_price=price, want_count=want, tags=tags, keywords=kw, seller="s")


def test_xianyu_market_computes_supply_demand_and_ratio(session) -> None:
    session.add_all([
        _item("1", "剪映会员", "1.28", 100),
        _item("2", "剪映会员", "2.00", 200, tags="累计降价5%"),
        _item("3", "剪映会员", "3.00", 300),
        _item("4", "ps教程", "9.90", 900),
    ])
    session.commit()
    m = xa.xianyu_market(session, 1)
    assert m["item_count"] == 4 and m["demand"]["want_total"] == 1500
    # 4 个价 [1.28, 2.00, 3.00, 9.90] → 中位数 = 中间两个的平均
    assert m["supply"]["median_price"] == 2.5 and m["supply"]["min_price"] == 1.28
    by_kw = {k["keyword"]: k for k in m["keywords"]}
    assert by_kw["剪映会员"]["items"] == 3 and by_kw["剪映会员"]["price_cut"] == 1
    # 供需比 = 想要总数 ÷ 在售条数。ps教程 比值更高(900/1)但**只有 1 条在售**,
    # 被"蓝海榜需 ≥3 条"的门槛挡掉(1 条样本能刷出任何离谱比值)—— 见下一条用例。
    assert [k["keyword"] for k in m["blue_ocean"]] == ["剪映会员"]
    assert m["ratio"] == 375.0
    assert sum(b["count"] for b in m["price_buckets"]) == 4


def test_xianyu_market_blue_ocean_needs_three_listings(session) -> None:
    """样本太小的词噪音大(1 条就能刷出离谱供需比)→ 蓝海榜要求 ≥3 条在售,再按比值排。"""
    session.add_all([
        _item("1", "冷门词", "1.00", 99999),          # 比值爆表但只有 1 条 → 必须被排除
        _item("2", "热词", "1.00", 10), _item("3", "热词", "1.00", 10), _item("4", "热词", "1.00", 10),
        _item("5", "温词", "1.00", 100), _item("6", "温词", "1.00", 100), _item("7", "温词", "1.00", 100),
    ])
    session.commit()
    m = xa.xianyu_market(session, 1)
    assert [k["keyword"] for k in m["blue_ocean"]] == ["温词", "热词"]   # 100 > 10
