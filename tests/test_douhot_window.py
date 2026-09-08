"""抖音关键词多窗口对比(近1h vs 近1天)单测:mock 热点宝接口,零网络。"""
import os
from datetime import datetime, timedelta

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db import models  # noqa: F401
from app.db.models import DouhotWatch, DouhotWindowSnap
from config.settings import Settings
from app.services import douhot
from app.services import douhot_window


def _settings(**kw) -> Settings:
    base = {"douhot_window_windows": "1,24", "douhot_alert_cooldown_hours": 24}
    base.update(kw)
    return Settings(_env_file=None, is_dev=True, **base)


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    yield db
    db.close()


def test_window_contrast_signals() -> None:
    # 冷启动:两窗口都冷
    assert douhot.window_contrast({"score": 0}, {"score": 0})["signal"] == "flat"
    # 新起势:近1天冷而近1h起来
    c = douhot.window_contrast({"score": 50}, {"score": 0})
    assert c["signal"] == "burst" and c["label"] == "新起势"
    # 爆发:近1h ≥ 近1天 1.5 倍
    c = douhot.window_contrast({"score": 150}, {"score": 100})
    assert c["signal"] == "burst" and c["label"] == "爆发" and c["ratio"] == 1.5
    # 回落:近1h 不足近1天一半
    c = douhot.window_contrast({"score": 30}, {"score": 100})
    assert c["signal"] == "fall" and c["label"] == "回落"
    # 高位延续
    assert douhot.window_contrast({"score": 80}, {"score": 100})["signal"] == "steady"


def test_fetch_keyword_windows_calls_both_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def _fake_heat(cookie, kw, settings=None, date_window=None):
        calls.append(date_window)
        return {"keyword": kw, "score": date_window * 10, "trend_growth": 0.1, "trend_label": "上升期"}

    monkeypatch.setattr(douhot, "fetch_keyword_heat", _fake_heat)
    out = douhot.fetch_keyword_windows("ck", "测试词", "word", settings=_settings())
    assert list(out.keys()) == [1, 24]
    assert out[1]["score"] == 10 and out[24]["score"] == 240
    assert calls == [1, 24]
    # 自定义窗口集
    out2 = douhot.fetch_keyword_windows("ck", "w", "word", settings=_settings(), windows=[1, 24, 72])
    assert sorted(out2.keys()) == [1, 24, 72]


def test_collect_windows_writes_snaps(monkeypatch: pytest.MonkeyPatch, session) -> None:
    import app.services.douhot_window as dw_mod
    import app.services.cookie_store as cs

    session.add(DouhotWatch(user_id=1, section="douhot", list_type="word", keyword="花少2", date_window=1))
    session.add(DouhotWatch(user_id=1, section="douhot", list_type="video", keyword="乡镇晋升录", date_window=24))
    session.commit()
    monkeypatch.setattr(dw_mod, "get_cookies", lambda db, uid: {"douyin": "ck"})
    monkeypatch.setattr(douhot, "fetch_keyword_windows",
                        lambda cookie, kw, lt, settings=None, windows=None: {
                            1: {"score": 150, "rank_now": 1, "title": kw + "主题"},
                            24: {"score": 100, "rank_now": 2, "title": kw + "主题"},
                        })
    out = dw_mod.collect_windows(session, 1, settings=_settings())
    assert out["status"] == "success" and out["words"] == 2 and out["ok"] == 2 and out["snaps"] == 4
    snaps = session.scalars(select(DouhotWindowSnap)).all()
    assert len(snaps) == 4
    assert {s.window for s in snaps} == {1, 24}
    assert all(s.captured_at == snaps[0].captured_at for s in snaps)  # 同一轮一批


def test_analytics_uses_latest_batch_and_contrast(session) -> None:
    ts1 = datetime.now()
    ts0 = ts1 - timedelta(minutes=10)
    # 旧批(不应被取)
    session.add_all([
        DouhotWindowSnap(user_id=1, list_type="word", keyword="测试词", window=1, score=1, captured_at=ts0),
        DouhotWindowSnap(user_id=1, list_type="word", keyword="测试词", window=24, score=1, captured_at=ts0),
    ])
    # 最新批
    session.add_all([
        DouhotWindowSnap(user_id=1, list_type="word", keyword="测试词", window=1, score=150, captured_at=ts1),
        DouhotWindowSnap(user_id=1, list_type="word", keyword="测试词", window=24, score=100, captured_at=ts1),
    ])
    session.commit()
    rows = douhot_window.analytics(session, 1, settings=_settings())
    assert len(rows) == 1
    r = rows[0]
    assert r["h1_score"] == 150 and r["h24_score"] == 100 and r["ratio"] == 1.5
    assert r["signal"] == "burst" and r["label"] == "爆发"


def test_collect_skips_without_watch_or_cookie(session) -> None:
    out = douhot_window.collect_windows(session, 1, settings=_settings())
    assert out["status"] == "skipped" and out["reason"] == "no_watch"
    session.add(DouhotWatch(user_id=1, section="douhot", list_type="word", keyword="词"))
    session.commit()
    out = douhot_window.collect_windows(session, 1, settings=_settings())
    assert out["status"] == "skipped" and out["reason"] == "no_cookie"
