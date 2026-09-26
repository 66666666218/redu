"""`_migrate()` 轻量迁移路径:给已有表补列/补索引(生产 MySQL 靠它,不写手写迁移)。

这些用例针对的是**启动时的一次性结构变更**,所以必须用真文件库 + 旧形状的表,
不能用测试里惯用的 `Base.metadata.create_all`(那直接就是新形状,测不到 ALTER)。
"""
from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from app.db import database as db_mod

OLD_ARTICLES = """
CREATE TABLE wechat_articles (
    id INTEGER PRIMARY KEY,
    user_id INTEGER,
    url VARCHAR(255),
    title VARCHAR(255),
    source VARCHAR(16) DEFAULT 'manual',
    pan_urls TEXT,
    created_at DATETIME,
    publish_at DATETIME,
    quality INTEGER DEFAULT 0
)
"""


@pytest.fixture
def legacy_db(tmp_path, monkeypatch):
    """造一个"升级前"的库:有 wechat_articles 表但没有 pushed_at 列/复合索引。"""
    path = tmp_path / "legacy.db"
    engine = create_engine(f"sqlite:///{path.as_posix()}", poolclass=NullPool, future=True)
    with engine.begin() as conn:
        conn.execute(text(OLD_ARTICLES))
        # 历史文章:上线前入库的,绝不能被当成"从未推送"补推给员工群
        conn.execute(text("INSERT INTO wechat_articles (user_id, url, title, source, created_at)"
                          " VALUES (1, 'u1', '老文', 'listen', '2026-09-26 09:00:00')"))
    # _migrate() 走 get_engine() 的模块级缓存,直接换掉它(不碰 DATABASE_URL)
    monkeypatch.setattr(db_mod, "_engine", engine)
    yield path
    engine.dispose()


def _columns(path) -> set[str]:
    con = sqlite3.connect(path)
    try:
        return {r[1] for r in con.execute("PRAGMA table_info(wechat_articles)")}
    finally:
        con.close()


def _indexes(path) -> set[str]:
    con = sqlite3.connect(path)
    try:
        return {r[1] for r in con.execute("PRAGMA index_list(wechat_articles)")}
    finally:
        con.close()


def test_migrate_adds_pushed_at_and_backfills(legacy_db) -> None:
    """`pushed_at` 补列 + **同趟**回填 `created_at`。

    回填不是可选项:新机制的判据是 `pushed_at IS NULL`,而存量文章大多确实在上线前
    就推过了。不回填的话,升级后的第一轮监听会把库里积压的历史文全当欠推灌进员工群。
    """
    assert "pushed_at" not in _columns(legacy_db)

    db_mod._migrate()

    assert "pushed_at" in _columns(legacy_db)
    con = sqlite3.connect(legacy_db)
    nulls = con.execute("select count(*) from wechat_articles where pushed_at is null").fetchone()[0]
    filled = con.execute("select pushed_at from wechat_articles where url='u1'").fetchone()[0]
    con.close()
    assert nulls == 0 and str(filled).startswith("2026-09-26")


def test_migrate_creates_pushed_index_in_same_pass(legacy_db) -> None:
    """补推每轮都要扫 `pushed_at IS NULL`,所以索引必须**首次迁移那一趟**就建上。

    回归:索引段沿用函数开头那个 inspector 的列缓存,而它不含本轮刚 ALTER 出的列,
    `issubset(cols)` 恒 False → 索引被静默跳过,要等下一次启动才建(而那一轮已经在全表扫)。
    """
    db_mod._migrate()
    assert "ix_wa_user_pushed" in _indexes(legacy_db)
    assert {"ix_wa_user_created", "ix_wa_user_url"} <= _indexes(legacy_db)


def test_migrate_is_idempotent(legacy_db) -> None:
    """重复启动不能再动结构、也不能改已定的 pushed_at(否则会把已推过的文重新变成欠推)。"""
    db_mod._migrate()
    before = _columns(legacy_db), _indexes(legacy_db)
    con = sqlite3.connect(legacy_db)
    pushed = con.execute("select pushed_at from wechat_articles where url='u1'").fetchone()[0]
    con.execute("INSERT INTO wechat_articles (user_id, url, title, source, created_at)"
                " VALUES (1, 'u2', '上线后新文', 'listen', '2026-09-26 10:00:00')")
    con.commit()
    con.close()

    db_mod._migrate()

    assert (_columns(legacy_db), _indexes(legacy_db)) == before
    con = sqlite3.connect(legacy_db)
    assert con.execute("select pushed_at from wechat_articles where url='u1'").fetchone()[0] == pushed
    # 加列之后的新行不在回填范围内:pushed_at 保持 NULL,由真的送达时业务自己盖
    assert con.execute("select pushed_at is null from wechat_articles where url='u2'").fetchone()[0] == 1
    con.close()
