"""过期转存清理单测(2026-10-03)。

用户口径:"可以定期清理久远资源,**一个星期内没有人再发了**就可以删除了。"
这里钉住三件事:① 判据(转存时间 or 外部又有人发)**取最新那个**;② 默认只预览不删;
③ **目录返回空必须报错** —— 迅雷接口失败也返回 `[]`,当成"没有过期资源"就是又一次静默假成功。
"""
import os
from datetime import datetime, timedelta

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db import models  # noqa: F401
from app.db.models import User, XunleiGroupShare
from app.services import xunlei_cleanup as xc
from app.services import xunlei_transfer as xt


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    db.add(User(id=1, username="u1", email="u1@b.c", password_hash="x", enabled=True))
    db.commit()
    yield db
    db.close()


class _S:
    xunlei_transfer_parent_id = "PID"
    xunlei_cleanup_max_per_run = 2


def _folder(name: str, days_ago: int, fid: str = "") -> dict:
    t = (datetime.now() - timedelta(days=days_ago)).isoformat()
    return {"kind": "drive#folder", "name": name, "id": fid or f"id-{name}",
            "created_time": t, "size": "0"}


def test_plan_flags_by_transfer_time_when_nobody_reposts(session, monkeypatch) -> None:
    """没人再发 → 按**转存时间**算闲置:超过 days 天即过期。"""
    monkeypatch.setattr(xt, "list_files", lambda *a, **k: [
        _folder("亚麻壁纸", 17), _folder("新资源A", 1)])
    p = xc.plan(session, 1, days=7, settings=_S())
    assert p["scanned"] == 2 and p["stale"] == 1 and p["keep"] == 1
    stale = next(f for f in p["folders"] if f["name"] == "亚麻壁纸")
    assert stale["days_idle"] >= 17 and stale["matched_by"] == "转存时间"


def test_plan_keeps_resource_that_someone_reposted_recently(session, monkeypatch) -> None:
    """**核心口径**:转存很久了,但**外面又有人发** → 不该删。

    这正是用户说的"一个星期内没有人再发了就可以删除"的那半边 ——
    `last_seen` 取"转存时间"与"外部来源时间"的**最大值**。
    """
    session.add(XunleiGroupShare(user_id=1, share_id="s1", group_id="g",
                                 title="手机警报器（警笛模拟器）2.0版",
                                 msg_time=datetime.now() - timedelta(days=1)))
    session.commit()
    monkeypatch.setattr(xt, "list_files", lambda *a, **k: [
        _folder("警笛模拟器", 30)])                      # 盘里那个 30 天前转的
    p = xc.plan(session, 1, days=7, settings=_S())
    f = p["folders"][0]
    assert f["matched_by"] == "外部又有人发" and f["days_idle"] <= 1
    assert p["stale"] == 0, "有人再发却判成过期 = 把活资源删了"


def test_plan_reports_error_on_empty_listing(session, monkeypatch) -> None:
    """⚠️ **目录返回空必须报错**,不能当成"没有过期资源"。

    迅雷接口失败也返回 `[]`(见 `xunlei_transfer.list_files` 的 except),照收就成了
    又一次静默假成功 —— 与本项目已修 4 次的 A 类问题同型。
    """
    monkeypatch.setattr(xt, "list_files", lambda *a, **k: [])
    p = xc.plan(session, 1, days=7, settings=_S())
    assert p["error"] and "空" in p["error"]
    assert p["scanned"] == 0


def test_plan_reports_error_when_parent_not_configured(session) -> None:
    class _NoParent:
        xunlei_transfer_parent_id = ""
        xunlei_cleanup_max_per_run = 2

    p = xc.plan(session, 1, days=7, settings=_NoParent())
    assert p["error"] and "parent_id" in p["error"]


def test_matches_requires_min_length_to_avoid_overmatch() -> None:
    """短的那侧要 ≥4 字,否则「软件」「资源」这种会把不相干的东西全匹配上。"""
    assert xc._matches("警笛模拟器", "手机警报器警笛模拟器20版")
    assert not xc._matches("软件", "某软件")
    assert not xc._matches("", "任何")


def test_run_cleanup_dry_run_never_deletes(session, monkeypatch) -> None:
    """默认只预览 —— 删盘难逆,不该被一个误点触发。"""
    called = {"n": 0}
    monkeypatch.setattr(xt, "list_files", lambda *a, **k: [_folder("旧资源", 30)])
    monkeypatch.setattr(xt, "trash_files", lambda ids: called.__setitem__("n", called["n"] + 1) or {})
    out = xc.run_cleanup(session, 1, days=7, settings=_S(), dry_run=True)
    assert out["to_delete"] == 1 and out["deleted"] == 0 and called["n"] == 0


def test_run_cleanup_moves_to_trash_and_respects_limit(session, monkeypatch) -> None:
    """真执行时:走 `trash_files`(**移入回收站**,留后悔路),并按单轮上限限流。"""
    deleted: list[str] = []
    monkeypatch.setattr(xt, "list_files", lambda *a, **k: [
        _folder("旧A", 30), _folder("旧B", 30), _folder("旧C", 30)])
    monkeypatch.setattr(xt, "trash_files",
                        lambda ids: deleted.extend(ids) or {"status": "ok", "deleted": len(ids)})
    out = xc.run_cleanup(session, 1, days=7, settings=_S(), dry_run=False)
    assert out["to_delete"] == 3 and out["deleted"] == 2      # _S.max_per_run = 2
    assert out["skipped_by_limit"] == 1 and len(deleted) == 2


def test_run_cleanup_also_unlists_from_resource_library(session, monkeypatch) -> None:
    """⚠️ **删了盘上的资源,资源清单里的那一行也要删**(2026-10-03)。

    `xunlei_resources` 是 `resource_library` 的取链来源 —— 盘上删了却留着行,
    **资源库就会给出已经失效的分享链**(比"没有链"更糟:用户点了打不开)。
    """
    from app.db.models import XunleiResource

    session.add(XunleiResource(user_id=1, fid="id-旧资源", name="旧资源",
                               share_url="https://pan.xunlei.com/s/DEAD", synced_at=None))
    session.commit()
    monkeypatch.setattr(xt, "list_files", lambda *a, **k: [_folder("旧资源", 30, fid="id-旧资源")])
    monkeypatch.setattr(xt, "trash_files", lambda ids: {"status": "ok", "deleted": len(ids)})
    out = xc.run_cleanup(session, 1, days=7, settings=_S(), dry_run=False)
    assert out["deleted"] == 1 and out["unlisted"] == 1
    assert session.query(XunleiResource).count() == 0


def test_dry_run_does_not_touch_resource_library(session, monkeypatch) -> None:
    """预览**什么都不动** —— 包括资源清单(别把"看一眼"变成写操作)。"""
    from app.db.models import XunleiResource

    session.add(XunleiResource(user_id=1, fid="id-旧资源", name="旧资源",
                               share_url="https://pan.xunlei.com/s/DEAD"))
    session.commit()
    monkeypatch.setattr(xt, "list_files", lambda *a, **k: [_folder("旧资源", 30, fid="id-旧资源")])
    out = xc.run_cleanup(session, 1, days=7, settings=_S(), dry_run=True)
    assert out["deleted"] == 0 and session.query(XunleiResource).count() == 1
