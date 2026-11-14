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
from sqlalchemy import create_engine, select
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


def test_plan_keeps_unmatched_folder(session, monkeypatch) -> None:
    """★ 2026-10-20 用户口径:「**监控到的文件才适合这个规则**」。

    没匹配到任何监控源的文件夹 —— 哪怕转存很久 —— **一律保留**:
    它可能是人工收藏,或不属于本流水线。只统计 `unmatched`,绝不判 stale。
    """
    monkeypatch.setattr(xt, "list_files", lambda *a, **k: [
        _folder("亚麻壁纸", 17), _folder("新资源A", 1)])
    monkeypatch.setattr(xt, "list_all_files", lambda *a, **k: [
        _folder("亚麻壁纸", 17), _folder("新资源A", 1)])
    p = xc.plan(session, 1, days=7, settings=_S())
    assert p["scanned"] == 2 and p["stale"] == 0 and p["keep"] == 2
    assert p["unmatched"] == 2
    f = next(f for f in p["folders"] if f["name"] == "亚麻壁纸")
    assert f["monitored"] is False and f["matched_by"] == "未匹配监控源"


def test_plan_flags_matched_folder_when_nobody_reposts(session, monkeypatch) -> None:
    """匹配到监控源、但**最近一次提到也已超过 days 天** ⇒ 才判过期。"""
    session.add(XunleiGroupShare(
        user_id=1, share_id="s-old", group_id="g", title="亚麻壁纸",
        msg_time=datetime.now() - timedelta(days=17)))
    session.commit()
    monkeypatch.setattr(xt, "list_all_files", lambda *a, **k: [_folder("亚麻壁纸", 30)])
    p = xc.plan(session, 1, days=7, settings=_S())
    assert p["stale"] == 1
    f = p["folders"][0]
    assert f["monitored"] is True and f["matched_by"] == "外部又有人发"
    assert f["days_idle"] >= 16


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
    # ★ 代码现在走翻页版 `list_all_files`(2026-10-10),替身也得给
    monkeypatch.setattr(xt, "list_all_files", lambda *a, **k: [
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
    # ★ 代码现在走翻页版 `list_all_files`(2026-10-10),替身也得给
    monkeypatch.setattr(xt, "list_all_files", lambda *a, **k: [])
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
    session.add(XunleiGroupShare(
        user_id=1, share_id="s-dry", group_id="g", title="旧资源包",
        msg_time=datetime.now() - timedelta(days=29)))
    session.commit()
    monkeypatch.setattr(xt, "list_files", lambda *a, **k: [_folder("旧资源包", 30)])
    # ★ 代码现在走翻页版 `list_all_files`(2026-10-10),替身也得给
    monkeypatch.setattr(xt, "list_all_files", lambda *a, **k: [_folder("旧资源包", 30)])
    monkeypatch.setattr(xt, "trash_files", lambda ids: called.__setitem__("n", called["n"] + 1) or {})
    out = xc.run_cleanup(session, 1, days=7, settings=_S(), dry_run=True)
    assert out["to_delete"] == 1 and out["deleted"] == 0 and called["n"] == 0


def test_run_cleanup_moves_to_trash_and_respects_limit(session, monkeypatch) -> None:
    """真执行时:走 `trash_files`(**移入回收站**,留后悔路),并按单轮上限限流。"""
    deleted: list[str] = []
    for i, name in enumerate(("旧资源A", "旧资源B", "旧资源C")):
        session.add(XunleiGroupShare(
            user_id=1, share_id=f"s-limit-{i}", group_id="g", title=name,
            msg_time=datetime.now() - timedelta(days=29)))
    session.commit()
    monkeypatch.setattr(xt, "list_files", lambda *a, **k: [
        _folder("旧资源A", 30), _folder("旧资源B", 30), _folder("旧资源C", 30)])
    # ★ 代码现在走翻页版 `list_all_files`(2026-10-10),替身也得给
    monkeypatch.setattr(xt, "list_all_files", lambda *a, **k: [
        _folder("旧资源A", 30), _folder("旧资源B", 30), _folder("旧资源C", 30)])
    monkeypatch.setattr(xt, "trash_files",
                        lambda ids, **k: deleted.extend(ids) or {"status": "ok", "deleted": len(ids)})
    out = xc.run_cleanup(session, 1, days=7, settings=_S(), dry_run=False)
    assert out["to_delete"] == 3 and out["deleted"] == 2      # _S.max_per_run = 2
    assert out["skipped_by_limit"] == 1 and len(deleted) == 2


def test_run_cleanup_also_unlists_from_resource_library(session, monkeypatch) -> None:
    """⚠️ **删了盘上的资源,资源清单里的那一行也要删**(2026-10-03)。

    `xunlei_resources` 是 `resource_library` 的取链来源 —— 盘上删了却留着行,
    **资源库就会给出已经失效的分享链**(比"没有链"更糟:用户点了打不开)。

    ⚠️ 2026-11-14:这里**不能再造 `share_url` 是我方迅雷链的行** —— 那种行现在归
    `_shared_fids` 保护、根本不该被删(见下面 `test_run_cleanup_does_not_trash_shared_folder`)。
    这条测的是"登记过但没链"的那类(扫盘失败/链没建成),它们照旧要联动清理。
    """
    from app.db.models import XunleiResource

    session.add(XunleiResource(user_id=1, fid="id-旧资源包", name="旧资源包",
                               share_url="", synced_at=None))
    session.add(XunleiGroupShare(
        user_id=1, share_id="s-unlist", group_id="g", title="旧资源包",
        msg_time=datetime.now() - timedelta(days=29)))
    session.commit()
    monkeypatch.setattr(xt, "list_files", lambda *a, **k: [_folder("旧资源包", 30, fid="id-旧资源包")])
    # ★ 代码现在走翻页版 `list_all_files`(2026-10-10),替身也得给
    monkeypatch.setattr(xt, "list_all_files", lambda *a, **k: [_folder("旧资源包", 30, fid="id-旧资源包")])
    monkeypatch.setattr(xt, "trash_files", lambda ids, **k: {"status": "ok", "deleted": len(ids)})
    out = xc.run_cleanup(session, 1, days=7, settings=_S(), dry_run=False)
    assert out["deleted"] == 1 and out["unlisted"] == 1
    assert session.query(XunleiResource).count() == 0


def test_clean_xunlei_iterates_all_accounts(session, monkeypatch) -> None:
    """★ 2026-10-20:新加的 `xunlei#3/#4` 各有各的落地目录。

    `pan_auto_clean.clean_xunlei` 必须按账号池**逐个**调用 `run_cleanup`;
    否则新号上的资源永远不会被 3 天规则扫到。
    """
    from app.services import pan_accounts as pa
    from app.services import pan_auto_clean as pac
    from app.services import xunlei_cleanup as xcl

    class _R:
        def __init__(self, key: str) -> None:
            self.cookie_key = key

    monkeypatch.setattr(pa, "accounts", lambda *a, **k: [_R("xunlei"), _R("xunlei#3")])
    seen: list[str] = []

    def _fake_run(db, uid, days, settings, dry_run, cookie_key=""):
        seen.append(cookie_key)
        return {"to_delete": 1, "deleted": 1, "unmatched": 0,
                "skipped_by_limit": 0, "errors": []}

    monkeypatch.setattr(xcl, "run_cleanup", _fake_run)

    class _S:
        pan_auto_clean_xunlei_days = 3

    out = pac.clean_xunlei(session, 1, _S(), dry_run=False, left=0)
    assert seen == ["xunlei", "xunlei#3"], f"没有逐账号扫:{seen}"
    assert out["deleted"] == 2 and out["accounts"] == 2


def test_dry_run_does_not_touch_resource_library(session, monkeypatch) -> None:
    """预览**什么都不动** —— 包括资源清单(别把"看一眼"变成写操作)。"""
    from app.db.models import XunleiResource

    session.add(XunleiResource(user_id=1, fid="id-旧资源", name="旧资源",
                               share_url="https://pan.xunlei.com/s/DEAD"))
    session.commit()
    monkeypatch.setattr(xt, "list_files", lambda *a, **k: [_folder("旧资源", 30, fid="id-旧资源")])
    # ★ 代码现在走翻页版 `list_all_files`(2026-10-10),替身也得给
    monkeypatch.setattr(xt, "list_all_files", lambda *a, **k: [_folder("旧资源", 30, fid="id-旧资源")])
    out = xc.run_cleanup(session, 1, days=7, settings=_S(), dry_run=True)
    assert out["deleted"] == 0 and session.query(XunleiResource).count() == 1


class TestDuplicateNameDedupe:
    """**同名去重**(2026-10-04,用户:"里面我发现一些重名的文件你去删除吧")。

    迅雷转存同名时会自动加 `(1)`/`(2)`,同一份资源就躺了好几份。

    ⚠️ **两条必须守住的**:
      ① **重名 ≠ 内容相同** —— 必须真进去比子项,**比不了/不一样就一个都不删**;
      ② **保留"无后缀"那个** —— 实测:转存出来的文件夹名 = **源内容的原名**,
         而资源清单里存的是**口令**,所以"高性价比人生指南"在盘上其实叫
         「爆🔥资源包【先存!以防下架】」。**按"(N) 是副本"的直觉删,会把刚搬成功的那份一起删掉。**
    """

    def _fake_list(self, monkeypatch, tree: dict) -> None:
        from app.services import xunlei_transfer as xt
        monkeypatch.setattr(xt, "list_files", lambda fid, **k: tree.get(str(fid), []))
    # ★ 代码现在走翻页版 `list_all_files`(2026-10-10),替身也得给
        monkeypatch.setattr(xt, "list_all_files", lambda fid, **k: tree.get(str(fid), []))

    def _child(self, name: str, size: int = 100) -> dict:
        return {"name": name, "size": size, "kind": "drive#file"}

    def test_identical_contents_are_flagged(self, session, monkeypatch) -> None:
        from app.services import xunlei_cleanup as cl

        tree = {
            "P": [{"name": "甲", "id": "A", "kind": "drive#folder", "created_time": "0"},
                  {"name": "甲(1)", "id": "B", "kind": "drive#folder", "created_time": "0"}],
            "A": [self._child("x.mp4", 9)], "B": [self._child("x.mp4", 9)],
        }
        self._fake_list(monkeypatch, tree)
        out = cl.plan_duplicates(session, 1, settings=self._S())
        assert [d["id"] for d in out["dups"]] == ["B"], "该删带 (1) 的那个、保留无后缀的"
        assert out["groups"][0]["keep"] == "甲"

    def test_different_contents_are_left_alone(self, session, monkeypatch) -> None:
        """⚠️ **同名但内容不同 → 一个都不删**(宁可留着,也别误删)。"""
        from app.services import xunlei_cleanup as cl

        tree = {
            "P": [{"name": "甲", "id": "A", "kind": "drive#folder", "created_time": "0"},
                  {"name": "甲(1)", "id": "B", "kind": "drive#folder", "created_time": "0"}],
            "A": [self._child("x.mp4", 9)], "B": [self._child("完全不同的东西.zip", 77)],
        }
        self._fake_list(monkeypatch, tree)
        out = cl.plan_duplicates(session, 1, settings=self._S())
        assert out["dups"] == [] and out["groups"] == []

    def test_unreadable_group_is_skipped(self, session, monkeypatch) -> None:
        """指纹读不出来时**整个组别动** —— 比不了就不删。"""
        from app.services import xunlei_cleanup as cl
        from app.services import xunlei_transfer as xt

        tree = {"P": [{"name": "甲", "id": "A", "kind": "drive#folder", "created_time": "0"},
                      {"name": "甲(1)", "id": "B", "kind": "drive#folder", "created_time": "0"}]}

        def _flaky(fid, limit=200):
            if str(fid) == "A":
                raise RuntimeError("接口炸了")
            return tree.get(str(fid), [])

        monkeypatch.setattr(xt, "list_files", _flaky)
    # ★ 代码现在走翻页版 `list_all_files`(2026-10-10),替身也得给
        monkeypatch.setattr(xt, "list_all_files", _flaky)
        assert cl.plan_duplicates(session, 1, settings=self._S())["dups"] == []

    def test_dry_run_deletes_nothing(self, session, monkeypatch) -> None:
        from app.services import xunlei_cleanup as cl

        self._fake_list(monkeypatch, {
            "P": [{"name": "甲", "id": "A", "kind": "drive#folder", "created_time": "0"},
                  {"name": "甲(1)", "id": "B", "kind": "drive#folder", "created_time": "0"}],
            "A": [self._child("x", 1)], "B": [self._child("x", 1)]})
        called = []
        from app.services import xunlei_transfer as xt
        monkeypatch.setattr(xt, "trash_files", lambda ids: called.append(ids))
        out = cl.dedupe_duplicates(session, 1, settings=self._S(), dry_run=True)
        assert out["deleted"] == 0 and called == []

    def test_execute_trashes_and_unlists(self, session, monkeypatch) -> None:
        from app.db.models import XunleiResource
        from app.services import xunlei_cleanup as cl
        from app.services import xunlei_transfer as xt

        session.add(XunleiResource(user_id=1, fid="B", name="口令", share_url="u"))
        session.commit()
        self._fake_list(monkeypatch, {
            "P": [{"name": "甲", "id": "A", "kind": "drive#folder", "created_time": "0"},
                  {"name": "甲(1)", "id": "B", "kind": "drive#folder", "created_time": "0"}],
            "A": [self._child("x", 1)], "B": [self._child("x", 1)]})
        trashed = []
        monkeypatch.setattr(xt, "trash_files", lambda ids: trashed.extend(ids) or {"status": "ok"})
        out = cl.dedupe_duplicates(session, 1, settings=self._S(), dry_run=False)
        assert out["deleted"] == 1 and trashed == ["B"]
        assert out["unlisted"] == 1, "盘上删了要联动删清单行(否则资源库给失效链)"
        assert session.scalars(select(XunleiResource)).all() == []

    class _S:
        xunlei_transfer_parent_id = "P"


class TestDedupeTick:
    """同名去重**定时作业**(2026-10-04 用户:"做成定时作业")。

    ⚠️ **它与 `cleanup_tick` 默认值相反** —— 那个默认关(删盘难逆,先让人看清单),
    这个**默认开**:因为只删**内容可证明完全相同**的副本,且走移入回收站,风险不是一个量级。
    """

    def _S(self, **kw):
        base = {"xunlei_transfer_parent_id": "P", "xunlei_dedupe_enabled": True,
                "xunlei_dedupe_max_per_run": 30}
        base.update(kw)
        return type("S", (), base)()

    def _wire(self, session, monkeypatch, tree):
        import app.db as appdb
        from app.services import xunlei_transfer as xt
        monkeypatch.setattr(appdb, "get_session_local",
                            lambda: sessionmaker(bind=session.get_bind()))
        monkeypatch.setattr(xt, "list_files", lambda fid, **k: tree.get(str(fid), []))
    # ★ 代码现在走翻页版 `list_all_files`(2026-10-10),替身也得给
        monkeypatch.setattr(xt, "list_all_files", lambda fid, **k: tree.get(str(fid), []))
        trashed = []
        monkeypatch.setattr(xt, "trash_files", lambda ids: trashed.extend(ids) or {"status": "ok"})
        return trashed

    def _tree(self):
        return {
            "P": [{"name": "甲", "id": "A", "kind": "drive#folder", "created_time": "0"},
                  {"name": "甲(1)", "id": "B", "kind": "drive#folder", "created_time": "0"}],
            "A": [{"name": "x", "size": 1}], "B": [{"name": "x", "size": 1}],
        }

    def test_disabled_switch_returns_zero_without_recording(self, session) -> None:
        """⚠️ 关了就直接返回、**不记运行记录** —— 免得出现"success 但啥也没干"的空轮。"""
        from app.db.models import RunRecord
        from app.services import xunlei_cleanup as cl

        assert cl.dedupe_tick(self._S(xunlei_dedupe_enabled=False)) == 0
        assert session.scalars(select(RunRecord)).all() == []

    def test_enabled_deletes_and_records(self, session, monkeypatch) -> None:
        from app.db.models import RunRecord
        from app.services import xunlei_cleanup as cl

        trashed = self._wire(session, monkeypatch, self._tree())
        assert cl.dedupe_tick(self._S()) == 1
        assert trashed == ["B"]
        row = session.scalars(select(RunRecord).where(RunRecord.kind == "xunlei_dedupe")).first()
        assert row is not None and row.status == "success"
        assert "移入回收站1" in row.detail

    def test_respects_per_run_limit(self, session, monkeypatch) -> None:
        """`trash_files` 没有批量接口、逐个删 —— 必须限流,别一次打爆。"""
        from app.services import xunlei_cleanup as cl

        tree = {"P": [{"name": "甲", "id": "A", "kind": "drive#folder", "created_time": "0"}]}
        for i in range(1, 5):
            tree["P"].append({"name": f"甲({i})", "id": f"B{i}", "kind": "drive#folder",
                              "created_time": "0"})
            tree[f"B{i}"] = [{"name": "x", "size": 1}]
        tree["A"] = [{"name": "x", "size": 1}]
        trashed = self._wire(session, monkeypatch, tree)
        cl.dedupe_tick(self._S(xunlei_dedupe_max_per_run=2))
        assert len(trashed) == 2, f"该被限到 2 个:{trashed}"


# ------------------------------------ 重名后缀的两种位置(2026-10-04)

def test_suffix_regex_catches_the_mid_name_form() -> None:
    """⚠️ 迅雷把 `(N)` 插在**最后一个点之前**(`…2.0版` → `…2(1).0版`),只认结尾会**永远漏掉这一类**。

    实测:`最全文件` 下真有一对 `手机警报器（警笛模拟器）2(1).0版` / `…2.0版`,
    而周日的去重作业一直没碰过它们 —— 因为旧正则只匹配**结尾**的 `(N)`。
    """
    assert xc._SUFFIX_RE.sub("", "手机警报器（警笛模拟器）2(1).0版").strip() == \
        "手机警报器（警笛模拟器）2.0版"
    assert xc._SUFFIX_RE.sub("", "资源包 (1)").strip() == "资源包"
    assert xc._SUFFIX_RE.sub("", "资源包（2）").strip() == "资源包"
    # ⚠️ 正常名字**不许误伤**(前瞻: `(N)` 后面只能只剩最后一段、或直接结束)
    assert xc._SUFFIX_RE.sub("", "套装(1)张").strip() == "套装(1)张"
    assert xc._SUFFIX_RE.sub("", "第2.0版教程").strip() == "第2.0版教程"


# ------------------------------------ 我方已发分享链的 fid 永不删(2026-11-14)
#
# ★ 事故:**清理作业按「3 天没人再提」删文件夹,但它不知道那个文件夹
#   正被我们自己的分享链指着** —— 删掉 = 已发出的链立刻变成"已失效"。
#   实测 `xunlei_resources` 12/12 源文件 `file_not_found`。
#   夸克那边从 2026-10-20 就有 `KEEP_SHARED`,迅雷这边一直缺这道边界。

def test_shared_fids_reads_both_tables(session) -> None:
    """⚠️ 两张表都要查 —— 实测 `xunlei_group_shares.fid` 与 `xunlei_resources.fid`
    的交集只有 **45** 条,只查一张会漏掉绝大多数。"""
    from app.db.models import XunleiResource

    session.add(XunleiGroupShare(user_id=1, share_id="s", group_id="g", title="t",
                                 fid="F-group", our_url="https://pan.xunlei.com/s/AAA"))
    session.add(XunleiGroupShare(user_id=1, share_id="s2", group_id="g", title="t2",
                                 fid="F-quark", our_url="https://pan.quark.cn/s/BBB"))
    session.add(XunleiGroupShare(user_id=1, share_id="s3", group_id="g", title="t3",
                                 fid="F-nourl", our_url=""))
    session.add(XunleiResource(user_id=1, fid="F-res", name="n",
                               share_url="https://pan.xunlei.com/s/CCC"))
    session.add(XunleiResource(user_id=1, fid="F-res-nolink", name="n2", share_url=""))
    session.commit()

    assert xc._shared_fids(session, 1) == {"F-group", "F-res"}


def test_plan_never_marks_shared_folder_stale(session, monkeypatch) -> None:
    """★★ **核心边界**:匹配到监控源、闲置 30 天,但**我方分享链指着它** ⇒ 不判过期。

    没有这一条,每天 36~81 个文件夹被删,其中就包括正被发出去的链。
    """
    from app.db.models import XunleiResource

    session.add(XunleiGroupShare(user_id=1, share_id="s-old", group_id="g",
                                 title="亚麻壁纸",
                                 msg_time=datetime.now() - timedelta(days=29)))
    session.add(XunleiResource(user_id=1, fid="id-亚麻壁纸", name="亚麻壁纸",
                               share_url="https://pan.xunlei.com/s/LIVE"))
    session.commit()
    monkeypatch.setattr(xt, "list_all_files",
                        lambda *a, **k: [_folder("亚麻壁纸", 30, fid="id-亚麻壁纸")])

    p = xc.plan(session, 1, days=7, settings=_S())
    assert p["stale"] == 0, "我方分享链指着的文件夹被判过期 = 自己把已发出去的链打死"
    assert p["kept_shared"] == 1
    assert p["folders"][0]["shared"] is True


def test_run_cleanup_does_not_trash_shared_folder(session, monkeypatch) -> None:
    """⚠️ 执行清单要**再排一次** —— 只靠 `plan` 里的计数,保护的条目照样被删。"""
    from app.db.models import XunleiResource

    session.add(XunleiGroupShare(user_id=1, share_id="s-old", group_id="g",
                                 title="旧资源包",
                                 msg_time=datetime.now() - timedelta(days=29)))
    session.add(XunleiResource(user_id=1, fid="id-旧资源包", name="旧资源包",
                               share_url="https://pan.xunlei.com/s/LIVE"))
    session.commit()
    monkeypatch.setattr(xt, "list_all_files",
                        lambda *a, **k: [_folder("旧资源包", 30, fid="id-旧资源包")])
    trashed: list[str] = []
    monkeypatch.setattr(xt, "trash_files",
                        lambda ids, **k: trashed.extend(ids) or {"status": "ok"})
    out = xc.run_cleanup(session, 1, days=7, settings=_S(), dry_run=False)
    assert out["to_delete"] == 0 and out["deleted"] == 0 and trashed == []


def test_dedupe_never_trashes_shared_copy(session, monkeypatch) -> None:
    """同名去重留"无后缀"的那份 —— **万一分出去的是带 `(1)` 的那份**,按后缀删就把活链删了。"""
    from app.db.models import XunleiResource

    session.add(XunleiResource(user_id=1, fid="B", name="甲",
                               share_url="https://pan.xunlei.com/s/LIVE"))
    session.commit()
    tree = {
        "P": [{"name": "甲", "id": "A", "kind": "drive#folder", "created_time": "0"},
              {"name": "甲(1)", "id": "B", "kind": "drive#folder", "created_time": "0"}],
        "A": [{"name": "x", "size": 9}], "B": [{"name": "x", "size": 9}],
    }
    monkeypatch.setattr(xt, "list_all_files", lambda fid, **k: tree.get(str(fid), []))

    class _S:
        xunlei_transfer_parent_id = "P"

    out = xc.plan_duplicates(session, 1, settings=_S())
    assert out["dups"] == [], "正被分享的那份不该进删除清单"
    assert out["groups"][0]["skipped_shared"] == 1


# ------------------------------------ 链接有效期:永久(2026-11-14)

def test_share_expiration_defaults_to_permanent() -> None:
    """★★ 事故本体:原来两处建链都写死 `"7"` ⇒ 每条迅雷链 7 天后必然失效。

    实测同一文件夹: `"7"` → `expiration_at=2026-11-21`;`"-1"` → `expiration_at="-1"`(永久)。
    ⚠️ 回退值**绝不能是正数** —— 那正是这次事故的口径。
    """
    class _Empty:
        pass

    assert xt.SHARE_EXPIRATION_DAYS == "-1"
    assert xt.share_expiration_days(_Empty()) == "-1"


def test_share_expiration_can_be_overridden() -> None:
    class _S:
        xunlei_share_expiration_days = "365"

    assert xt.share_expiration_days(_S()) == "365"
