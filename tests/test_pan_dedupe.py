"""盘内去重(排定作业)单测(2026-10-07)。

用户口径「B 自动删就可以」—— 排定的周作业改成真的删。**删盘是难逆操作**,
所以这里盯的是三条"不许越界"的规矩,而不是"删得对不对"(那由 `build_plan` 的用例管)。

⚠️ 缘起:排定的那套(`xunlei_cleanup.dedupe_duplicates`)**从来没删掉过东西**
(10-04 找到 4 组、全判"不完全相同"、删 0),而能找出重复的那套一直是手动脚本
⇒ **"有个去重作业"这件事看起来成立,实际上一件都没删过**。
"""
import os
from datetime import datetime, timedelta

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.db import models  # noqa: E402,F401
from app.db.database import Base  # noqa: E402
from app.db.models import RunRecord, User  # noqa: E402
from app.services import pan_dedupe as pd  # noqa: E402

NOW = datetime.now()


@pytest.fixture
def session():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)()
    db.add(User(id=1, username="u1", email="u1@b.c", password_hash="x", enabled=True))
    db.commit()
    yield db
    db.close()


class _S:
    xunlei_dedupe_enabled = True
    xunlei_dedupe_depth = 3
    xunlei_dedupe_max_per_run = 30


def _group(name: str, drop_ids: list[str], risky: bool = False) -> dict:
    return {"name": name, "parent": "/x", "risky": risky, "risky_reason": "大",
            "keep": {"id": "K" + name, "items": 9, "size": 900, "path": "/x/" + name},
            "drop": [{"id": i, "items": 1, "size": 10, "path": f"/x/{name}({n})"}
                     for n, i in enumerate(drop_ids)]}


def _plan(groups: list[dict], complete: bool = True) -> dict:
    safe = [g for g in groups if not g["risky"]]
    return {"plan": groups, "scanned_dirs": 10, "calls": 20, "complete": complete,
            "n_drop": sum(len(g["drop"]) for g in safe),
            "n_risky": sum(1 for g in groups if g["risky"]),
            "freed": sum(s["size"] for g in safe for s in g["drop"]),
            "risky_freed": 0}


class TestTrashPlan:
    def test_默认跳过风险组(self, monkeypatch) -> None:
        """⚠️ "要删的比留的还大"多半意味着**那两份根本不是同一样东西** —— 绝不自动删。"""
        sent: list[list[str]] = []
        monkeypatch.setattr(pd.xt, "trash_files",
                            lambda fids: sent.append(list(fids)) or {"deleted": len(fids)})
        plan = [_group("安全", ["a", "b"]), _group("风险", ["c"], risky=True)]
        out = pd.trash_plan(plan)
        assert out["fids"] == ["a", "b"] and sent == [["a", "b"]]

    def test_单轮上限生效(self, monkeypatch) -> None:
        """一次删太多难复核 —— 超上限的余下下周继续。"""
        monkeypatch.setattr(pd.xt, "trash_files",
                            lambda fids: {"deleted": len(fids)})
        out = pd.trash_plan([_group("大组", ["a", "b", "c", "d"])], cap=2)
        assert out["fids"] == ["a", "b"]

    def test_没有要删的就不调接口(self, monkeypatch) -> None:
        called: list = []
        monkeypatch.setattr(pd.xt, "trash_files", lambda fids: called.append(fids) or {})
        assert pd.trash_plan([_group("只风险", ["a"], risky=True)])["deleted"] == 0
        assert called == []


class TestTick:
    def _patch(self, monkeypatch, plan: dict):
        monkeypatch.setattr(pd, "build_plan", lambda *a, **k: plan)
        monkeypatch.setattr(pd.xt, "trash_files",
                            lambda fids: {"deleted": len(fids), "errors": []})
        monkeypatch.setattr(pd, "_notify", lambda *a, **k: None)
        monkeypatch.setattr("app.db.get_session_local", lambda: (lambda: None))

    def test_开关关了就什么都不做(self, session, monkeypatch) -> None:
        monkeypatch.setattr("app.db.get_session_local", lambda: (lambda: session))

        class _Off(_S):
            xunlei_dedupe_enabled = False

        assert pd.pan_dedupe_tick(settings=_Off()) == 0

    def test_扫不完就一个都不删(self, session, monkeypatch) -> None:
        """★★ **本模块最要紧的一条**:撞 API 预算时 `_dir_stats` 只能数到一部分,
        而"留内容多的那份"**恰恰依赖条目数** —— 数了一半就选,可能把内容更全的那份删掉。
        """
        self._patch(monkeypatch, _plan([_group("g", ["a"])], complete=False))
        monkeypatch.setattr("app.db.get_session_local", lambda: (lambda: session))
        assert pd.pan_dedupe_tick(settings=_S()) == 0
        assert not session.query(RunRecord).filter_by(kind="xunlei_dedupe").filter(
            RunRecord.status == "success").all(), "没扫完却记了 success"
        note = session.query(RunRecord).filter_by(kind="xunlei_dedupe").one().detail
        assert "撞 API 预算" in note

    def test_正常一轮会删并记账(self, session, monkeypatch) -> None:
        self._patch(monkeypatch, _plan([_group("g", ["a", "b"])]))
        monkeypatch.setattr("app.db.get_session_local", lambda: (lambda: session))
        assert pd.pan_dedupe_tick(settings=_S()) == 2
        rec = session.query(RunRecord).filter_by(kind="xunlei_dedupe").one()
        assert rec.status == "success" and "移入回收站 2 个整包" in rec.detail

    def test_风险组不删但要说出来(self, session, monkeypatch) -> None:
        self._patch(monkeypatch, _plan([_group("安全", ["a"]), _group("风险", ["b"], risky=True)]))
        monkeypatch.setattr("app.db.get_session_local", lambda: (lambda: session))
        seen: dict = {}
        monkeypatch.setattr(pd, "_notify", lambda db, uid, note, risky, errs, **k:
                            seen.update(note=note, risky=risky))
        assert pd.pan_dedupe_tick(settings=_S()) == 1
        assert len(seen["risky"]) == 1, "风险组必须进告警交人工看"
        assert "跳过risky1" in seen["note"]

    def test_超上限要说明余下下周继续(self, session, monkeypatch) -> None:
        self._patch(monkeypatch, _plan([_group("g", ["a", "b", "c"])]))
        monkeypatch.setattr("app.db.get_session_local", lambda: (lambda: session))

        class _Cap(_S):
            xunlei_dedupe_max_per_run = 1

        assert pd.pan_dedupe_tick(settings=_Cap()) == 1
        assert "余下下周继续" in session.query(RunRecord).filter_by(
            kind="xunlei_dedupe").one().detail
