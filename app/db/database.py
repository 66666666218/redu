"""数据库连接与会话(MySQL / SQLAlchemy 2.0)。

- `Base`:声明式基类(见 models.py)。
- `get_db()`:FastAPI 依赖注入的会话。
- `init_db()`:建表。
"""
from __future__ import annotations

import os
import threading
import time
import traceback
from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from config.settings import get_settings
from app.utils import get_logger

logger = get_logger(__name__)

#: **当前开着的写事务**:`id(conn) -> (起始时刻, 调用栈, 线程名)`。
#: 撞锁时靠它把"当时是谁抱着锁"直接打出来(见 `_install_write_tx_watchdog`)。
_OPEN_WRITE_TX: dict[int, tuple[float, str, str]] = {}

#: 已经就"持有太久"报过的事务 —— 同一个事务只报一次,别刷屏(告警变噪音的第一步)。
_WARNED_TX: set[int] = set()

#: 诊断自己的文件名片段 —— 打调用栈时要把它滤掉,否则栈尾永远是这个 listener。
#: 用 `os.path.join` 拼,免得在 f-string 里写反斜杠(老版本 Python 不允许)。
_SELF_FRAME = os.path.join("app", "db", "database.py")

#: 写事务开着超过这么多秒就告警。20 秒是照 `busy_timeout=30` 定的 ——
#: 别人最多等 30 秒就会以 `database is locked` 失败,所以在 20 秒时先喊,
#: 留出 10 秒让我们**在它造成失败之前**看见。
WRITE_TX_WARN_SEC = 20.0


def commit_with_lock_report(session, what: str = "", retries: int = 1) -> bool:
    """提交;撞锁时**量出等了多久**、报现场,并**重试一次**。返回是否成功。

    ⚠️⚠️ **为什么要量"等了多久"**(2026-10-09):这能一刀切开两种完全不同的真因 ——

    | 等的时间 | 含义 | 修法 |
    |---|---|---|
    | ≈ `busy_timeout`(30s) | 持有者**真抱着 30 秒** | 去把那个长持有者找出来 |
    | **几乎立刻返回** | 是 SQLite **不遵守 busy handler** 的那类锁(`BUSY_SNAPSHOT` / `-shm`) | 持有者只需抱一瞬 ⇒ **看门狗看不到它正是必然** ⇒ 修法是**重试** |

    我们一直只看到"撞锁了",**没人量过这个数** —— 于是三次都在猜"谁抱了那么久",
    而看门狗(阈值 20 秒)一次都没响,自相矛盾了一整天。

    **重试为什么是合理的、不是掩盖**:这类锁是**事务级的瞬时冲突**,换一个新事务通常就能过
    (重试后仍失败会照实往上抛)。**日志里会明写"重试成功/仍失败"**,不会把问题藏起来。
    """
    import time as _t

    _first_wait = 0.0
    for attempt in range(retries + 1):
        t0 = _t.monotonic()
        try:
            session.commit()
            if attempt:
                logger.warning("SQLite 撞锁后**重试第 %d 次提交成功**(首次等了 %.2fs)%s",
                               attempt, _first_wait, f"—— {what}" if what else "")
            return True
        except Exception as exc:  # noqa: BLE001
            dt = _t.monotonic() - t0
            if "database is locked" not in str(exc):
                raise
            if attempt == 0:
                _first_wait = dt
            verdict = ("**等了整整 %.2fs ⇒ 持有者真抱着**(去找那个长持有者)" % dt
                       if dt > 5 else
                       "**几乎立刻返回(%.2fs)⇒ 是「不遵守 busy_timeout」的那类锁,"
                       "持有者只需抱一瞬** —— 看门狗看不到它正是必然" % dt)
            logger.error("SQLite 撞锁(%s):%s · %s", what or "?", verdict,
                         lock_diagnostics(session))
            session.rollback()
            if attempt >= retries:
                raise
            _t.sleep(0.5)
    return False


