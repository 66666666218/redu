"""2026-09-14 全项目审计修复回归:
SSRF 守卫 / 采集空数据≠成功 / 告警冷却门发送成功才落库 / JWT 有效期 / retry 收敛。
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, FeishuAlert, RunRecord, User


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    yield db
    db.close()


class TestSsrfGuard:
    def test_private_ip_rejected(self):
        from app.utils.net import UnsafeUrlError, assert_public_url

        for url in ("http://127.0.0.1:8080/x", "http://192.168.1.1/a",
                    "http://169.254.169.254/latest/meta-data/", "http://10.0.0.1/",
                    "file:///etc/passwd", "ftp://x", "http://localhost/"):
            with pytest.raises(UnsafeUrlError):
                assert_public_url(url)

    def test_public_domain_passes(self):
        from app.utils.net import assert_public_url

        assert_public_url("https://mp.weixin.qq.com/s/abc")  # 不抛即过

    def test_fetch_article_content_skips_internal(self, monkeypatch):
        from app.services import wechat_monitor as wm

        called = {"n": 0}

        def _boom(*a, **kw):
            called["n"] += 1
            raise AssertionError("不应发起网络请求")

        monkeypatch.setattr(wm.requests, "get", _boom)
        assert wm.fetch_article_content("http://169.254.169.254/latest/meta-data/") == ""
        assert wm.extract_article_meta("http://127.0.0.1:8080/") == {}
        assert called["n"] == 0


class TestEmptyParseNotSuccess:
    def test_weibo_empty_realtime_raises(self, monkeypatch):
        from app.services import collector

        monkeypatch.setattr(collector, "_get_json", lambda s, url, headers=None: {"data": {"realtime": []}})
        import types
        st = types.SimpleNamespace(weibo_cookie="")
        with pytest.raises(collector.CollectionError):
            collector.fetch_hot_search(st, session=object())

    def test_xianyu_all_keywords_fail_raises(self):
        from app.services import xianyu
        from config.settings import get_settings

        class _C:
            def search(self, kw):
                raise xianyu.XianyuError("网络错误")

        st = get_settings()
        with pytest.raises(xianyu.XianyuError, match="全部关键词"):
            xianyu.collect_hot(st, client=_C(), start_offset=0)


class TestAlertGateCommitOnSend:
    def test_notify_incident_rolls_back_on_send_failure(self, session, monkeypatch):
        from app.services import alert_service
        from config.settings import get_settings

        session.add(User(id=1, username="a", email="a@b.c", password_hash="x", role="admin"))
        session.commit()

        class _Feishu:
            def __init__(self, *a, **kw):
                pass

            def send(self, *a, **kw):
                return False

        monkeypatch.setattr("app.services.feishu_client.FeishuClient", _Feishu)
        st = get_settings()
        ok = alert_service.notify_incident(session, 1, "xianyu", "测试事件", "详情", settings=st)
        assert ok is False
        # 冷却门行不得落库(发送失败不烧冷却期)
        assert session.scalar(select(FeishuAlert)) is None
        # 成功路径:第二次 mock 成功 → 落库
        monkeypatch.setattr("app.services.feishu_client.FeishuClient.send", lambda self, *a, **k: True)
        assert alert_service.notify_incident(session, 1, "xianyu", "测试事件", "详情", settings=st) is True
        assert session.scalar(select(FeishuAlert)) is not None


class TestJwtExpiry:
    def test_default_is_days_not_seconds(self):
        from config.settings import get_settings

        # 604800 分钟 = 420 天的历史 bug:默认值必须是"7 天 = 10080 分钟"
        assert get_settings().jwt_expire_minutes == 10080


class TestRetryConvergence:
    def test_success_closes_old_failed_record(self, session, monkeypatch):
        import app.admin as admin_svc
        import app.db as appdb

        monkeypatch.setattr(appdb, "get_session_local", lambda: (lambda: session))
        monkeypatch.setattr(session, "close", lambda: None)

        session.add(User(id=1, username="a", email="a@b.c", password_hash="x", role="admin"))
        session.add(RunRecord(run_id="1", user_id=1, kind="weibo", status="failed",
                              started_at=datetime.now() - timedelta(hours=2), retry_count=0))
        session.commit()

        def _ok(db, uid, st=None):
            return {"platform": "weibo", "count": 5}

        from app.services import tenant as tenant_mod
        monkeypatch.setattr(tenant_mod, "run_weibo", _ok)
        out = admin_svc.retry_failed_runs(max_retry=3)
        assert out["retried"] == 1
        run = session.scalar(select(RunRecord))
        assert run.status == "recovered"  # 旧失败记录已关闭,30 分钟后不会再被重复重试
        # 第二轮:不再有 eligible failed 记录
        assert admin_svc.retry_failed_runs(max_retry=3)["retried"] == 0
