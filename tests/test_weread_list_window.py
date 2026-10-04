"""列表额度轮转窗口(2026-10-04)。

背景:微信读书 `/web/mp/articles` —— **唯一带精确阅读数**(`readNum`)的接口 ——
有**会话额度**。2026-10-04 实测:续期后从书架第 1 个号问起,**前 29 个全成功、
第 30 个被 `-10100` 挡**。而一轮监听有 75 个号 ⇒ **全问必然只有前 29 个拿得到阅读数**,
后面的还白挨一次熔断。

所以列表只对**一段轮转窗口**里的号要,窗口平滑右移,让每个号迟早轮到。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.db.database import Base  # noqa: E402
from app.db.models import SystemConfig, WechatBenchmark  # noqa: E402
import app.services.wechat_monitor  # noqa: F401,E402  先导入门面,绕开包内循环导入
from app.services.wechat import _listen as L  # noqa: E402


@pytest.fixture()
def session():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()


def _books(session, n: int) -> list[WechatBenchmark]:
    rows = [WechatBenchmark(user_id=1, nickname=f"号{i}", weread_book_id=f"MP_WXS_{i}",
                            biz=f"MP_WXS_{i}", active=True) for i in range(n)]
    session.add_all(rows)
    session.commit()
    return rows


def test_window_size_is_within_measured_quota() -> None:
    """窗口不能超过实测额度(29),否则后面的号又要挨熔断。"""
    assert L._LIST_WINDOW <= 29
    assert L._LIST_WINDOW >= 10, "太小会让每个号等太久才轮到一次"


def test_window_rotates_and_eventually_covers_every_book(session) -> None:
    """⚠️ **核心性质**:轮转若干轮之后,**每个号都该被轮到过**。

    这才是这个机制存在的理由 —— 额度只够问一部分号,那就轮流问;
    如果窗口不转(或推游标的条件写错),队尾的号会**永久拿不到阅读数**。
    """
    rows = _books(session, 60)
    seen: set[int] = set()
    rounds = 0
    while rounds < 20 and len(seen) < len(rows):
        allow, nxt = L._list_window(session, 1, rows)
        seen |= allow
        L._advance_list_cursor(session, 1, nxt)
        session.commit()
        rounds += 1
    assert len(seen) == len(rows), f"{rounds} 轮后仍有 {len(rows) - len(seen)} 个号没轮到"
    assert len(seen & {r.id for r in rows}) == len(rows)


def test_window_does_not_exceed_size_per_round(session) -> None:
    """单轮问列表的号数 == 窗口大小(不能多 —— 多一个就可能踩到额度)。"""
    rows = _books(session, 60)
    allow, _ = L._list_window(session, 1, rows)
    assert len(allow) == L._LIST_WINDOW


def test_cursor_is_persisted_across_calls(session) -> None:
    """游标存在 `system_config`,进程重启后接着转(否则每次都从同一批开始)。"""
    rows = _books(session, 60)
    _, nxt = L._list_window(session, 1, rows)
    L._advance_list_cursor(session, 1, nxt)
    session.commit()
    row = session.scalar(select(SystemConfig).where(
        SystemConfig.key == "weread_list_cursor_1"))
    assert row is not None and int(row.value) == nxt
    # 再取一次窗口,应当已经移过位置
    allow2, _ = L._list_window(session, 1, rows)
    allow1, _ = L._list_window(session, 1, rows)
    assert allow2 == allow1  # 未推进时窗口稳定
    assert len(allow2) == L._LIST_WINDOW


def test_window_handles_empty_and_wrapping(session) -> None:
    """空书架不炸;游标越界能环绕。"""
    assert L._list_window(session, 1, []) == (set(), 0)
    rows = _books(session, 30)
    L._advance_list_cursor(session, 1, 29)      # 逼近末端
    session.commit()
    allow, nxt = L._list_window(session, 1, rows)
    assert len(allow) == L._LIST_WINDOW and 0 <= nxt < len(rows)


def test_quota_error_codes_include_the_measured_one() -> None:
    """实测的额度边界码是 `-10100` —— 不登记它,熔断器就不会合闸,剩余号会一路硬撞。"""
    assert "-10100" in L._WEREAD_QUOTA_MARKS
    assert L._is_weread_quota_error(RuntimeError("微信读书错误 code=-10100:"))
