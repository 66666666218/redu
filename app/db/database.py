"""数据库连接与会话(MySQL / SQLAlchemy 2.0)。

- `Base`:声明式基类(见 models.py)。
- `get_db()`:FastAPI 依赖注入的会话。
- `init_db()`:建表。
"""
from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from config.settings import get_settings


class Base(DeclarativeBase):
    """ORM 基类。"""


_engine = None
_SessionLocal: sessionmaker[Session] | None = None


def get_engine():
    """延迟创建并缓存 SQLAlchemy 引擎。"""
    global _engine
    if _engine is None:
        _engine = create_engine(
            get_settings().database_url,
            pool_pre_ping=True,
            pool_recycle=3600,
            future=True,
        )
    return _engine


def get_session_local() -> sessionmaker[Session]:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), autoflush=False, autocommit=False, expire_on_commit=False)
    return _SessionLocal


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖:每个请求一个会话,结束后关闭。"""
    db = get_session_local()()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """按当前 metadata 建表(幂等)。连不上库时仅告警,不阻断(服务器上 MySQL 就绪即成功)。"""
    from app.db import models  # noqa: F401  确保模型已注册

    try:
        Base.metadata.create_all(bind=get_engine())
        _migrate()
    except Exception as exc:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).warning("数据库建表失败(请确认 DATABASE_URL 与 MySQL 已就绪):%s", exc)


def _migrate() -> None:
    """轻量迁移:为用户表补充 email/reset_token/reset_expires 列(旧库)。"""
    from sqlalchemy import inspect, text

    inspector = inspect(get_engine())
    if "users" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("users")}
    additions = ["email VARCHAR(128)", "reset_token VARCHAR(128)", "reset_expires DATETIME"]
    with get_engine().begin() as conn:
        for coldef in additions:
            col = coldef.split()[0]
            if col not in cols:
                conn.execute(text(f"ALTER TABLE users ADD COLUMN {coldef}"))
