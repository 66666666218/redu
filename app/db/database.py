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
        url = get_settings().database_url
        engine_kwargs = dict(
            pool_pre_ping=True,
            pool_recycle=3600,
            pool_size=25,       # ≥ 调度线程池 24(每线程可各持一个长事务 session)
            max_overflow=10,    # 突发溢出(API 并发峰值)
            future=True,
        )
        if url.startswith("sqlite"):
            # SQLite 本地部署:开启 WAL(读写不互斥)+ busy_timeout(写锁冲突时等待
            # 而非立即抛 database is locked——白天高峰 collect_tick 连续 4 分钟
            # 撞锁的 2026-09-19 实战)。MySQL 无此问题(行锁)。
            from sqlalchemy import event

            engine_kwargs["connect_args"] = {"timeout": 30}  # sqlite3 busy_timeout 秒
            _engine = create_engine(url, **engine_kwargs)

            @event.listens_for(_engine, "connect")
            def _sqlite_pragma(dbapi_conn, _record):
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA busy_timeout=30000")
                cur.execute("PRAGMA synchronous=NORMAL")
                cur.close()
        else:
            _engine = create_engine(url, **engine_kwargs)
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
        "douhot_watch": ["section VARCHAR(16) DEFAULT 'douhot'", "filter_keyword VARCHAR(64) DEFAULT ''",
                        "date_window INTEGER"],
        "douhot_watch_snap": ["section VARCHAR(16) DEFAULT 'douhot'", "entry_title VARCHAR(255) DEFAULT ''", "trend_growth FLOAT DEFAULT 0"],
        "wechat_benchmarks": ["weread_book_id VARCHAR(64) DEFAULT ''", "biz VARCHAR(64) DEFAULT ''"],
        "wechat_candidates": ["url VARCHAR(600) DEFAULT ''",  # 收录按钮用(v2.6.0)
                              "import_tries INTEGER DEFAULT 0"],  # 自动收录失败重试计数(v2.13.0)
        "hotspot_suggestions": ["saves INTEGER DEFAULT 0", "saves_at DATETIME",
                                "platforms VARCHAR(64) DEFAULT ''",
                                "opportunity FLOAT DEFAULT 0",
                                "acted INTEGER DEFAULT 0", "acted_at DATETIME",
                                "article_id INTEGER", "reads_gain INTEGER DEFAULT 0",
                                "settled_at DATETIME", "repost_gain INTEGER DEFAULT 0",
                                "draft TEXT DEFAULT ''"],  # AI 发布文案(v2.5.0 按需生成)
        "hotspot_events": ["reappear_count INTEGER DEFAULT 0", "last_growth FLOAT"],
        "wechat_articles": ["source VARCHAR(16) DEFAULT 'manual'", "benchmark_id INTEGER",
                            "pan_types VARCHAR(128) DEFAULT ''", "pan_urls TEXT", "my_pan_urls TEXT",
                            "read_num INTEGER DEFAULT 0", "zan_num INTEGER DEFAULT 0", "looking_num INTEGER DEFAULT 0",
                            "share_num INTEGER DEFAULT 0", "collect_num INTEGER DEFAULT 0",
                            "comment_count INTEGER DEFAULT 0", "traffic_at DATETIME",
                            "sample_count INTEGER DEFAULT 0",
                            "first_read_num INTEGER DEFAULT 0",
                            "trend_flag VARCHAR(16) DEFAULT ''", "quality INTEGER DEFAULT 0",
                            # 进过飞书卡片的时间;NULL=从未推出去,由监听开头的补推扫回
                            "pushed_at DATETIME"],
    }
    added_pushed_at = False
    with get_engine().begin() as conn:
        for table, coldefs in additions.items():
            if table not in existing:
                continue
            cols = {c["name"] for c in inspector.get_columns(table)}
            for coldef in coldefs:
                col = coldef.split()[0]
                if col not in cols:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {coldef}"))
                    if table == "wechat_articles" and col == "pushed_at":
                        added_pushed_at = True
        # 加列那一次:存量行一律视为"已推过"。否则新机制上线后的第一轮,补推会把库里
        # 积压的历史文章全当成"从未推过"重新灌进员工群(刷屏事故)。代价是"上线前 24h 内
        # 真没推出去的文"这一次补不回来——一次性、有界,而从此以后新增的行都受补推保护。
        if added_pushed_at and "wechat_articles" in existing:
            conn.execute(text("UPDATE wechat_articles SET pushed_at = created_at "
                              "WHERE pushed_at IS NULL"))
        # 高频查询复合索引(文章过万后采样/去重查询需要)。
        # 注意:CREATE INDEX IF NOT EXISTS 是 SQLite 方言,MySQL 不支持,
        # 且 conn 必须先绑定再使用(早前版本引用了尚未定义的 conn,整个块被静默吞掉)。
        if "wechat_articles" in existing:
            # 重新反射:函数开头那个 inspector 缓存里没有本轮刚 ALTER 出的列,沿用会让
            # `ix_wa_user_pushed` 在首次迁移那一趟被静默跳过(得等下次启动才建上)。
            fresh = inspect(get_engine())
            cols = {c["name"] for c in fresh.get_columns("wechat_articles")}
            idx = {i["name"] for i in fresh.get_indexes("wechat_articles")}
            for name, needed in (("ix_wa_user_created", ("user_id", "created_at")),
                                 ("ix_wa_user_url", ("user_id", "url")),
                                 # 补推每轮都要扫"pushed_at IS NULL",没索引就是全表扫
                                 ("ix_wa_user_pushed", ("user_id", "pushed_at"))):
                if name in idx or not set(needed).issubset(cols):
                    continue
                conn.execute(text(f"CREATE INDEX {name} ON wechat_articles ({', '.join(needed)})"))
        # 一次性迁移:公众号监听间隔 360 → 60 分钟(逼近实时,免费源扛得住)
        if "user_schedules" in existing:
            conn.execute(text(
                "UPDATE user_schedules SET interval_minutes = 60 "
                "WHERE section = 'wechat' AND interval_minutes = 360"))
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
