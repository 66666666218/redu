"""苗头 Agent 自学习回测:命中率样本必须跨调用持久化(否则累计≥10 永远凑不齐)。

回归 agent_learning.backtest_and_learn 的一处缺陷:signal_stats 被加载、在循环里累加,
但历史实现从未写回 system_config → 每次调用从空重建,自学习闭环断裂。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import json
from datetime import datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db import models  # noqa: F401
from app.db.models import AgentStage, SystemConfig, User, WeiboHotItem
from app.services import agent_learning


def _session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _seed_rising(db, user_id: int, kw: str) -> None:
    """某关键词:3 天前基线=100,昨天=200(翻倍 → 回测判命中)。阶段行 3 天前更新。"""
    now = datetime.now()
    db.add(WeiboHotItem(user_id=user_id, title=kw, heat=100, rank=1, captured_at=now - timedelta(days=4)))
    db.add(WeiboHotItem(user_id=user_id, title=kw, heat=200, rank=1, captured_at=now - timedelta(days=1)))
    db.add(AgentStage(user_id=user_id, board="weibo", norm=kw, kw=kw, stage="苗头",
                      parts="增速+100%", score=100,
                      updated_at=now - timedelta(days=3)))
    db.commit()


def _load_stats(db) -> dict:
    row = db.scalar(select(SystemConfig).where(SystemConfig.key == "agent_signal_stats"))
    return json.loads(row.value) if row and row.value else {}


def test_backtest_persists_signal_stats() -> None:
    """回测后 agent_signal_stats 必须落库:这是自学习累计的前提(修复前恒为空)。"""
    db = _session()
    for i, kw in enumerate(["词甲", "词乙", "词丙"]):
        _seed_rising(db, 1, kw)

    out = agent_learning.backtest_and_learn(db, 1)
    assert out["backtested"] == 3
    assert out["hits"] == 3, "热度翻倍应判命中"

    stats = _load_stats(db)
    assert stats, "signal_stats 应被持久化(缺陷:从未写回)"
    assert stats.get("增速", {}).get("total") == 3
    assert stats.get("增速", {}).get("hits") == 3
    db.close()


def test_backtest_seen_cursor_dedups_across_calls() -> None:
    """同一静止阶段行次日再回测应被 seen 游标跳过,命中率样本不重复累加。"""
    db = _session()
    _seed_rising(db, 1, "稳定词")

    agent_learning.backtest_and_learn(db, 1)
    after_first = _load_stats(db)
    assert after_first.get("增速", {}).get("total") == 1

    # 第二次调用:updated_at 未变 → row_sig 命中 seen → 跳过,不再计数
    out2 = agent_learning.backtest_and_learn(db, 1)
    assert out2["backtested"] == 0
    after_second = _load_stats(db)
    assert after_second.get("增速", {}).get("total") == 1, "静止行不应重复记账"
    db.close()


class TestSignalMappingOrder:
    """★ **信号标签→权重键的映射里,顺序就是语义**(2026-10-07)。

    判定是 `startswith(sig) or sig in part` 且**首个匹配胜** —— 所以「跨平台共振」
    必须排在「共振」**前面**。排后面的话它永远被"共振"吃掉,**而且不报错、只是永远学不到**
    (这类静默失效最难发现,所以抽成纯函数单独钉住)。
    """

    def test_跨平台共振不能被共振吃掉(self) -> None:
        from app.services.agent_learning import sig_of_part

        assert sig_of_part("跨平台共振(4平台/32号)") == "cross_resonance"
        assert sig_of_part("共振") == "resonance"

    def test_其余信号照旧(self) -> None:
        from app.services.agent_learning import sig_of_part

        assert sig_of_part("增速+52%") == "velocity"
        assert sig_of_part("排名↑3") == "rank_jump"
        assert sig_of_part("量级200") == "volume"

    def test_认不出的返回空而不是瞎归一类(self) -> None:
        from app.services.agent_learning import sig_of_part

        assert sig_of_part("某个新标签") == ""

    def test_新权重键有默认值(self) -> None:
        """没有默认值的话,`load_weights` 补不出来,打分时会 KeyError。"""
        from app.services.agent_learning import DEFAULT_WEIGHTS

        assert DEFAULT_WEIGHTS.get("cross_resonance") == 35
