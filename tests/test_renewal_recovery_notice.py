"""微信读书「续期恢复正常」通知的**前提条件**单测(2026-10-06)。

## 问题
那条通知写着"**此前如有「续期失败」告警,以本条为准**",但代码**从不检查此前失败过没有** ——
它只是"续期成功就发",冷静期 6 小时,而续期**每 6 小时成功一次** ⇒
**系统完全健康也天天推"恢复正常"**(实测约 2 次/天,用户直接来问"为什么今天还在提醒我")。

**没有失败就报恢复 = 把告警变成噪音;而噪音会训练人忽略整块告警**
(与"恒为 0 的档位""假红"同一条教训)。

判据改成旗子:失败时立(`_mark_renewal_failed`)、**发出去才落**(`_clear_renewal_failed`)。
"""
import os
import sys

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.db.database import Base  # noqa: E402
from app.db import models  # noqa: E402,F401
from app.db.models import SystemConfig, User  # noqa: E402
from app.services import wechat_monitor  # noqa: E402   # 先导门面,否则循环导入
from app.services.cookie_store import set_cookie  # noqa: E402
from config.settings import Settings  # noqa: E402

SRC = sys.modules["app.services.wechat._source"]


@pytest.fixture
def engine():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    eng_local = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)
    eng._mk = eng_local  # type: ignore[attr-defined]
    return eng


@pytest.fixture
def session(engine):
    db = engine._mk()  # type: ignore[attr-defined]
    db.add(User(id=1, username="u1", email="u1@b.c", password_hash="x", enabled=True))
    set_cookie(db, 1, "weread", "wr_rt=R; wr_skey=OLD")
    db.commit()
    yield db
    db.close()


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, is_dev=True, **kw)


class _Weread:
    """假微信读书:续期换得出新 skey,书架验证通过。"""

    def __init__(self, cookie) -> None:
        self.cookie = cookie

    def refresh_skey(self):
        return "wr_rt=R; wr_skey=NEW"

    def shelf(self):
        return {"books": []}


@pytest.fixture
def sent(monkeypatch):
    """拦下飞书推送,返回收件箱。"""
    box: list[str] = []

    class _Sender:
        def __init__(self) -> None:
            pass

        def send(self, text):
            box.append(text)
            return True

    monkeypatch.setattr(wechat_monitor, "WereadClient", _Weread)
    import app.services.feishu_client as fc
    monkeypatch.setattr(fc, "webhook_for", lambda st, section: "https://hook/x")
    monkeypatch.setattr(fc, "FeishuClient", lambda hook, secret=None: _Sender())
    import app.services.alert_service as asvc
    monkeypatch.setattr(asvc, "feishu_alert_gate",
                        lambda db, u, sec, key, cool, reason="": True)
    return box


class TestNoticeOnlyAfterAFailure:
    def test_没失败过_就不该发恢复通知(self, session, sent) -> None:
        """★ 这条是用户被问到脸上的那件事:系统一直好好的,却天天报"恢复正常"。"""
        out = SRC.refresh_weread_cookie(session, 1, settings=_settings())
        assert out["status"] == "success", out
        assert sent == [], (
            "**没有失败就报恢复** ⇒ 通知变成噪音,而噪音会训练人忽略整块告警。"
            f"实际发了:{sent}")

    def test_真的失败过_才发并且发完落旗(self, session, sent) -> None:
        SRC._mark_renewal_failed(session, 1)
        session.commit()
        assert SRC._renewal_failed_since(session, 1), "前提:失败旗子立着"

        out = SRC.refresh_weread_cookie(session, 1, settings=_settings())
        assert out["status"] == "success", out
        assert len(sent) == 1 and "恢复正常" in sent[0], sent
        assert not SRC._renewal_failed_since(session, 1), (
            "**发出去就该落旗** —— 不落的话下一轮又会以同一个理由再发一次(又变成噪音)")

    def test_连着成功不会重复发(self, session, sent) -> None:
        SRC._mark_renewal_failed(session, 1)
        session.commit()
        SRC.refresh_weread_cookie(session, 1, settings=_settings())
        SRC.refresh_weread_cookie(session, 1, settings=_settings())
        SRC.refresh_weread_cookie(session, 1, settings=_settings())
        assert len(sent) == 1, f"一次失败只该配一次恢复通知,实际 {len(sent)} 条"


class TestFailureRaisesTheFlag:
    def test_续期失败会立旗(self, session, engine, monkeypatch) -> None:
        """★ 旗子是恢复通知的**唯一**依据 —— 它立不起来,恢复通知就永远不会发。"""
        # ⚠️ 它是在函数内 `from app.db import get_session_local` 取的,所以补丁要打在 `app.db` 上
        import app.db as appdb
        monkeypatch.setattr(appdb, "get_session_local", lambda: engine._mk)  # type: ignore[attr-defined]
        monkeypatch.setattr(wechat_monitor, "refresh_weread_cookie",
                            lambda db, uid, settings=None: {"status": "failed",
                                                            "reason": "renewal_failed"})
        import app.services.alert_service as asvc
        monkeypatch.setattr(asvc, "notify_incident", lambda *a, **k: True)

        SRC.weread_refresh_tick(settings=_settings())

        with engine._mk() as db:  # type: ignore[attr-defined]
            row = db.scalar(select(SystemConfig).where(
                SystemConfig.key == SRC._RENEWAL_FAILED_KEY.format(uid=1)))
        assert row is not None, "续期失败必须立旗,否则之后成功了也不会发恢复通知"
