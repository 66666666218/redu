"""保留期清理的判据(2026-10-20,用户口径)。

规则:**3 天无人重发才删;只删匹配到监控源的;我方分享链默认保护;百度默认只报告。**
"""
import os
from datetime import datetime, timedelta

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import models  # noqa: F401
from app.db.database import Base
from app.db.models import User, XunleiGroupShare
from app.services import pan_retention as pr


class _S:
    quark_fid_store = ""
    pan_intro_quark_dir = "/监听宣传"
    pan_auto_clean_baidu_root = "/root"
    pan_intro_baidu_dir = ""


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    db.add(User(id=1, username="u1", email="u1@b.c", password_hash="x", enabled=True))
    db.commit()
    yield db
    db.close()


def _old(days: int) -> datetime:
    return datetime.now() - timedelta(days=days)


def _source(session, title: str, days: int, sid: str = "s1") -> None:
    session.add(XunleiGroupShare(user_id=1, share_id=sid, group_id="g",
                                 title=title, msg_time=_old(days)))
    session.commit()


class _FakeQuark:
    items: dict[str, list] = {}
    deleted: list[str] = []

    def __init__(self, *a, **k):
        pass

    def resolve_named_dir(self, path):
        return ""

    def list_dir(self, fid):
        return self.items.get(fid, [])

    def delete_files(self, fids, *, to_recycle=True):
        self.deleted.extend(fids)
        #: 真实形状是 `{"ok","message","count"}`(2026-10-20 第一次真删踩到)
        return {"ok": True, "message": "已移入回收站", "count": len(fids)}


class _FakeBaidu:
    items: list | dict = []
    deleted: list[str] = []

    def __init__(self, *a, **k):
        pass

    def list_dir(self, path):
        if isinstance(self.items, dict):
            return self.items.get(path, [])
        return self.items

    def delete_paths(self, paths, **k):
        self.deleted.extend(paths)
        return len(paths)


def _quark_entry(name: str, fid: str, days: int = 10) -> dict:
    return {"file_name": name, "fid": fid, "dir": True,
            "updated_at": int(_old(days).timestamp() * 1000)}


def _patch_quark(monkeypatch, items, protected=None):
    from app.services import quark_dup

    _FakeQuark.items = {"home": items}
    _FakeQuark.deleted = []
    monkeypatch.setattr("app.services.pan_accounts.cookie_for", lambda *a, **k: "ck")
    monkeypatch.setattr("app.services.quark_transfer.QuarkTransfer", _FakeQuark)
    monkeypatch.setattr(quark_dup, "list_homes",
                        lambda qt, base="redian监听": [{"file_name": "redian监听_1020",
                                                        "fid": "home"}])
    monkeypatch.setattr(quark_dup, "refresh_protect",
                        lambda qt, dirs=None, settings=None: (
                            set(protected or []), set(), 0, []))


class TestQuark:
    def test_只删监控到且超期的_未匹配永不删(self, session, monkeypatch) -> None:
        _source(session, "旧资源A", 5)
        _patch_quark(monkeypatch, [_quark_entry("旧资源A", "q1"),
                                   _quark_entry("完全没监控过", "q2")])
        p = pr.plan_quark(session, 1, _S(), days=3, keep_shared=False)
        assert p["stale"] == 1 and p["unmatched"] == 1
        rows = {r["fid"]: r for r in p["rows"]}
        assert rows["q1"]["stale"] is True and rows["q2"]["stale"] is False

    def test_共享链保护生效(self, session, monkeypatch) -> None:
        _source(session, "旧资源A", 5)
        _patch_quark(monkeypatch, [_quark_entry("旧资源A", "q1")], protected={"q1"})
        p = pr.plan_quark(session, 1, _S(), days=3, keep_shared=True)
        assert p["stale"] == 0 and p["protected"] == 1

    def test_dry_run_不删(self, session, monkeypatch) -> None:
        _source(session, "旧资源A", 5)
        _patch_quark(monkeypatch, [_quark_entry("旧资源A", "q1")])
        out = pr.run_quark(session, 1, _S(), days=3, dry_run=True, keep_shared=False)
        assert out["planned"] == 1 and out["deleted"] == 0 and _FakeQuark.deleted == []

    def test_真删走回收站(self, session, monkeypatch) -> None:
        _source(session, "旧资源A", 5)
        _patch_quark(monkeypatch, [_quark_entry("旧资源A", "q1")])
        out = pr.run_quark(session, 1, _S(), days=3, dry_run=False, keep_shared=False)
        assert out["deleted"] == 1 and _FakeQuark.deleted == ["q1"]


