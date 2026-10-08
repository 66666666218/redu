"""列表额度轮转(2026-10-04)。

背景:微信读书 `/web/mp/articles` —— **唯一带精确阅读数**(`readNum`)的接口 ——
有**会话额度**。2026-10-04 实测:续期后从书架第 1 个号问起,**前 29 个全成功、
第 30 个被 `-10100` 挡**。而一轮监听有几十个号 ⇒ **全问必然只有最前面几个拿得到阅读数**,
后面的还白挨一次熔断。

所以每轮只让**一部分号**问列表,其余只取 cover(不花额度)。

⚠️ **这里钉住的是"轮转"的两个性质** —— 它们才是这个机制存在的理由:
① **覆盖**:每个号迟早轮到(不能有号永远排不进去);
② **不卡死**:额度只够前 K 个时,不能因为"没问成的号永远排最前"而让 bookId 靠后的号饿死。
"""
import json
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.db.database import Base  # noqa: E402
from app.db.models import SystemConfig, WechatBenchmark  # noqa: E402
import app.services.wechat_monitor as _wm  # noqa: F401,E402  先导入门面,绕开包内循环导入
from app.services.wechat import _listen as L  # noqa: E402

assert _wm is not None


@pytest.fixture()
def session():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()


def _books(session, n: int) -> list[WechatBenchmark]:
    rows = [WechatBenchmark(user_id=1, nickname=f"号{i}", weread_book_id=f"MP_WXS_{i:03d}",
                            biz=f"MP_WXS_{i:03d}", active=True) for i in range(n)]
    session.add_all(rows)
    session.commit()
    return rows


def _keys(rows) -> set[str]:
    return {r.weread_book_id for r in rows}


def test_window_size_is_within_measured_quota() -> None:
    """窗口不能超过实测额度(29),否则后面的号又要挨熔断。"""
    assert L._LIST_WINDOW <= 29
    assert L._LIST_WINDOW >= 10, "太小会让每个号等太久才轮到一次"


def test_window_does_not_exceed_size_per_round(session) -> None:
    """单轮问列表的号数 == 窗口大小(多一个就可能踩到额度)。"""
    rows = _books(session, 60)
    assert len(L._list_window(session, 1, rows)) == L._LIST_WINDOW


def test_window_picks_the_least_recently_listed(session) -> None:
    """**核心挑法**:取"上次问到列表时间最久"的那批,从没问过的排最前。"""
    rows = _books(session, 60)
    first = L._list_window(session, 1, rows)
    assert first == _keys(rows[:L._LIST_WINDOW]), "从没问过 ⇒ 按 bookId 稳定取前 N 个"

    L._mark_listed(session, 1, first)
    session.commit()
    second = L._list_window(session, 1, rows)
    assert not (second & first), "刚问过的不该立刻又被挑中"
    assert second == _keys(rows[L._LIST_WINDOW:L._LIST_WINDOW * 2])


