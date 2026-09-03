"""百度采集解析 + 跨平台共同上升聚合单测(不联网)。"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db import models  # noqa: F401
from app.db.models import BaiduHotItem, WeiboHotItem
from app.services import baidu as baidu_svc
from app.services import cross_platform as cp


def _session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


# ---- 百度解析 ----
def test_collect_parses_nested_board() -> None:
    """top.baidu.com 深嵌套结构:递归提取 word+hotTag。"""
    sample = {"data": {"cards": [{"component": "a", "content": [{"content": [
        {"word": "火币", "hotTag": "12345", "url": "u1"},
        {"word": "新词", "hotTag": "6千", "url": "u2"},
        {"word": "纯注释无url", "desc": "x"},
    ]}]}]}}
    items = []
    baidu_svc._collect(sample, items, set())
    assert [i.title for i in items] == ["火币", "新词"]
    assert items[0].heat == 12345
    assert items[1].heat == 6000  # "6千" → 6000
    assert items[0].url == "u1"


def test_to_int_formats() -> None:
    assert baidu_svc._to_int("12345") == 12345
    assert baidu_svc._to_int("6千") == 6000
    assert baidu_svc._to_int("1.2万") == 12000
    assert baidu_svc._to_int("abc") == 0


# ---- 跨平台共同上升 ----
def test_rising_across_common_keyword() -> None:
    """关键词在微博+百度都在上升期 → 判定为跨平台共同上升(≥2板块)。"""
    db = _session()
    base = datetime(2026, 9, 1, 8)
    # 微博:某词加速上升
    for i, h in enumerate([1000, 1300, 1800, 2600]):
        db.add(WeiboHotItem(user_id=1, title="共同词", heat=h, rank=1, captured_at=base))
    # 百度:同一词也在上升
    for i, h in enumerate([500, 700, 1000, 1600]):
        db.add(BaiduHotItem(user_id=1, title="共同词", heat=h, rank=1, captured_at=base))
    # 只在微博上升(百度平稳)的词 → 不算共同
    for i, h in enumerate([100, 200, 300, 400]):
        db.add(WeiboHotItem(user_id=1, title="仅微博", heat=h, rank=2, captured_at=base))
    for i, h in enumerate([100, 100, 100, 100]):
        db.add(BaiduHotItem(user_id=1, title="仅微博", heat=h, rank=2, captured_at=base))
    db.commit()

    items = cp.rising_across(db, 1, min_platforms=2)
    assert items and items[0]["keyword"] == "共同词"
    assert items[0]["platforms"] == ["weibo", "baidu"]
    assert "仅微博" not in [i["keyword"] for i in items]
    db.close()
