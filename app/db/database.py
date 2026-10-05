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
from app.utils import get_logger

logger = get_logger(__name__)


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


# 迁移表:**要补的列**(提成模块级,好让"修远程冻住的库"复用同一份清单)。
# ⚠️ 每条形如 `列名 类型 [DEFAULT ...]` —— **第一个词是列名,不是类型**。
# ⚠️ TEXT 类的 `DEFAULT` 在 MySQL 上不合法(报 1101),由 `_coldef_for_mysql` 摘掉并补空串。
ADDITIONS: dict[str, list[str]] = {
    "users": [
        "email VARCHAR(128)", "role VARCHAR(16) DEFAULT 'user'", "enabled INTEGER DEFAULT 1",
        "smtp_host VARCHAR(128)", "smtp_port INTEGER", "smtp_user VARCHAR(128)",
        "smtp_pass VARCHAR(255)", "smtp_from VARCHAR(128)", "reset_token VARCHAR(128)", "reset_expires DATETIME",
    ],
    "runs": ["retry_count INTEGER DEFAULT 0"],
    # 作业**注册时刻**(2026-10-05):原来只在"执行后"才有心跳行,于是"注册了但还没到第一次
    # 执行点"的作业对账时无法与"真·从没跑过"区分(chain_report 当天就被误报过一次)。
    "job_heartbeats": ["first_seen_at DATETIME"],
    # 闲鱼行情三件套(2026-10-03):want_count/sold_price/tags 全部出自**搜索响应**,
    # 不必打详情接口 → 需求热度绕开滑块验证。存量行 want_count=0 = "还没用新解析重采过"。
    "xianyu_items": ["want_count INTEGER DEFAULT 0", "sold_price VARCHAR(32) DEFAULT ''",
                     "tags VARCHAR(255) DEFAULT ''"],
    # source 区分「搜索免费行情」与「详情深采」:同日同商品一行,靠它判优先级不被覆盖。
    # 存量行一律标 detail —— 历史行确实都是深采写的,标 search 会让深采跳过它们、永不补全。
    "xianyu_daily": ["source VARCHAR(16) DEFAULT 'detail'", "tags VARCHAR(255) DEFAULT ''"],
    # 拉新周录的**分渠道明细**(2026-10-03 用户口径:"我只能给你我的"且要分渠道):
    # JSON 如 {"douyin": 42, "wechat": 18}。只有分开录,才能分别对账两条链。
    # ⚠️ **就是这条把远程库冻住的**:`TEXT DEFAULT ''` 在 MySQL 报 1101,而它后面还有 8 张表。
    "pan_recruit_weekly": ["channels TEXT DEFAULT ''"],
    # 线索搬成了哪条链(2026-10-04 补,计划第 12 项):原来落库时**丢了**,
    # 于是"这个口令到底搬没搬成"事后查不出来。存量行留空 = "还没用新版重采过"。
    "douyin_leads": ["our_url VARCHAR(500) DEFAULT ''"],
    "alerts": ["section VARCHAR(32) DEFAULT ''"],
    "douhot_watch": ["section VARCHAR(16) DEFAULT 'douhot'", "filter_keyword VARCHAR(64) DEFAULT ''",
                     "date_window INTEGER"],
    "douhot_watch_snap": ["section VARCHAR(16) DEFAULT 'douhot'", "entry_title VARCHAR(255) DEFAULT ''",
                          "trend_growth FLOAT DEFAULT 0"],
    "wechat_benchmarks": ["weread_book_id VARCHAR(64) DEFAULT ''", "biz VARCHAR(64) DEFAULT ''"],
    "wechat_candidates": ["url VARCHAR(600) DEFAULT ''",  # 收录按钮用(v2.6.0)
                          "import_tries INTEGER DEFAULT 0"],  # 自动收录失败重试计数(v2.13.0)
    "hotspot_suggestions": ["saves INTEGER DEFAULT 0", "saves_at DATETIME",
                            "platforms VARCHAR(64) DEFAULT ''",
                            "category VARCHAR(16) DEFAULT ''",   # 验证品类(结算归因聚合键,2026-10-01)
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


def _coldef_for_mysql(coldef: str) -> tuple[str, bool]:
    """把列定义改成 **MySQL 也吃得下**的形态。返回 `(列定义, 是否要把存量行补成空串)`。

    ⚠️ **为什么必须做**(2026-10-05 生产定位到根因):
    **MySQL 不允许 `TEXT`/`BLOB`/`JSON` 列带 `DEFAULT`**(报 `1101`),而本地 SQLite 允许。
    于是 `channels TEXT DEFAULT ''` 这条**在远程库上必失败** —— 而失败一列会把它**后面
    所有列**一起挡掉(原实现共用一个事务),**远程库就这样静默冻住**。

    这里把 TEXT 类的 `DEFAULT ...` 摘掉,并让调用方加完列后
    `UPDATE ... SET col='' WHERE col IS NULL` —— **语义与 `DEFAULT ''` 等价**
    (代码本来都按 `or ""` 读)。非 TEXT 类(VARCHAR/INTEGER/DATETIME)**原样返回**。
    """
    parts = coldef.split()
    # ⚠️ `coldef` 是 **`列名 类型 [DEFAULT ...]`**(如 `channels TEXT DEFAULT ''`),
    # **第一个词是列名不是类型** —— 我第一版就按第一个词判,结果全部原样返回、等于没改
    # (测试当场打出来才看见)。
    typ = parts[1].upper() if len(parts) > 1 else ""
    typ = typ.split("(")[0]        # `VARCHAR(500)` → `VARCHAR`
    if typ in ("TEXT", "LONGTEXT", "MEDIUMTEXT", "TINYTEXT", "BLOB", "JSON"):
        if " DEFAULT " in coldef:
            return coldef.split(" DEFAULT ")[0].strip(), True
    return coldef, False


def _migrate() -> None:
    """轻量迁移:为已存在的表补充缺失列(兼容旧库)。"""
    from sqlalchemy import inspect, text

    inspector = inspect(get_engine())
    existing = set(inspector.get_table_names())
    added_pushed_at = False
    # ⚠️⚠️ **每列一个事务 + 逐列吞错**(2026-10-05 生产定位):
    # 原来所有列共用一个 `with get_engine().begin()`,**一列失败就整段抛出** ⇒
    # 它**后面所有列全都没加上**,而且只在启动时打一条 ERROR —— **远程库就这样静默冻住了**。
    # 实测(远程 MySQL):`pan_recruit_weekly.channels TEXT DEFAULT ''` 报
    # `(1101, "BLOB, TEXT, GEOMETRY or JSON column 'channels' can't have a default value")`
    # ⇒ 它之后的 `hotspot_suggestions.draft` 等**全部没上**,于是任何全字段 SELECT 都
    # `Unknown column 'draft'`(远程的选题 Agent 一碰就炸)。
    for table, coldefs in ADDITIONS.items():
        if table not in existing:
            continue
        cols = {c["name"] for c in inspector.get_columns(table)}
        for coldef in coldefs:
            ddl, backfill = _coldef_for_mysql(coldef)
            col = ddl.split()[0]
            if col in cols:
                continue
            try:
                with get_engine().begin() as conn:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))
                    if backfill:
                        # MySQL 不许 TEXT 带 DEFAULT ⇒ 摘掉默认值,改用它把存量行补齐,
                        # 语义与 `DEFAULT ''` 等价(代码本来就按 `or ""` 读)
                        conn.execute(text(f"UPDATE {table} SET {col} = '' WHERE {col} IS NULL"))
                if table == "wechat_articles" and col == "pushed_at":
                    added_pushed_at = True
            except Exception as exc:  # noqa: BLE001 - **单列失败绝不能挡住其余列**
                logger.warning("迁移跳过 %s.%s(其余列继续):%s",
                               table, col, str(exc)[:140])
    with get_engine().begin() as conn:
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
