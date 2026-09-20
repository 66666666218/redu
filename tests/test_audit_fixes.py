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

    def test_redirect_to_internal_refused(self, monkeypatch):
        """公开首跳 302 跳到内网元数据端点:逐跳重校验须拦下,绝不向内网发起第二次请求。"""
        from app.services import wechat_monitor as wm
        from app.utils import net

        def fake_assert(u):
            if any(x in u for x in ("169.254", "127.0.0.1", "10.0.0", "192.168")):
                raise net.UnsafeUrlError(u)

        monkeypatch.setattr(net, "assert_public_url", fake_assert)

        state = {"n": 0}

        class _R:
            def __init__(self, status, headers=None, text=""):
                self.status_code, self.headers, self.text = status, headers or {}, text

        def fake_get(url, timeout, headers, allow_redirects=True):
            state["n"] += 1
            assert "169.254" not in url, "向内网发起了请求(逐跳守卫未生效)"
            return _R(302, {"Location": "http://169.254.169.254/latest/meta-data/"})

        monkeypatch.setattr(wm.requests, "get", fake_get)
        assert wm.fetch_article_content("http://evil.example/s") == ""
        assert wm.extract_article_meta("http://evil.example/s") == {}
        assert state["n"] == 2  # 两函数各发一次首跳;302 内网目标被守卫拒,无第三跳


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


class TestFixedTimeDigestHonest:
    def test_send_failure_does_not_burn_daily_flag(self, session, monkeypatch):
        """定时总结发送失败必须诚实:不置 last_alert_at、不计 sent(与 evaluate 同款)。"""
        from app.db.models import AlertRule
        from app.services import alert_service

        session.add(User(id=1, username="u", password_hash="x"))
        rule = AlertRule(user_id=1, section="weibo", rule_type="fixed_time",
                         alert_time=datetime.now().strftime("%H:%M"), enabled=True, last_alert_at=None)
        session.add(rule)
        session.commit()

        monkeypatch.setattr(alert_service, "_build_digest", lambda db, uid, sec, settings: "digest")

        class _N:
            def __init__(self, ok):
                self.ok = ok

            def send(self, subject, body):
                return self.ok

        # 发送失败:sent=0 且 last_alert_at 仍为空(当天不被静默标记为"已发")
        monkeypatch.setattr(alert_service, "get_user_notifier", lambda user, settings: _N(False))
        assert alert_service.run_fixed_time_digests(db=session, settings=object()) == 0
        assert session.get(AlertRule, rule.id).last_alert_at is None

        # 发送成功:计 1 并落 last_alert_at
        monkeypatch.setattr(alert_service, "get_user_notifier", lambda user, settings: _N(True))
        assert alert_service.run_fixed_time_digests(db=session, settings=object()) == 1
        assert session.get(AlertRule, rule.id).last_alert_at is not None

    def test_disabled_user_digest_skipped(self, session, monkeypatch):
        """管理员禁用用户后,其 fixed_time 定时总结不再派发(封禁即停推)。"""
        from app.db.models import AlertRule
        from app.services import alert_service

        hhmm = datetime.now().strftime("%H:%M")
        session.add(User(id=1, username="on", password_hash="x"))
        session.add(User(id=2, username="off", password_hash="x"))
        session.add(AlertRule(user_id=1, section="weibo", rule_type="fixed_time",
                              alert_time=hhmm, enabled=True))
        session.add(AlertRule(user_id=2, section="weibo", rule_type="fixed_time",
                              alert_time=hhmm, enabled=True))
        session.get(User, 2).enabled = False
        session.commit()

        monkeypatch.setattr(alert_service, "_build_digest", lambda db, uid, sec, settings: "digest")

        class _Send:
            def send(self, subject, body):
                return True

        monkeypatch.setattr(alert_service, "get_user_notifier", lambda user, settings: _Send())
        # 仅启用用户(uid=1)派发,禁用用户(uid=2)跳过
        assert alert_service.run_fixed_time_digests(db=session, settings=object()) == 1


