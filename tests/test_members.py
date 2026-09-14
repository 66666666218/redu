"""群会员续费管理单测:到期计算/tick 名单/冷却门。"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db.models import GroupMember, User
from app.services import members as svc


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(User(id=1, username="a", email="a@b.c", password_hash="x"))
    db.commit()
    yield db
    db.close()


class _FakeFeishu:
    sent = []

    def __init__(self, *a, **kw):
        pass

    def send(self, msg):
        _FakeFeishu.sent.append(msg)
        return True


def _settings():
    import types
    return types.SimpleNamespace(
        feishu_webhook="https://hook/x", feishu_webhook_wechat="https://hook/wc",
        feishu_secret="", feishu_alert_cooldown_hours=24)


def test_member_states(session):
    now = datetime.now()
    m_ok = GroupMember(user_id=1, nickname="新人", joined_at=now - timedelta(days=5), cycle_days=30)
    m_due = GroupMember(user_id=1, nickname="该收", joined_at=now - timedelta(days=30, hours=2), cycle_days=30)
    m_over = GroupMember(user_id=1, nickname="该踢", joined_at=now - timedelta(days=32), cycle_days=30)
    session.add_all([m_ok, m_due, m_over])
    session.commit()
    assert svc.member_state(m_ok, now)["state"] == "ok"
    assert svc.member_state(m_due, now)["state"] == "due"       # 到期 2h:该私信收续费
    assert svc.member_state(m_over, now)["state"] == "overdue"  # 超 48h:该踢


def test_renew_resets_cycle(session):
    session.add(GroupMember(user_id=1, nickname="甲", joined_at=datetime.now() - timedelta(days=40)))
    session.commit()
    m = session.scalar(select(GroupMember))
    assert svc.member_state(m)["state"] in ("due", "overdue")
    assert svc.renew(session, 1, m.id) is True
    assert svc.member_state(m)["state"] == "ok"  # 续费后回到新周期
    # 租户隔离:他人续不了
    assert svc.renew(session, 99, m.id) is False


def test_renewal_tick_lists_due_and_overdue(session, monkeypatch):
    import app.services.feishu_client as fc

    monkeypatch.setattr(fc, "FeishuClient", _FakeFeishu)
    _FakeFeishu.sent = []
    now = datetime.now()
    session.add_all([
        GroupMember(user_id=1, nickname="到期的", joined_at=now - timedelta(days=30, hours=1)),
        GroupMember(user_id=1, nickname="超期的", joined_at=now - timedelta(days=33)),
        GroupMember(user_id=1, nickname="没到期的", joined_at=now - timedelta(days=3)),
    ])
    session.commit()
    out = svc.renewal_tick(settings=_settings(), db=session)
    assert out["due"] == 1 and out["overdue"] == 1
    assert len(_FakeFeishu.sent) == 1
    text = _FakeFeishu.sent[0]
    assert "该收续费" in text and "到期的" in text
    assert "该踢出群聊" in text and "超期的" in text
    assert "没到期的" not in text
    # 第二次跑:冷却门(24h)拦住,不重复推
    out2 = svc.renewal_tick(settings=_settings(), db=session)
    assert len(_FakeFeishu.sent) == 1
