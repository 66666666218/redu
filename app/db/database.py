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
    """按当前 metadata 建表(幂等)。

    连不上库时不阻断启动(等 MySQL 就绪后重启即可),但**必须把完整堆栈记成 ERROR**:
    早前只打一行 WARNING,加上生产日志未初始化,结果建表失败被静默吞掉——
    应用照常启动、`/healthz` 照常 200,一调注册就 `OperationalError`,极难定位。
    """
    from app.db import models  # noqa: F401  确保模型已注册

    try:
        Base.metadata.create_all(bind=get_engine())
        _migrate()
    except Exception:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).exception(
            "数据库建表/迁移失败,服务将无法读写数据(请检查 DATABASE_URL 与 MySQL 是否就绪);"
            "可访问 /healthz 查看数据库状态"
        )


def db_status() -> dict:
    """数据库自查:连通性 + 关键表/列是否齐备。

    供 `/healthz` 无鉴权暴露——数据库坏掉时登录接口本身也用不了,
    诊断信息若放在需要鉴权的接口后面就永远看不到。
    只返回结构信息与异常**类型**,不返回异常消息(可能含连接串/口令)。
    """
    from sqlalchemy import inspect, text

    try:
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        required = ("users", "user_cookies", "user_schedules")
        cols = {c["name"] for c in inspector.get_columns("users")} if "users" in tables else set()
        return {
            "connected": True,
            "missing_tables": [t for t in required if t not in tables],
            "users_missing_columns": [c for c in ("email", "role", "enabled") if cols and c not in cols],
        }
    except Exception as exc:  # noqa: BLE001
        return {"connected": False, "error_type": type(exc).__name__}


def _migrate() -> None:
    """轻量迁移:为已存在的表补充缺失列(兼容旧库)。"""
    from sqlalchemy import inspect, text

    inspector = inspect(get_engine())
    existing = set(inspector.get_table_names())
    additions = {
        "users": [
            "email VARCHAR(128)", "role VARCHAR(16) DEFAULT 'user'", "enabled INTEGER DEFAULT 1",
            "smtp_host VARCHAR(128)", "smtp_port INTEGER", "smtp_user VARCHAR(128)",
            "smtp_pass VARCHAR(255)", "smtp_from VARCHAR(128)", "reset_token VARCHAR(128)", "reset_expires DATETIME",
        ],
        "runs": ["retry_count INTEGER DEFAULT 0"],
        "alerts": ["section VARCHAR(32) DEFAULT ''"],
        "douhot_watch": ["section VARCHAR(16) DEFAULT 'douhot'"],
        "douhot_watch_snap": ["section VARCHAR(16) DEFAULT 'douhot'", "entry_title VARCHAR(255) DEFAULT ''"],
    }
    with get_engine().begin() as conn:
        for table, coldefs in additions.items():
            if table not in existing:
                continue
            cols = {c["name"] for c in inspector.get_columns(table)}
            for coldef in coldefs:
                col = coldef.split()[0]
                if col not in cols:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {coldef}"))
        # 去除 douhot_watch 旧的 (user_id, list_type, keyword) 唯一索引:
        # 关键词监控泛化到四个板块后,同一关键词可在多板块监控,旧约束会 UNIQUE 冲突。
        if "douhot_watch" in existing:
            dialect = conn.dialect.name
            try:
                if dialect == "mysql":
                    conn.execute(text("DROP INDEX uq_watch ON douhot_watch"))
                elif dialect == "sqlite":
                    conn.execute(text("DROP INDEX IF EXISTS uq_watch"))
            except Exception:  # noqa: BLE001 - 索引不存在/已删则忽略
                pass
