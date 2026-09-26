"""app/db/tx.py 保存点语义单测:兜底写失败只能撤销自己那段,不得牵连调用方数据。

背景(2026-09-26 第八轮审计):项目里大量 `except Exception: session.rollback()`
式兜底,撤销的其实是**整个外层事务**——监听轮第一条 commit 在收尾,
中途一次告警失败就能把本轮已采的新文全部抹掉。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db import models  # noqa: F401
from app.db.models import FeishuAlert, User
from app.db.tx import HeldSavepoint, savepoint


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(User(id=1, username="a", email="a@b.c", password_hash="x", role="admin"))
    db.commit()
    yield db
    db.close()


def _sections(db) -> set[str]:
    db.expire_all()
    return {r.section for r in db.scalars(select(FeishuAlert)).all()}


def test_savepoint_discards_only_its_own_block(session) -> None:
    session.add(FeishuAlert(user_id=1, section="caller", title="t1", reason="r"))
    session.flush()
    with pytest.raises(RuntimeError):
        with savepoint(session):
            session.add(FeishuAlert(user_id=1, section="doomed", title="t2", reason="r"))
            raise RuntimeError("块内炸了")
    session.commit()
    assert _sections(session) == {"caller"}


def test_savepoint_survives_flush_conflict(session) -> None:
    """块内 flush 撞唯一约束(并发双门):撤销自己那一行,外层事务仍可用。"""
    session.add(FeishuAlert(user_id=1, section="gate", title="dup", reason="first"))
    session.commit()
    session.add(FeishuAlert(user_id=1, section="caller", title="keep", reason="r"))  # 未提交
    with pytest.raises(IntegrityError):
        with savepoint(session):
            session.add(FeishuAlert(user_id=1, section="gate", title="dup", reason="second"))
            session.flush()
    session.commit()
    assert _sections(session) == {"gate", "caller"}


def test_savepoint_tolerates_outer_commit_inside_block(session) -> None:
    """块内发生外层 commit(告警成功路径要落冷却门)→ 保存点已随之关闭,收尾不得报错。"""
    with savepoint(session):
        session.add(FeishuAlert(user_id=1, section="gate", title="t", reason="r"))
        session.commit()
    assert _sections(session) == {"gate"}


def test_held_savepoint_keep_and_discard(session) -> None:
    session.add(FeishuAlert(user_id=1, section="caller", title="c", reason="r"))  # 未 commit
    held = HeldSavepoint(session)
    session.add(FeishuAlert(user_id=1, section="gate", title="g", reason="r"))
    held.close(False)          # 发送失败:只撤冷却行
    session.commit()
    assert _sections(session) == {"caller"}

    again = HeldSavepoint(session)
    session.add(FeishuAlert(user_id=1, section="gate2", title="g2", reason="r"))
    again.close(True)          # 发送成功:冷却行留下,随调用方 commit 落库
    session.commit()
    assert _sections(session) == {"caller", "gate2"}
