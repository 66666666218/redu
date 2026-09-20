"""跨平台共同上升告警门回归:冷却只烧"入卡"的前 N 条,溢出词下轮轮候而非静默丢失。

见 cross_platform.run_cross_platform_alert:卡片渲染 hits[:12],历史缺陷是对全部候选
先写冷却门再截断渲染 → 第 13 条起被烧冷却却从未展示,整个冷却窗口静默丢失。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

from types import SimpleNamespace

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db import models  # noqa: F401
from app.db.models import FeishuAlert
from app.services import cross_platform as cp


def _session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _items(n: int) -> list[dict]:
    return [{"keyword": f"词{i}", "platforms": ["weibo", "baidu"],
             "forecasts": {"weibo": 100 + i, "baidu": 90 + i}, "burst": False,
             "avg_forecast": 95 + i} for i in range(n)]


class _FakeClient:
    """记录每次 send 的文本,始终返回成功。"""

    last_text = None
    calls = 0

    def __init__(self, *a, **kw):
        pass

    def send(self, text):  # noqa: ANN001
        _FakeClient.last_text = text
        _FakeClient.calls += 1
        return True


def test_cross_platform_backlog_rotates_not_silently_cooled(monkeypatch) -> None:
    """15 个候选 > 卡片上限 12:首轮推前 12 并只落这 12 条冷却;次轮把溢出 3 条补推。"""
    db = _session()
    settings = SimpleNamespace(feishu_webhook="https://hook", feishu_secret="s",
                               feishu_alert_cooldown_hours=6)
    _FakeClient.calls = 0
    monkeypatch.setattr("app.services.feishu_client.FeishuClient", _FakeClient)
    # rising_across 返回全部 15 条(模拟超过卡片上限)
    monkeypatch.setattr(cp, "rising_across", lambda db_, uid, min_platforms=2: _items(15))

    pushed1 = cp.run_cross_platform_alert(1, settings=settings, db=db)
    assert pushed1 == 12, "首轮只应推送并落冷却入卡的前 12 条"
    cooled = db.scalars(select(FeishuAlert.title).where(FeishuAlert.section == "cross_up")).all()
    assert len(cooled) == 12, f"溢出词不应被烧冷却(实际 {len(cooled)} 条)"
    # 溢出的"词12/13/14"首轮未入卡 → 首轮不应有它们的冷却
    for i in (12, 13, 14):
        assert f"up:词{i}" not in cooled

    # 次轮:前 12 仍在冷却窗口被跳过,溢出的 3 条补推(证明未静默丢失)
    pushed2 = cp.run_cross_platform_alert(1, settings=settings, db=db)
    assert pushed2 == 3, f"溢出的 3 条应在次轮轮候推出(实际 {pushed2})"
    assert "词12" in (_FakeClient.last_text or "")
    total = db.scalar(select(func.count()).select_from(FeishuAlert).where(
        FeishuAlert.section == "cross_up"))
    assert total == 15, "两轮合计覆盖全部 15 条,无丢失"
    db.close()


def test_cross_platform_send_failure_writes_no_cooldown(monkeypatch) -> None:
    """发送失败时不落任何冷却门:全部候选下轮仍可再推,不丢告警。"""
    db = _session()
    settings = SimpleNamespace(feishu_webhook="https://hook", feishu_secret="s",
                               feishu_alert_cooldown_hours=6)

    class _FailClient(_FakeClient):
        def send(self, text):  # noqa: ANN001
            return False

    monkeypatch.setattr("app.services.feishu_client.FeishuClient", _FailClient)
    monkeypatch.setattr(cp, "rising_across", lambda db_, uid, min_platforms=2: _items(5))

    pushed = cp.run_cross_platform_alert(1, settings=settings, db=db)
    assert pushed == 0
    n = db.scalar(select(func.count()).select_from(FeishuAlert).where(FeishuAlert.section == "cross_up"))
    assert n == 0, "发送失败不应留下冷却门记录"
    db.close()