def lock_diagnostics(session=None) -> str:
    """撞锁现场的一句话诊断 —— **实测**这个连接的参数,不靠"我们以为设了"。

    ⚠️⚠️ **为什么要有它**(2026-10-09):
    05:12 / 13:13 / 21:13 三次 `database is locked`,而 `app.db.database` 的
    **ERROR 一条都没有**(看门狗的 `handle_error` 那条路在生产里**没触发**,原因至今未知)——
    可是**调用点的 `except` 确实跑到了**(它的 traceback 明明在日志里)。
    ⇒ 诊断不能只挂在"我以为是受害者报错那条路"上,要挂在**一定会跑的那一处**。
    同时把 `busy_timeout` / `journal_mode` 取**实际值**:整个排查一直在假设
    "busy_timeout=30000,所以等的人会等 30 秒",而**假设本身从来没验过**。
    """
    bits: list[str] = []
    try:
        if session is not None:
            conn = session.connection()
            bt = conn.exec_driver_sql("PRAGMA busy_timeout").scalar()
            jm = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
            bits.append(f"本连接实测 busy_timeout={bt}ms, journal_mode={jm}")
    except Exception as exc:  # noqa: BLE001 - 诊断自己不许再抛
        bits.append(f"(取连接参数失败:{type(exc).__name__})")
    now = time.monotonic()
    if _OPEN_WRITE_TX:
        for key, (t0, stack, tname) in sorted(_OPEN_WRITE_TX.items(),
                                              key=lambda kv: now - kv[1][0],
                                              reverse=True)[:2]:
            frames = [ln.strip() for ln in stack.splitlines()
                      if ln.strip().startswith('File "')
                      and "site-packages" not in ln and _SELF_FRAME not in ln]
            bits.append(f"本进程有写事务开着 {now - t0:.1f}s(线程 {tname})"
                        f"，开在 {frames[-1] if frames else '?'}")
    else:
        bits.append("本进程**没有**开着的写事务 ⇒ 抱锁的在**别的进程**")
    return " · ".join(bits)


