"""品类权重(2026-10-04 用户口径:「自动调选题权重」)。

钉住的是**三条护栏**——本仓对"自动调权重"本来就存疑(`DouyinLead.share_count` 的
docstring 写着"在拿到几周真实偏差之前**不用它自动调权重**"),所以这里宁可保守。
"""
import os
from datetime import datetime, timedelta

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.db.database import Base  # noqa: E402
from app.db.models import HotspotSuggestion  # noqa: E402
from app.services import category_weight as cw  # noqa: E402


@pytest.fixture()
def session():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()


def _settled(session, cat: str, n: int, reads: int, reposts: int, days_ago: int = 1) -> None:
    for _ in range(n):
        session.add(HotspotSuggestion(
            user_id=1, keyword="kw", kind="match", category=cat, link="x",
            acted=True,
            reads_gain=reads, repost_gain=reposts,
            settled_at=datetime.now() - timedelta(days=days_ago)))
    session.commit()


# ---------------------------------------------------------------- 护栏
def test_no_data_means_empty_and_neutral(session) -> None:
    """★ **向后兼容**:没有任何结算数据 ⇒ 空 dict ⇒ 倍数恒 1.0,公式与以前完全一致。"""
    assert cw.multipliers(session, 1) == {}
    assert cw.multiplier_for({}, "影视") == 1.0


def test_categories_below_min_samples_are_excluded(session) -> None:
    """★ **绝不拿 1 条样本去调全盘** —— 样本 <`MIN_SAMPLES` 的品类整个不参与。"""
    _settled(session, "资料", 1, reads=99999, reposts=99999)     # 只有 1 条,成绩再好看也不算
    assert cw.multipliers(session, 1) == {}


def test_multiplier_is_clamped(session) -> None:
    """★ **倍数夹在 `[LO, HI]`** —— 一次调太猛会让整个选题被单一品类带偏。"""
    _settled(session, "爆款", 5, reads=1_000_000, reposts=1_000_000)
    _settled(session, "冷门", 5, reads=1, reposts=1)
    m = cw.multipliers(session, 1)
    # 比值极大/极小 ⇒ 被**夹**在上下限内(不是恰好等于端点:基准是两类的均值)
    assert 1.9 < m["爆款"]["reads"] <= cw.HI, f"应贴住上限,实际 {m['爆款']['reads']}"
    assert cw.LO <= m["冷门"]["reads"] < 0.6, f"应贴住下限,实际 {m['冷门']['reads']}"
    assert cw.multiplier_for(m, "爆款") <= cw.HI


def test_clamp_handles_garbage() -> None:
    assert cw.clamp(float("nan")) == 1.0
    assert cw.clamp(-3) == 1.0
    assert cw.clamp(0) == 1.0
    assert cw.clamp("x") == 1.0
    assert cw.clamp(1.5) == 1.5


def test_ratio_is_neutral_when_baseline_is_zero() -> None:
    """全盘均值为 0 ⇒ 没法比,**返回 1.0** 而不是除零爆掉或编一个值。"""
    assert cw.ratio_of(100, 0) == 1.0
    assert cw.ratio_of(0, 0) == 1.0


# ---------------------------------------------------------------- 取值
def test_multiplier_is_the_geometric_mean(session) -> None:
    """★ 用**几何平均**而不是算术:倍数天然是乘性的。

    一个依据说"该品类特别好"(夹到 2.0)、另一个说"特别差"(夹到 0.5)——
    算术平均会给出 1.25(看着像"偏好"),而真相是 **1.0(两条依据正好抵消)**。
    """
    assert cw.multiplier_for({"X": {"reads": 2.0, "reposts": 0.5, "n": 9}}, "X") == 1.0
    assert cw.multiplier_for({"X": {"reads": 2.0, "reposts": 2.0, "n": 9}}, "X") == 2.0


def test_relative_ranking_between_categories(session) -> None:
    """同盘对比:成绩好的品类倍数 > 1,差的 < 1(而不是只看绝对值)。"""
    _settled(session, "好", 4, reads=400, reposts=40)
    _settled(session, "差", 4, reads=100, reposts=10)
    m = cw.multipliers(session, 1)
    assert m["好"]["reads"] > 1.0 > m["差"]["reads"]
    assert m["好"]["n"] == 4 and m["差"]["n"] == 4        # 样本数要带出来


def test_unknown_category_is_neutral() -> None:
    """没学过的品类 → 1.0(**不许**因为"没数据"就惩罚它)。"""
    assert cw.multiplier_for({"影视": {"reads": 2.0, "reposts": 2.0, "n": 5}}, "资料") == 1.0
    assert cw.multiplier_for({"影视": {"reads": 2.0, "reposts": 2.0, "n": 5}}, "") == 1.0


def test_old_settlements_are_ignored(session) -> None:
    """只看窗口内的结算 —— 半年前的成绩不该主导今天的选题。"""
    _settled(session, "老品类", 5, reads=1000, reposts=100, days_ago=200)
    assert cw.multipliers(session, 1, days=60) == {}
