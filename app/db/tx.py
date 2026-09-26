"""SAVEPOINT 工具:把"易碎的附带写入"关进保存点,失败只撤销自己那段。

背景:项目里多处兜底逻辑写 `session.rollback()` 表示"这段锦上添花失败了",
但 `Session.rollback()` 撤销的是**整个外层事务**。监听轮里第一条 commit 在收尾,
中途一次告警发送失败就能把本轮已采到的全部新文章(或已付费的采样)一起抹掉
(2026-09-26 第八轮审计)。这两种包装让这类回滚只作用于自己的写:

- `savepoint(session)`:块内异常 → 只回滚本块,异常继续外抛。
- `HeldSavepoint(session)`:攥在手里,由调用方稍后决定 `close(keep=…)`,
  供"先抢冷却门 → 发送 → 失败则不烧门"这类跨网络调用的两段式收尾。
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy.orm import Session, SessionTransaction


def _quiet(tx: SessionTransaction, keep: bool) -> None:
    """收尾保存点:keep=True 释放(本段写留在外层事务,随调用方 commit 落库),False 撤销本段。

    段内可能已发生外层 `commit()`(如告警成功路径要落冷却门),那时保存点已随外层事务关闭,
    再对它下命令会抛异常——静默跳过即可,数据已经落库。
    """
    try:
        tx.commit() if keep else tx.rollback()
    except Exception:  # noqa: BLE001 - 事务已关闭
        pass


class HeldSavepoint:
    """跨调用持有一个 SAVEPOINT,由调用方决定这段写留不留。"""

    def __init__(self, session: Session) -> None:
        self._tx = session.begin_nested()

    def close(self, keep: bool) -> None:
        _quiet(self._tx, keep)


@contextmanager
def savepoint(session: Session) -> Iterator[None]:
    """在 SAVEPOINT 内执行;块内异常只回滚本块,外层未提交数据不受牵连。"""
    held = HeldSavepoint(session)
    try:
        yield
    except BaseException:
        held.close(False)
        raise
    held.close(True)
