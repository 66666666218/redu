"""迅雷盘同步单测(2026-10-02):资源包判定、跨源去重、无凭据降级。

这个模块**每 30 分钟真跑一次**(scheduler),之前一直没有测试覆盖 —— 补上。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db import models  # noqa: F401
from app.db.models import User, XunleiGroupShare, XunleiResource
from app.services import xunlei_sync as xs


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    db.add(User(id=1, username="admin", email="a@b.c", password_hash="x", enabled=True))
    db.commit()
    yield db
    db.close()


class _FakeXt:
    """假迅雷客户端:给一棵树,记下开了哪些分享。"""

    def __init__(self, tree: dict[str, list[dict]]):
        self.tree = tree
        self.shared: list[str] = []
        self._cred = {"access_token": "a"}

    def _credentials(self, settings=None):
        return self._cred

    def list_files(self, parent_id="", **kw):
        return self.tree.get(parent_id, [])

    def share_files(self, file_ids, **kw):
        self.shared += list(file_ids)
        return {"status": "ok", "share_url": f"https://pan.xunlei.com/s/{file_ids[0]}?pwd=x",
                "code": "x"}


def _folder(fid, name):
    return {"id": fid, "name": name, "kind": "drive#folder"}


def _file(fid, name):
    return {"id": fid, "name": name, "kind": "drive#file"}


def test_collect_registers_folder_with_files_as_one_pack(session) -> None:
    """文件夹里**含文件** → 整包登记为一个资源(分享链指向它,下载者拿到的是一整套)。"""
    xt = _FakeXt({"": [_folder("P1", "右右玩软件")],
                  "P1": [_file("F1", "安装包.apk"), _file("F2", "教程.mp4")]})
    new: list[dict] = []
    xs._collect_resources(xt, session, 1, "", 0, set(), new)
    assert xt.shared == ["P1"]
    assert [n["name"] for n in new] == ["右右玩软件"]


def test_collect_descends_pure_folder_and_container(session) -> None:
    """**纯文件夹**(里面只有文件夹)与**分类目录**(名字含"转存/资源/文件"…)都继续下钻,
    不登记自己 —— 否则会把一整个分类目录当成一个资源包推出去。"""
    xt = _FakeXt({"": [_folder("C1", "我的转存"), _folder("P2", "白泽的梦")],
                  "C1": [_folder("P3", "蓝河工具箱")],      # 纯文件夹 → 下钻
                  "P2": [_folder("P4", "里层")],            # 纯文件夹 → 下钻
                  "P3": [_file("F1", "a.zip")], "P4": [_file("F2", "b.zip")]})
    new: list[dict] = []
    xs._collect_resources(xt, session, 1, "", 0, set(), new)
    assert sorted(xt.shared) == ["P3", "P4"]               # 登记的是里面的真资源
    assert sorted(n["name"] for n in new) == ["蓝河工具箱", "里层"]


def test_collect_skips_empty_dirs_system_dirs_and_known(session) -> None:
    """空目录不算资源;迅雷自带系统目录跳过;`known` 里的 fid 不再重复登记。"""
    xt = _FakeXt({"": [_folder("E1", "空目录"), _folder("S1", "超级保险箱"),
                       _folder("K1", "已知资源")],
                  "E1": [], "S1": [_file("F9", "x")], "K1": [_file("F8", "y")]})
    new: list[dict] = []
    xs._collect_resources(xt, session, 1, "", 0, {"K1"}, new)
    assert xt.shared == [] and new == []


def test_sync_dedupes_against_group_collector(session, monkeypatch) -> None:
    """⚠️ **跨源去重**:群采集/口令解析转存进来的资源已经在 `xunlei_group_shares` 里,
    扫盘**必须按 fid 跳过** —— 否则资源库重复展示、还给同一份资源再开一条分享链。

    (实测两表 fid 交集曾有 10 条。)
    """
    session.add(XunleiGroupShare(user_id=1, group_id="g", share_id="s1", title="白泽的梦",
                                 fid="P2", status="ok"))
    session.add(XunleiResource(user_id=1, fid="P1", name="扫盘登记过的"))
    session.commit()
    seen: dict = {}

    def fake_collect(xt, s, u, pid, depth, known, new, pn=""):
        seen["known"] = set(known)          # 只关心"哪些 fid 被当成已知"

    import app.services.xunlei_transfer as xt_mod
    monkeypatch.setattr(xt_mod, "_credentials", staticmethod(lambda settings=None: {"a": 1}))
    monkeypatch.setattr(xt_mod, "list_files",
                        staticmethod(lambda parent_id="", **kw: [_folder("P1", "x")]))
    monkeypatch.setattr(xs, "_collect_resources", fake_collect)

    xs.sync_xunlei_resources(session, 1)
    assert "P2" in seen["known"], "群采集登记过的 fid 没进 known —— 会被重复登记"
    assert "P1" in seen["known"], "扫盘自己登记过的 fid 也要认"


def test_sync_without_cred_is_noop(session, monkeypatch) -> None:
    """没凭据 → 明确返回 no_cred,不抛。"""
    import app.services.xunlei_transfer as xt_mod

    monkeypatch.setattr(xt_mod, "_credentials", staticmethod(lambda settings=None: {}))
    assert xs.sync_xunlei_resources(session, 1)["status"] == "no_cred"