def _install_write_tx_watchdog(engine) -> None:
    """给「把写事务开着做慢活」装一个**报警器**(只报不修)。

    ## 为什么需要它(2026-10-08 实证)
    本机是 SQLite **单写者**,且 `busy_timeout=30000` —— 也就是**别人要等 30 秒才放弃**。
    于是只要有人把**写事务**开着去跑几分钟的慢活(网络 / 浏览器 / LLM),其它作业的写
    就会一个个以 `database is locked` 失败。

    **实测代价**:公众号转存在**夸克那边已经存成、空间也花了**,记录却因
    `_commit_now` 撞锁没落盘 ⇒ 卡片显示「⏳待转存」,而下一轮还会**重存一份、多占一份空间**。
    错误时间戳**精确落在长作业窗口里**(04:02、08:03~08:06),而空闲时写只要 0.003 秒
    ⇒ 不是"锁坏了",是**有人持有太久**。

    ## 判据是"持有"时长,不是单条语句耗时
    从**这个事务里的第一条写语句**算到 `commit` —— 慢的是**持有**,
    单看某一句话是看不出来的。

    ## 它为什么带调用栈
    光说"有个事务开了 200 秒"没法修。栈直接指出**开它的那一处代码**,
    下一次窗口自己就把真凶报出来 —— 比读代码猜可靠得多。
    """
    write_heads = ("insert", "update", "delete", "replace")
    # ★ **装的时候就喊一声**(2026-10-08 补):13:13 那次撞锁,服务端**一条诊断都没有**,
    #   而我无法判断是"没装"还是"装了没触发"。这行日志让下一个看的人一眼就知道它活着。
    # ★ **把实测参数喊出来**(2026-10-09):整个排查一直在假设 busy_timeout=30000,
    #   而那个假设**从来没验过**。这一行让下一个看日志的人一眼知道真实值。
    try:
        with engine.connect() as _c:
            _bt = _c.exec_driver_sql("PRAGMA busy_timeout").scalar()
            _jm = _c.exec_driver_sql("PRAGMA journal_mode").scalar()
        logger.info("SQLite 写事务看门狗已装(阈值 %.0fs);实测 busy_timeout=%sms, journal_mode=%s",
                    WRITE_TX_WARN_SEC, _bt, _jm)
    except Exception:  # noqa: BLE001 - 探一下参数而已,失败不该挡住启动
        logger.info("SQLite 写事务看门狗已装(阈值 %.0fs)", WRITE_TX_WARN_SEC)

    @event.listens_for(engine, "before_cursor_execute")
    def _mark_write_start(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        info = conn.info
        if info.get("tx_write_t0") is not None:
            return
        if str(statement).lstrip()[:8].lower().startswith(write_heads):
            t0 = time.monotonic()
            # ⚠️⚠️ **`limit` 必须够大**:`format_stack(limit=N)` 取的是**最里面** N 帧
            # (最近的),而这里最里面那十几帧**全是 SQLAlchemy 自己 + 我这个 listener**
            # —— 调用方那一帧在最外层,`limit=14` 直接把它截掉了。
            # 实测代价:30 条诊断一条都没点到名,栈尾永远停在 `_mark_write_start`,
            # 我得回头把它调大才有用。60 是留足余量(SQLAlchemy flush 链 ≈ 15 层)。
            stack = "".join(traceback.format_stack(limit=60))
            info["tx_write_t0"] = t0
            info["tx_write_stack"] = stack
            _OPEN_WRITE_TX[id(conn)] = (t0, stack, threading.current_thread().name)

    def _close(conn, how: str) -> None:  # noqa: ANN001
        info = conn.info
        t0 = info.pop("tx_write_t0", None)
        stack = info.pop("tx_write_stack", None)
        _OPEN_WRITE_TX.pop(id(conn), None)
        _WARNED_TX.discard(id(conn))
        if t0 is None:
            return
        dt = time.monotonic() - t0
        if dt >= WRITE_TX_WARN_SEC:
            logger.warning(
                "SQLite 写事务**持有 %.1fs** 才 %s —— 单写者下这会饿死别人的写"
                "(别人等 30 秒就 `database is locked`)。底下是**开这个写事务那一处**的调用栈,"
                "去修它(慢活不该放在事务里):\n%s", dt, how, stack)

    @event.listens_for(engine, "commit")
    def _on_commit(conn):  # noqa: ANN001
        _close(conn, "commit")

    @event.listens_for(engine, "rollback")
    def _on_rollback(conn):  # noqa: ANN001
        _close(conn, "rollback")

    @event.listens_for(engine, "begin")
    def _on_begin(conn):  # noqa: ANN001 - 新事务不该继承上一个的计时
        conn.info.pop("tx_write_t0", None)
        conn.info.pop("tx_write_stack", None)
        _OPEN_WRITE_TX.pop(id(conn), None)

    @event.listens_for(engine, "handle_error")
    def _on_error(exc_ctx):  # noqa: ANN001
        """★ **撞锁的那一刻,把"当前所有开着的写事务"全打出来**。

        ⚠️ 为什么值得这么麻烦:光记"某次提交失败"没法修 —— 得知道**当时是谁抱着锁**。
        这个钩子在受害者报错的那一瞬间做快照,所以它报的就是真凶(而不是等下一个窗口从
        一堆日志里对时间戳猜)。栈里第一条非 SQLAlchemy 的帧就是那处代码。
        """
        if "database is locked" not in str(getattr(exc_ctx, "original_exception", "") or ""):
            return
        # ⚠️ **把"受害者自己"排除掉**:它也在 before_cursor_execute 里登记过(那条写语句
        #    就是失败的那条),不排除的话它会把自己列成"持有者",把真凶淹掉。
        victim = getattr(exc_ctx, "connection", None)
        now = time.monotonic()
        openers = [(k, v) for k, v in _OPEN_WRITE_TX.items() if victim is None or k != id(victim)]
        if not openers:
            logger.error("SQLite 撞锁(`database is locked`)—— 但**本进程没有别的开着的写事务** "
                         "⇒ 抱锁的是**别的进程**(另一个实例 / 外部工具),不是本服务内部")
            return
        parts = []
        for _, (t0, stack, tname) in sorted(openers, key=lambda kv: now - kv[1][0],
                                           reverse=True)[:3]:
            # ⚠️ 只取 `File "…"` **开头的帧** —— `format_stack()` 每条是**多行**
            #    (帧 + 源码行),逐行过滤会把孤立的源码行留在里面,看起来像乱码(实测)。
            # ⚠️ 判据是"不在 site-packages 里",**不是**"路径含 redian" ——
            #    后者在测试/别的部署路径下会把栈滤成空(也实测踩到了)。
            frames = [ln.strip() for ln in stack.splitlines()
                      if ln.strip().startswith('File "')
                      and "site-packages" not in ln and _SELF_FRAME not in ln]
            parts.append(f"  持有 {now - t0:.1f}s(线程 {tname}),开在:\n"
                         + "\n".join("    " + f for f in frames[-4:]))
        logger.error("SQLite 撞锁(`database is locked`)—— **当前开着的写事务**:\n%s",
                     "\n".join(parts))

    # ★★ **主动采样**(2026-10-08 补,对着 13:13 那次「撞锁却零诊断」加的)。
    #
    # 为什么非要它:原来只有两条路会说话 —— **持有者提交时**(长持有告警)和
    # **受害者报错时**(撞锁点名)。而 13:13 那次**两条都没响**(原因至今没定位),
    # 于是我一整天都在**瞎修**(只能看到"又撞锁了",看不到是谁)。
    # 这条守护线程**不依赖任何错误路径**:每 5 秒扫一遍"开着的写事务",
    # 谁超过阈值就报一次(同一事务只报一次,不刷屏)。
    def _sample_forever() -> None:
        while True:
            time.sleep(5.0)
            now = time.monotonic()
            for key, (t0, stack, tname) in list(_OPEN_WRITE_TX.items()):
                if now - t0 < WRITE_TX_WARN_SEC or key in _WARNED_TX:
                    continue
                _WARNED_TX.add(key)
                frames = [ln.strip() for ln in stack.splitlines()
                          if ln.strip().startswith('File "')
                          and "site-packages" not in ln and _SELF_FRAME not in ln]
                logger.warning(
                    "SQLite 写事务**已经持有 %.0fs 还没提交**(线程 %s)—— 单写者下这会饿死别人的写。"
                    "开在:\n%s", now - t0, tname,
                    "\n".join("    " + f for f in frames[-5:]))

    threading.Thread(target=_sample_forever, daemon=True, name="sqlite-tx-sampler").start()


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
            engine_kwargs["connect_args"] = {"timeout": 30}  # sqlite3 busy_timeout 秒
            _engine = create_engine(url, **engine_kwargs)

            @event.listens_for(_engine, "connect")
            def _sqlite_pragma(dbapi_conn, _record):
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA busy_timeout=30000")
                cur.execute("PRAGMA synchronous=NORMAL")
                cur.close()

            # ★ 写事务持有太久的报警器(2026-10-08):`busy_timeout` 只能让**等的人**
            #   多等 30 秒,挡不住**持有的人**开着事务跑几分钟慢活。它只报不修,
            #   目的是把"是谁"打出来。见 `_install_write_tx_watchdog` 的说明。
            _install_write_tx_watchdog(_engine)
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
    "job_heartbeats": ["first_seen_at DATETIME",
                      # **上次执行耗时(ms)**(2026-10-07):此前全仓量不到任何作业时长
                      # (runs.finished_at 从没被写过),错峰只能靠时刻聚类猜。
                      "last_duration_ms INTEGER"],
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
    # `kouling_tried_at`(2026-10-06):夸克口令那条链**试过一次就不再试**。
    # 起因:抖音线索里绝大多数是**迅雷形态**(《》里的群/分享口令),夸克 App 匹配不了 ——
    # 实测同样两条标题,迅雷型不弹卡片、夸克型才弹。不留痕的话每轮都会重试同一批失败项。
        # `publish_at`(2026-10-06):**帖子发布时间** —— 新鲜度的**唯一真依据**。
    # ⚠️ 此前只有 `found_at`(我们发现的时刻),那是**假的新鲜度**:
    # 老帖被推广号反复推时,照样是"刚发现",于是"每天搬 8 条"搬回来的大半是老资源。
    "douyin_leads": ["our_url VARCHAR(500) DEFAULT ''", "kouling_tried_at DATETIME",
                     "publish_at DATETIME",
                     # **首次搬成时刻**(2026-10-07):从"看到"到"搬成"隔了多久,
                     # 是判断"够不够及时"的真指标。存量行 NULL = **当时没记**,
                     # 读取端当"没记"处理,**不要拿 found_at 顶**(那是编数据)。
                     "moved_at DATETIME",
                     # 夸克口令**失败原因**(2026-10-07):此前只进日志,而日志不落盘
                     # ⇒ "失败 7 条为什么"查不出来,只能猜。
                     "last_error VARCHAR(200) DEFAULT ''",
                     # "真的试过"几次(2026-10-07):从此"试过一次就判死"改成"试够 N 次才放弃"。
                     "kouling_tries INTEGER DEFAULT 0"],
    # 迅雷群分享的**首次搬成时刻**(2026-10-07)。⚠️ 别用 `synced_at` 顶 ——
    # 那一列每轮采集都刷新,减出来的"搬成耗时"会随重跑次数变大。
    "xunlei_group_shares": ["moved_at DATETIME"],
    "alerts": ["section VARCHAR(32) DEFAULT ''"],
    "douhot_watch": ["section VARCHAR(16) DEFAULT 'douhot'", "filter_keyword VARCHAR(64) DEFAULT ''",
                     "date_window INTEGER"],
    "douhot_watch_snap": ["section VARCHAR(16) DEFAULT 'douhot'", "entry_title VARCHAR(255) DEFAULT ''",
                          "trend_growth FLOAT DEFAULT 0"],
    "wechat_benchmarks": ["weread_book_id VARCHAR(64) DEFAULT ''", "biz VARCHAR(64) DEFAULT ''"],
    "wechat_candidates": ["url VARCHAR(600) DEFAULT ''",  # 收录按钮用(v2.6.0)
                          "import_tries INTEGER DEFAULT 0"],  # 自动收录失败重试计数(v2.13.0)
    # B站对标号的**扫描状态**(2026-10-05):识别"空壳号"并让轮转**先扫没扫过的**。
    # 起因:实测轮转轮到第 1 个号(uid 650752289)时它**一条投稿都没有**,白跑一轮;
    # 59 个号里有多少这种得先能**量出来**。`video_count = -1` 表示"还没扫过"。
    # ⚠️ **这一项只能有一处** —— 我 2026-10-07 往文件别处又写了一个同名键,
    # 于是**后写的把先写的整个覆盖掉、还不报错**(Python 字典字面量重复键取后者),
    # 新列静默没建上。已加守卫:`tests/test_migrations_unique.py` 用 AST 查重复键。
    "cross_platform_accounts": ["last_scan_at DATETIME", "video_count INTEGER DEFAULT -1",
                                # 自适应降频(2026-10-07):内容没变就给一段冷却,
                                # 别再反复重扫同一个号(实测池子里能出内容的只有 ~14 个)。
                                "last_titles_fp VARCHAR(64) DEFAULT ''",
                                "next_scan_after DATETIME"],
    # B站投稿的**发布时间**(2026-10-06):`fetch_user_titles` 本来就把 `created` 取回来了,
    # **只是下游一直没人用** ⇒ 于是"扫到 30 条"里混着好几年前的老视频,全当新内容喂给 Agent。
    "hot_source_items": ["published_at DATETIME"],    "hotspot_suggestions": ["saves INTEGER DEFAULT 0", "saves_at DATETIME",
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
                        # 基线**时刻**(2026-10-10):没有它就算不出增速,而增速才是
                        # "正在起量"的信号(绝对值是累计量,偏向老文章)
                        "first_read_at DATETIME",
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
