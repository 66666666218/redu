"""SQLite **写事务看门狗**测试(2026-10-08)。

## 它守的是什么
本机是 SQLite **单写者**,而 `busy_timeout=30000` 只能让**等的人**多等 30 秒 ——
它挡不住**持有的人**开着写事务去跑几分钟慢活(网络/浏览器/LLM)。
2026-10-08 实测代价:公众号转存**在夸克那边已经存成、空间也花了**,记录却因
`_commit_now` 撞锁没落盘 ⇒ 卡片显示「⏳待转存」,下一轮还要**重存一份**。

所以看门狗做两件事:
1. **持有太久就告警**(带调用栈)—— 修根因的线索;
2. **撞锁那一刻把"当前开着的写事务"全打出来** —— 直接点名是谁抱着锁,
   不用等下一个窗口从日志里对时间戳猜。

这里测的就是这两件事。
"""
from __future__ import annotations

import logging
import time

import pytest
from sqlalchemy import create_engine, text

from app.db import database as dbm


class _Collect(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        self.records.append(record)


@pytest.fixture
def cap():
    h = _Collect()
    lg = logging.getLogger("app.db.database")
    lg.addHandler(h)
    lg.setLevel(logging.WARNING)
    yield h
    lg.removeHandler(h)


@pytest.fixture
def engine(tmp_path, monkeypatch):
    """真实文件库 + 看门狗。⚠️ 临时把阈值调小,免得测试真等 20 秒。"""
    monkeypatch.setattr(dbm, "WRITE_TX_WARN_SEC", 0.5)
    eng = create_engine(f"sqlite:///{tmp_path/'t.db'}", connect_args={"timeout": 2})
    dbm._install_write_tx_watchdog(eng)
    with eng.begin() as c:
        c.execute(text("CREATE TABLE t (k TEXT)"))
    yield eng
    eng.dispose()


def _hold_write_tx_in_named_function(conn):
    """★ 故意写成**有名字的函数** —— 撞锁时那份栈里应当出现这个名字。"""
    conn.execute(text("INSERT INTO t VALUES ('culprit')"))


# ---------------------------------------------------------------------------
# ① 持有太久 → 告警(带调用栈)
# ---------------------------------------------------------------------------


def test_写事务持有太久要告警并带调用栈(engine, cap):
    with engine.begin() as c:
        c.execute(text("INSERT INTO t VALUES ('a')"))
        time.sleep(0.8)                      # > 阈值 0.5s
    warn = [r for r in cap.records if "写事务**持有" in r.getMessage()]
    assert warn, "持有超过阈值却没有告警"
    msg = warn[0].getMessage()
    assert "test_db_write_tx_watchdog" in msg, f"栈里没指到测试文件:\n{msg}"


def test_快事务不该告警(engine, cap):
    """反面对照 —— 不然告警会被噪音淹掉(本仓反复讲过的"告警变噪音"教训)。"""
    for _ in range(3):
        with engine.begin() as c:
            c.execute(text("INSERT INTO t VALUES ('b')"))
    assert not [r for r in cap.records if "写事务**持有" in r.getMessage()]


# ---------------------------------------------------------------------------
# ② 撞锁那一刻 → 点名持有者
# ---------------------------------------------------------------------------


def test_撞锁要点名是谁抱着锁(engine, cap):
    """★ 核心用例:受害者报错的那一瞬间,快照"当前开着的写事务",栈里指到**开它的那处**。"""
    holder = engine.connect()
    _hold_write_tx_in_named_function(holder)      # 抱着写事务不放
    try:
        victim = engine.connect()
        try:
            with pytest.raises(Exception):
                victim.execute(text("INSERT INTO t VALUES ('v')"))
                victim.commit()
        finally:
            victim.close()
    finally:
        holder.rollback()
        holder.close()

    errs = [r for r in cap.records if "撞锁" in r.getMessage()]
    assert errs, "撞锁了却没有点名"
    msg = errs[0].getMessage()
    assert "_hold_write_tx_in_named_function" in msg, (
        f"栈里没指到**开写事务那一处**:\n{msg}")
    assert "持有" in msg and "s(线程" in msg, f"没报持有时长/线程:\n{msg}"


def test_没有别的持有者时要说清是别的进程(engine, cap):
    """★ **区分"我们内部"与"外部进程"**:两种的修法完全不同,含糊过去就会白查。

    做法:直接给引擎发一条**会被判为撞锁**的错误(用一个开着写事务的**另一个引擎**
    模拟"别的进程" —— 它的连接不在本进程的登记表里)。
    """
    other = create_engine(f"sqlite:///{engine.url.database}", connect_args={"timeout": 1})
    oc = other.connect()
    oc.execute(text("INSERT INTO t VALUES ('outside')"))      # 外部持有者,不进本进程登记表
    try:
        with pytest.raises(Exception):
            with engine.begin() as c:
                c.execute(text("INSERT INTO t VALUES ('v')"))
    finally:
        oc.rollback()
        oc.close()
        other.dispose()
    errs = [r for r in cap.records if "撞锁" in r.getMessage()]
    assert errs and "别的进程" in errs[0].getMessage(), (
        f"外部持有时应说清是别的进程,实际:\n{errs[0].getMessage() if errs else '(没报)'}")
