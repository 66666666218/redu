"""重点关键词告警单测:跨板块共振/板块内反复/冷却去重。"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db import models  # noqa: F401
from app.db.models import BaiduHotItem, FeishuAlert, WeiboHotItem, XianyuItem
from app.services import feishu_client, focus_alert


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    yield db
    db.close()


@pytest.fixture
def st():
    from config.settings import Settings
    return Settings(_env_file=None, is_dev=True,
                    feishu_webhook="https://open.feishu.cn/hook/main",
                    feishu_webhook_xianyu="https://open.feishu.cn/hook/xianyu",
                    focus_repeat_rounds=3, focus_min_len=4, focus_cooldown_hours=24)


class _FakeFeishu:
    sent: list[str] = []
    last_webhook: str = ""

    def __init__(self, webhook, secret="") -> None:
        _FakeFeishu.last_webhook = webhook

    def send(self, msg: str) -> bool:
        _FakeFeishu.sent.append(msg)
        return True

    def send_card(self, card: dict) -> bool:
        import json
        _FakeFeishu.sent.append(json.dumps(card, ensure_ascii=False))
        return True


@pytest.fixture(autouse=True)
def _fake_feishu(monkeypatch):
    _FakeFeishu.sent = []
    monkeypatch.setattr(feishu_client, "FeishuClient", _FakeFeishu)


def _now(): return datetime.now()


def test_cross_board_resonance_triggers(session, st) -> None:
    """同一关键词出现在微博+百度最新一批 → 🔴重点跨板块卡片推总群。"""
    for i, ts in enumerate([_now() - timedelta(hours=i) for i in range(3)]):
        session.add(WeiboHotItem(user_id=1, title="宫崎骏动画刷屏", heat=100000 + i, rank=1, captured_at=ts))
    session.add(BaiduHotItem(user_id=1, title="宫崎骏动画", heat=50000, rank=2, captured_at=_now()))
    session.add(XianyuItem(user_id=1, item_id="q0", title="无关商品", price="¥1", hit_keywords=1,
                           best_rank=3, created_at=_now()))
    session.commit()

    n = focus_alert.run_focus_alert(session, 1, settings=st)
    assert n == 2  # 同一关键词可同时触发:①跨板块共振卡(总群) ②板块内反复卡(微博群)
    all_text = " ".join(_FakeFeishu.sent)
    assert "跨板块共振" in all_text and "宫崎骏动画" in all_text
    assert "微博" in all_text and "百度" in all_text
    # 24h 冷却:第二轮不重推
    assert focus_alert.run_focus_alert(session, 1, settings=st) == 0


def test_containment_match_across_boards(session, st) -> None:
    """短标题包含于长标题(≥focus_min_len)也算共振。"""
    session.add(WeiboHotItem(user_id=1, title="宫崎骏动画", heat=1, rank=1, captured_at=_now()))
    session.add(XianyuItem(user_id=1, item_id="q1", title="宫崎骏动画全集4K修复版", price="¥30",
                           hit_keywords=1, best_rank=1, created_at=_now()))
    session.commit()
    assert focus_alert.run_focus_alert(session, 1, settings=st) == 1


def test_repeat_in_one_board_triggers_to_dedicated_group(session, st) -> None:
    """同一商品近24h出现≥3轮 → 反复卡片推闲鱼专属群(而非总群)。"""
    for i in range(3):
        session.add(XianyuItem(user_id=1, item_id=f"q{i}", title="某热门资源包", price="¥19.9",
                               hit_keywords=2, best_rank=1 + i,
                               created_at=_now() - timedelta(hours=i * 2)))
    session.commit()
    n = focus_alert.run_focus_alert(session, 1, settings=st)
    assert n == 1
    card = _FakeFeishu.sent[-1]
    assert "反复出现" in card and "某热门资源包" in card and "3 轮" in card
    assert _FakeFeishu.last_webhook == "https://open.feishu.cn/hook/xianyu"  # 推的是闲鱼专属群


def test_repeat_below_rounds_no_trigger(session, st) -> None:
    session.add(XianyuItem(user_id=1, item_id="q9", title="只出现一次的资源", price="¥5",
                           hit_keywords=1, best_rank=1, created_at=_now()))
    session.commit()
    assert focus_alert.run_focus_alert(session, 1, settings=st) == 0


def test_unrelated_boards_no_false_match(session, st) -> None:
    session.add(WeiboHotItem(user_id=1, title="天气预报", heat=1, rank=1, captured_at=_now()))
    session.add(BaiduHotItem(user_id=1, title="股市行情", heat=1, rank=1, captured_at=_now()))
    session.commit()
    assert focus_alert.run_focus_alert(session, 1, settings=st) == 0
