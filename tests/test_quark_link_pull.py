"""夸克链成果**回传**单测(2026-10-14)。

## 为什么有这条链
夸克口令链要签名服务,所以它在**远程**跑;而抖音卡片在**本机**生成。
`remote_sync` 是**单向**(本机→远程),远程搬成的 `our_url` 从来没回来 ——
实测**远程 236 条已搬成 vs 本地只看得到 64 条**,于是卡片上那批夸克口令
永远显示"没有链"(用户口径:「有一个解开的就用哪个解开的链接」)。

## 这里守的三条(全是"别把已知的东西弄坏")
1. **只补空位**:本地已有链 ⇒ **绝不覆盖**(本机自己搬的更贴近本机口径);
2. **只认识的行**:远程有、本地没有的行**不动**(不许顺着远程往回建行);
3. **可空跑**:远程没数据 ⇒ 一条都不写、也不提交。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.db.database import Base  # noqa: E402
from app.db import models  # noqa: E402,F401
from app.db.models import DouyinLead  # noqa: E402
from app.services import quark_link_pull as qlp  # noqa: E402


@pytest.fixture
def session():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)()
    yield db
    db.close()


def _local(session, aid: str, our: str = "", moved=None) -> DouyinLead:
    row = DouyinLead(user_id=1, aweme_id=aid, found_date="2026-10-14",
                     title="t", our_url=our, moved_at=moved)
    session.add(row)
    session.commit()
    return row


def _remote(*rows) -> object:
    """假"读远程":返回 [(aweme_id, our_url, moved_at)] 形状的 dict 列表。"""
    return lambda settings: [{"aweme_id": a, "our_url": u, "moved_at": m} for a, u, m in rows]


class TestPullOnce:
    def test_把远程的链补进本地空位(self, session) -> None:
        row = _local(session, "a1")
        out = qlp.pull_once(session, settings=None,
                            remote_rows=_remote(("a1", "https://pan.quark.cn/s/R", None)))
        assert out == {"remote": 1, "updated": 1}
        session.refresh(row)
        assert row.our_url == "https://pan.quark.cn/s/R"

    def test_本地已有链绝不覆盖(self, session) -> None:
        """★ 本机自己搬的那条更贴近本机口径 ⇒ 远程的值再"新"也不许盖。"""
        row = _local(session, "a1", our="https://pan.xunlei.com/s/LOCAL")
        out = qlp.pull_once(session, settings=None,
                            remote_rows=_remote(("a1", "https://pan.quark.cn/s/R", None)))
        assert out["updated"] == 0
        session.refresh(row)
        assert row.our_url == "https://pan.xunlei.com/s/LOCAL"

    def test_本地没有的行不动_不顺着远程建行(self, session) -> None:
        out = qlp.pull_once(session, settings=None,
                            remote_rows=_remote(("ghost", "https://pan.quark.cn/s/R", None)))
        assert out == {"remote": 1, "updated": 0}
        assert session.query(DouyinLead).count() == 0

    def test_远程空则不写不提交(self, session) -> None:
        _local(session, "a1")
        assert qlp.pull_once(session, settings=None, remote_rows=_remote()) == \
            {"remote": 0, "updated": 0}

    def test_moved_at只在本地为空时补(self, session) -> None:
        from datetime import datetime

        earlier = datetime(2026, 10, 14, 9, 0, 0)
        row = _local(session, "a1", moved=earlier)
        later = datetime(2026, 10, 14, 12, 0, 0)
        qlp.pull_once(session, settings=None,
                      remote_rows=_remote(("a1", "https://pan.quark.cn/s/R", later)))
        session.refresh(row)
        assert row.moved_at == earlier        # 本机的"首次搬成时刻"不被远程改写


class TestTickRecordsNumbers:
    def test_数字进运行记录_0也要看得见(self, session, monkeypatch) -> None:
        """⚠️ 「补上 0 条」与「这条路没接上」必须能分开看 —— 所以数字必须落 runs。"""
        from app.db.models import User

        session.add(User(id=1, username="u", email="u@b.c", password_hash="x", enabled=True))
        session.commit()
        monkeypatch.setattr("app.db.get_session_local", lambda: (lambda: session))
        monkeypatch.setattr(qlp, "pull_once", lambda s, st=None: {"remote": 3, "updated": 2})
        got = qlp.quark_link_pull_tick(settings=object())
        assert got == 2
        run = session.execute(
            __import__("sqlalchemy").text(
                "select detail from runs where kind='quark_link_pull'")).scalar()
        assert "远程已搬成3" in run and "本地补上2" in run