class TestProxyCredRedaction:
    """带鉴权代理的连接异常 repr 含 http://user:pass@host,不得经 RunRecord.detail 泄露。"""

    def test_redact_masks_userinfo_keeps_host(self):
        from app.services.tenant_base import _redact_proxy_creds

        out = _redact_proxy_creds("请求失败: ProxyError at http://u1:secret@1.2.3.4:8080 reset")
        assert "secret" not in out and "u1:" not in out
        assert "1.2.3.4:8080" in out  # 出口 IP/端口保留给运维定位

    def test_redact_leaves_plain_detail(self):
        from app.services.tenant_base import _redact_proxy_creds

        s = "XianyuVerify: 闲鱼人机验证(滑块),需人工处理:FAIL_SYS_USER_VALIDATE"
        assert _redact_proxy_creds(s) == s  # 无 ://user@ 段,原样(冷却匹配依赖此文案)

    def test_record_run_stores_masked_detail(self, session):
        from app.services.tenant_base import _record_run

        _record_run(session, 1, "weibo", "failed",
                    "ConnectionError: Cannot connect to proxy http://proxyuser:proxypass@9.9.9.9:3128")
        session.commit()
        row = session.scalar(select(RunRecord).where(RunRecord.kind == "weibo"))
        assert "proxypass" not in row.detail and "proxyuser:" not in row.detail
        assert "9.9.9.9:3128" in row.detail

    def test_douhot_network_error_message_no_creds(self, monkeypatch):
        """douhot_client:代理连接异常只把 exc 送日志,DouhotError 消息仅带类型名。"""
        import requests as rq

        from app.services import douhot_client as dc

        client = dc.DouhotClient.__new__(dc.DouhotClient)
        client.proxies = {"http": "http://u:p@1.1.1.1:80", "https": "http://u:p@1.1.1.1:80"}
        client._settings = None  # 重试分支不自建代理(避免触碰 get_proxies),沿用上面的 proxies
        client.timeout = 1

        def boom(*a, **kw):
            raise rq.ConnectionError("Cannot connect to proxy http://u:p@1.1.1.1:80")

        class _Sess:
            def request(self, *a, **kw):
                return boom()

        client.session = _Sess()
        with pytest.raises(dc.DouhotError) as ei:
            client._call("POST", "/x", "ref", {})
        assert ":p@" not in str(ei.value) and "ConnectionError" in str(ei.value)

    def test_collect_http_500_masks_proxy_creds(self, monkeypatch):
        """采集 HTTP 500 响应面:requests 代理连接异常含 http://user:pass@host,
        经 collect() 回给浏览器前必须遮蔽(RunRecord 咽喉点只护 DB 面,不覆盖此面)。"""
        import requests as rq
        from fastapi import HTTPException

        from app.api import collect as collect_api
        from app.services import tenant

        def boom(*a, **kw):
            raise rq.ConnectionError(
                "Cannot connect to proxy http://pu:psecret@9.9.9.9:3128")

        monkeypatch.setattr(tenant, "run_xianyu", boom)

        class _User:
            id = 1

        with pytest.raises(HTTPException) as ei:
            collect_api.collect("xianyu", _User(), db=None)  # type: ignore[arg-type]
        detail = ei.value.detail
        assert "psecret" not in detail and "pu:" not in detail
        assert "9.9.9.9:3128" in detail  # 出口 IP/端口保留便于排障

    def test_retry_run_msg_masks_proxy_creds(self, session, monkeypatch):
        """admin.retry_run:采集异常含代理凭证时,msg(回管理端 + 落 AdminLog)须遮蔽。"""
        import requests as rq

        from app import admin as admin_svc
        from app.services import tenant

        run = RunRecord(user_id=1, kind="xianyu", status="failed", run_id="r1")
        session.add(run)
        session.commit()

        def boom(*a, **kw):
            raise rq.ConnectionError("Cannot connect to proxy http://pu:psecret@9.9.9.9:3128")

        monkeypatch.setattr(tenant, "run_xianyu", boom)
        res = admin_svc.retry_run(session, str(run.id), settings=object())
        assert res["ok"] is False
        assert "psecret" not in res["msg"] and "pu:" not in res["msg"]
        assert "9.9.9.9:3128" in res["msg"]


