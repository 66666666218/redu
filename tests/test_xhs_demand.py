"""小红书「求1型」扫描的**纯逻辑**测试(2026-10-30)。不开浏览器、不联网。

浏览器那半(`search_notes`/`note_comments`)只能在**人开着并登录过的**那个 CDP 浏览器上实测,
所以这里守的是:
1. 判据与抖音**同一套**(密度/重复率来自 `demand_watch` 的归一化);
2. CDP 不可达时**明确 skip**,不自己拉起浏览器(脚本拉起的浏览器 `webdriver=True`,那条路就废了)。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest

from app.services import xhs_demand as X


def test_评论统计用同一套判据():
    texts = ["1", "1", "求完整版", "还有吗", "求", "好看", "1", "求链接", "哈哈", "1"]
    st = X.stats_from_texts(texts)

    assert st["total"] == 10
    assert st["ones"] == 4
    assert st["demand"] == 4          # 求完整版 / 求 / 求链接 / 还有吗(「有吗」也算求)
    assert st["dup_ratio"] > 0


def test_重复短句也按归一算():
    texts = ["刚看完", "刚看完[笑哭]", "刚看完。", "冰露", "好看", "1"]
    st = X.stats_from_texts(texts)
    assert st["dup_ratio"] >= 0.3


class _S:
    demand_xhs_enabled = True
    demand_seed_words = "小小绘"


def test_CDP不可达时明确跳过_不自己开浏览器(monkeypatch):
    from app.services import demand_watch as dw

    monkeypatch.setattr(X, "available", lambda settings=None: (False, "CDP 不可达"))
    out = dw.scan_xhs(None, 1, _S())

    assert out["skipped"] == "CDP 不可达"


def test_关掉开关就不碰浏览器(monkeypatch):
    from app.services import demand_watch as dw

    class S(_S):
        demand_xhs_enabled = False

    monkeypatch.setattr(X, "available", lambda settings=None: pytest.fail("关了开关不该探活"))
    assert dw.scan_xhs(None, 1, S())["skipped"] == "disabled"


# ---------------------------------------------------------------- 小样本不判

class _Page:
    def close(self):
        pass


class _Ctx:
    def new_page(self):
        return _Page()


class _Browser:
    contexts = [_Ctx()]

    def close(self):
        pass


class _P:
    def stop(self):
        pass


@pytest.fixture
def session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db import models  # noqa: F401
    from app.db.database import Base
    from app.db.models import User

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    db.add(User(id=1, username="u1", email="u1@b.c", password_hash="x", enabled=True))
    db.commit()
    yield db
    db.close()


def test_评论太少只记不判(session, monkeypatch):
    """用户给的「小马云」那条只有 4 条评论 —— 样本太小时密度是噪音。"""
    from app.db.models import DemandScan
    from app.services import demand_watch as dw

    class S(_S):
        demand_xhs_min_comments = 8
        demand_xhs_notes_per_seed = 1

    monkeypatch.setattr(X, "available", lambda settings=None: (True, "ok"))
    monkeypatch.setattr(X, "_connect", lambda: (_P(), _Browser()))
    monkeypatch.setattr(X, "search_notes", lambda *a, **k: [
        {"id": "n1", "token": "t", "title": "小马云也是放飞自我了#范小勤", "author": "A",
         "comments": 20}])
    monkeypatch.setattr(X, "note_comments", lambda *a, **k: ["求🥹", "好看", "有", "好新鲜"])
    monkeypatch.setattr(dw, "push_request", lambda *a, **k: pytest.fail("样本不足不该推"))

    out = dw.scan_xhs(session, 1, S())

    assert out["scanned"] == 1 and out["flagged"] == 0
    row = session.query(DemandScan).one()
    assert row.why == "样本不足" and row.flagged is False