def test_window_covers_every_book_despite_reordering(session) -> None:
    """⚠️ **覆盖性质**(第一版就是栽在这里):真实一轮的号列表**既不是全量、次序还每轮都变** ——
    先被按批轮转切成子集,再被书架闸门按"有没有更新"重排。按**序号**切窗口在那种列表上
    是**碰巧**能覆盖;按"最久没轮到"挑则**与子集、与排序都无关**。

    这里模拟:每轮从池里随机取 36 个(批轮转)+ 打乱(闸门重排),断言
    `ceil(142/25)=6` 轮之内**每个号都被轮到过**。
    """
    import random

    random.seed(7)
    rows = _books(session, 142)
    pool = list(rows)
    B = 36                                              # = clamp(ceil(142/4), 8, 75)
    n_groups = -(-len(pool) // B)
    covered: set[str] = set()
    rounds = 0
    while rounds < 20 and len(covered) < len(rows):
        start = (rounds % n_groups) * B                 # 批轮转(与 _select_listen_batch 同式)
        batch = pool[start:start + B] or pool
        random.shuffle(batch)                           # 书架闸门按 tier 重排
        picked = L._list_window(session, 1, batch)
        covered |= picked
        L._mark_listed(session, 1, picked)
        session.commit()
        rounds += 1
    assert len(covered) == len(rows), f"{rounds} 轮后仍有 {len(rows) - len(covered)} 个号没轮到"
    assert rounds <= 8, f"142 号 / 窗口 25 应约 6 轮全覆盖,实际用了 {rounds} 轮"


def test_unasked_books_keep_their_place(session) -> None:
    """⚠️ **不卡死**:额度只够前 10 个、窗口 25 个时,没问成的号**不许**永远霸占队头。

    记号按"**问没问过**"记(成败都记)⇒ 每个被挑中的号都轮到过;若按"问没问成"记,
    队头永远是同一批(次键是 bookId),bookId 靠后的号会**永远进不来**。
    这里直接验模型:`_list_attempts` 把失败也算作"问过"。
    """
    stats: dict = {}
    assert L._list_attempts(stats) == 0
    stats["weread_list_skipped"] = 5           # 没问过(不在窗口/被熔断) → 不算
    assert L._list_attempts(stats) == 0
    stats["weread_list_off"] = 1               # 问过但没成 → **算**
    assert L._list_attempts(stats) == 1
    stats["weread_list_ok"] = 2                # 问成了 → 算
    stats["weread_list_off_new"] = 3
    assert L._list_attempts(stats) == 6


def test_marks_only_touch_the_books_that_got_their_turn(session) -> None:
    """没轮到的号记号**保持旧值**(否则它们会被"别人前进"顶到队尾、永久饿死)。"""
    rows = _books(session, 60)
    picked = L._list_window(session, 1, rows)
    L._mark_listed(session, 1, picked)
    session.commit()
    raw = session.scalar(select(SystemConfig).where(
        SystemConfig.key == "weread_list_marks_1"))
    stored = json.loads(raw.value)
    assert set(stored) == picked, "只该记被挑中的那些号"
    assert all(v > 0 for v in stored.values())


def test_later_batches_still_see_unlisted_books_first(session) -> None:
    """记号落在 `weread_book_id` 上 ⇒ **换一批子集**也照样认得出来(序号做不到这点)。"""
    rows = _books(session, 60)
    first = L._list_window(session, 1, rows[:40])
    L._mark_listed(session, 1, first)
    session.commit()
    # 下一轮换了子集(20~59 号)。这一批里 20~24 号**问过了**、25~59 号**没问过**。
    batch = rows[20:]
    nxt = L._list_window(session, 1, batch)
    still_unlisted = _keys(rows[25:])             # 25~59:这一批里从没问过的
    assert not (nxt & _keys(rows[20:25])),         "同一批里还有从没问过的号时,不该再挑问过的 —— 那就是饥饿"
    assert nxt <= (still_unlisted | _keys(rows[20:25]))
    assert still_unlisted >= nxt, "没问过的号全都在窗口里(它们比问过的优先)"


def test_window_handles_empty_and_bad_payload(session) -> None:
    """空列表不炸;`system_config` 里的记号烂了要能回落成"全都从没问过",不能抛。"""
    assert L._list_window(session, 1, []) == set()
    L._mark_listed(session, 1, set())              # 空集:什么都不写
    assert session.scalar(select(SystemConfig).where(
        SystemConfig.key == "weread_list_marks_1")) is None
    session.add(SystemConfig(key="weread_list_marks_1", value="{不是 json"))
    session.commit()
    rows = _books(session, 30)
    assert len(L._list_window(session, 1, rows)) == L._LIST_WINDOW


def test_quota_error_codes_include_the_measured_one() -> None:
    """实测的额度边界码是 `-10100` —— 不登记它,熔断器就不会合闸,剩余号会一路硬撞。"""
    assert "-10100" in L._WEREAD_QUOTA_MARKS
    assert L._is_weread_quota_error(RuntimeError("微信读书错误 code=-10100:"))


# ---------------------------------------------------------------------------
# ★ 2026-10-08:没有 bookId 的号把窗口吃光 ⇒ 阅读数**静默死了两天**
# ---------------------------------------------------------------------------


def _books_without_id(session, n: int) -> list[WechatBenchmark]:
    """手动加的号:有昵称、**没有 bookId**(关联不上微信读书的书) —— 真实库里 263 个号里
    有 57 个是这种(实测)。它们照样要**采 cover**(别的源回答),只是**永远用不上列表窗口**。"""
    rows = [WechatBenchmark(user_id=1, nickname=f"手{i}", weread_book_id="", biz="",
                            active=True) for i in range(n)]
    session.add_all(rows)
    session.commit()
    return rows


def test_没有_bookId的号不许占用窗口名额(session) -> None:
    """★★★ 对着 2026-10-07~10-08「阅读数整片为 0」那条事故加的。真凶**不是**凭据、不是风控。

    机制:`_list_key` 对没有 bookId 的号返回**空串**,而空串在记号里没有(取 0.0 ⇒ 排最前),
    于是窗口的 25 个名额**全被它们占住**;`{...}` 又把 25 个空串**收敛成一个元素 `{""}`**。
    真正有 bookId 的号 `_list_key(b) in {""}` **恒为 False** ⇒ 窗口对它们恒为空 ⇒
    **网页路与 App 路一次都没被调用过**。

    ⚠️ 这里的形状必须是**空 bookId 的号多于窗口**(30 > 25):少于窗口时它们占不满,
    真号还能挤进去几个,这个 bug 就**显不出来**了 —— 用"能跑"反推"没问题"正是本仓的老坑。
    """
    real = _books(session, 60)
    blanks = _books_without_id(session, 30)
    rows = blanks + real
    w = L._list_window(session, 1, rows)
    assert "" not in w, "空键不该出现在窗口里(它在集合里会把整个窗口塌成一个元素)"
    assert len(w) == L._LIST_WINDOW, (
        f"窗口被没有 bookId 的号吃掉了:只有 {len(w)} 个 —— 阅读数会整片掉成 0")
    assert w <= _keys(real), "窗口里只该是有 bookId 的真号"
    assert w == _keys(real[:L._LIST_WINDOW]), "挑法不变:仍按「最久没轮到」取前 N 个"


def test_全是空_bookId的池子不炸_只是没有窗口(session) -> None:
    """整池都没 bookId 时要回**空集**(没有谁问得动列表),而不是抛、也不是放出空键。"""
    _books_without_id(session, 30)
    w = L._list_window(session, 1, session.query(WechatBenchmark).all())
    assert w == set(), f"没有可问的号时窗口必须是空的,实际 {w}"


def test_混入空_bookId的号_不影响轮转覆盖(session) -> None:
    """覆盖性质(见 `test_window_covers_every_book_despite_reordering`)在**夹着空号**的池子上
    必须照样成立 —— 这是"修了窗口大小、却把轮转弄坏"的防线。"""
    real = _books(session, 60)
    pool = list(real) + _books_without_id(session, 30)
    covered: set[str] = set()
    for _ in range(6):
        w = L._list_window(session, 1, pool)
        covered |= w
        L._mark_listed(session, 1, w)
        session.commit()
    assert covered == _keys(real), (
        f"6 轮后还有 {len(_keys(real)) - len(covered)} 个真号没轮到过列表")