class TestBaidu:
    def _patch(self, monkeypatch, items):
        _FakeBaidu.items = items
        _FakeBaidu.deleted = []
        monkeypatch.setattr("app.services.pan_accounts.cookie_for", lambda *a, **k: "ck")
        monkeypatch.setattr("app.services.baidupan_transfer.BaiduPanClient", _FakeBaidu)

    def test_批次目录只下钻不删(self, session, monkeypatch) -> None:
        """★ 2026-10-20 干跑抓到:百度顶层是 `来自资源批量转存_*` 批次目录。

        直接拿批次名匹配会**误把整批当成一个资源** ⇒ 批次目录只下钻,资源是里面的条目。
        """
        _source(session, "旧资源A", 5)
        self._patch(monkeypatch, {
            "/root": [{"server_filename": "来自资源批量转存_20261009_143619",
                       "path": "/root/batch", "isdir": True}],
            "/root/batch": [{"server_filename": "旧资源A",
                             "path": "/root/batch/旧资源A", "isdir": True,
                             "server_mtime": int(_old(10).timestamp())}],
        })
        p = pr.plan_baidu(session, 1, _S(), days=3)
        assert p["stale"] == 1
        assert p["rows"][0]["fid"] == "/root/batch/旧资源A", \
            "不能把批次目录本身当资源"

    def test_默认只报告不删(self, session, monkeypatch) -> None:
        _source(session, "旧资源A", 5)
        self._patch(monkeypatch, [{"server_filename": "旧资源A",
                                   "path": "/root/旧资源A", "isdir": True,
                                   "server_mtime": int(_old(10).timestamp())}])
        out = pr.run_baidu(session, 1, _S(), days=3, dry_run=False,
                           allow_delete=False)
        assert out["planned"] == 1 and out["deleted"] == 0
        assert out["dry_run"] is True and _FakeBaidu.deleted == []

    def test_开关打开才删(self, session, monkeypatch) -> None:
        _source(session, "旧资源A", 5)
        self._patch(monkeypatch, [{"server_filename": "旧资源A",
                                   "path": "/root/旧资源A", "isdir": True,
                                   "server_mtime": int(_old(10).timestamp())}])
        out = pr.run_baidu(session, 1, _S(), days=3, dry_run=False,
                           allow_delete=True)
        assert out["deleted"] == 1 and _FakeBaidu.deleted == ["/root/旧资源A"]

    def test_我方已发链的资源不删(self, session, monkeypatch) -> None:
        """★★★ 2026-11-20 修:`keep_shared` 在百度这半边此前是**假的**。

        `plan_baidu` 建行时硬编码 `protected=False`,参数收进来就被丢掉 ⇒
        删了 = 已发出的客户链接立刻失效,而百度**没有回收站**(删了捞不回来)。
        判据用**标题里的《书名号》**(百度分享链反查不到盘内路径,只能按名字护)。
        """
        from app.db.models import DiscoveredPanLink

        _source(session, "旧资源A", 5)
        _source(session, "旧资源B", 5, sid="s2")
        session.add(DiscoveredPanLink(
            user_id=1, platform="weibo", origin_url="https://pan.baidu.com/s/x",
            title="《旧资源A》更至第 8 集", our_url="https://pan.baidu.com/s/1abc"))
        session.commit()
        self._patch(monkeypatch, [
            {"server_filename": "旧资源A", "path": "/root/旧资源A", "isdir": True,
             "server_mtime": int(_old(10).timestamp())},
            {"server_filename": "旧资源B", "path": "/root/旧资源B", "isdir": True,
             "server_mtime": int(_old(10).timestamp())},
        ])
        p = pr.plan_baidu(session, 1, _S(), days=3)
        by = {r["name"]: r for r in p["rows"]}
        assert by["旧资源A"]["protected"] is True, "我方已发链的资源必须被保护"
        assert by["旧资源A"]["stale"] is False
        assert by["旧资源B"]["stale"] is True, "没发过链的照常过期"
        assert p["protected"] == 1

        #: 真跑一遍:只删 B
        out = pr.run_baidu(session, 1, _S(), days=3, dry_run=False, allow_delete=True)
        assert out["deleted"] == 1 and _FakeBaidu.deleted == ["/root/旧资源B"]

    def test_保护名单为空时整轮不删(self, session, monkeypatch) -> None:
        """⚠️ 拿不到我方链的名字清单就**不猜** —— 百度删了不可逆,宁可这轮白跑。

        判据:有我方百度链,但从标题里一条书名号名字都没解析出来 ⇒ 返回错误、不删。
        """
        from app.db.models import DiscoveredPanLink

        _source(session, "旧资源A", 5)
        session.add(DiscoveredPanLink(
            user_id=1, platform="weibo", origin_url="https://pan.baidu.com/s/x",
            title="没有书名号的标题", our_url="https://pan.baidu.com/s/1abc"))
        session.commit()
        self._patch(monkeypatch, [{"server_filename": "旧资源A",
                                   "path": "/root/旧资源A", "isdir": True,
                                   "server_mtime": int(_old(10).timestamp())}])
        out = pr.run_baidu(session, 1, _S(), days=3, dry_run=False, allow_delete=True)
        assert out["deleted"] == 0 and _FakeBaidu.deleted == []
        assert "保护名单为空" in str(out.get("error") or ""), out
