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

    def test_一次成功重试后更早的失败一起关闭_不再逐条重放(self, session, monkeypatch):
        """★★ 2026-10-19 的实际事故:10-18 留下的 4 条抖音失败被**逐条重放**。

        `recent` 每个 (user, kind) 只取最新一条 failed;只关闭当前这条的话,
        下一条更早的会在 30 分钟后成为新候选 —— 实测 04:31 / 05:01 / 05:31 / 06:01
        连续 4 次重跑(每次 8 个搜索),随后 12:10 撞上 `verify_check`。
        ⇒ 一次成功重试已经证明链路恢复,同链更早的失败记录应一起关闭。
        """
        import app.admin as admin_svc
        import app.db as appdb
        from app.services import tenant as tenant_mod

        monkeypatch.setattr(appdb, "get_session_local", lambda: (lambda: session))
        monkeypatch.setattr(session, "close", lambda: None)

        session.add(User(id=1, username="a", email="a@b.c", password_hash="x", role="admin"))
        base = datetime.now() - timedelta(hours=8)
        for i in range(3):
            session.add(RunRecord(run_id=str(i), user_id=1, kind="weibo", status="failed",
                                  started_at=base + timedelta(hours=i), retry_count=0))
        session.commit()

        calls: list[int] = []

        def _ok(db, uid, st=None):
            calls.append(1)
            return {"platform": "weibo", "count": 5}

        monkeypatch.setattr(tenant_mod, "run_weibo", _ok)
        assert admin_svc.retry_failed_runs(max_retry=3)["retried"] == 1
        assert len(calls) == 1, "只该跑一次,不许把历史失败逐条重放"
        rows = session.scalars(select(RunRecord).order_by(RunRecord.id)).all()
        assert all(r.status == "recovered" for r in rows), \
            [(r.run_id, r.status, r.detail) for r in rows]
        assert any("superseded_by_retry" in (r.detail or "") for r in rows), \
            "更早的失败要留痕(不是静默消失)"
        assert admin_svc.retry_failed_runs(max_retry=3)["retried"] == 0, "关闭后不该再被重试"


    def test_重试又失败时不许标成已恢复(self, session, monkeypatch):
        """★★ **2026-10-16 实测撞到的假成功,而它正好把自愈关掉了**。

        `run_douyin_leads` 这类**多平台** runner 会把单平台失败**吞进
        `_record_run(..., "failed")` 然后正常返回**(它自己的注释写着"单平台失败不影响其余")
        —— 而这里原来用「没抛异常」当成功 ⇒ 老记录被标成 `recovered → retry_ok`,
        而 `recovered` 的语义正是**「这条不用再试了」**。

        ⚠️ 实测代价:12:10 抖音失败 → **13:30 自动重试**又失败 → 却记成 `retry_ok`,
        看起来"自己好了"。⇒ 判据改成**看这一轮的运行记录自己怎么说**。
        """
        import app.admin as admin_svc
        import app.db as appdb

        monkeypatch.setattr(appdb, "get_session_local", lambda: (lambda: session))
        monkeypatch.setattr(session, "close", lambda: None)

        session.add(User(id=1, username="a", email="a@b.c", password_hash="x", role="admin"))
        session.add(RunRecord(run_id="1", user_id=1, kind="weibo", status="failed",
                              started_at=datetime.now() - timedelta(hours=2), retry_count=0))
        session.commit()

        def _swallow(db, uid, st=None):
            """替身与真身**同形**:自己记一条 failed,然后**正常返回**(不抛)。"""
            db.add(RunRecord(run_id="2", user_id=1, kind="weibo", status="failed",
                             detail="又炸了", started_at=datetime.now(), retry_count=0))
            db.commit()
            return {"platform": "weibo", "count": 0}

        from app.services import tenant as tenant_mod
        monkeypatch.setattr(tenant_mod, "run_weibo", _swallow)
        assert admin_svc.retry_failed_runs(max_retry=3)["retried"] == 0
        old = session.scalars(select(RunRecord).where(RunRecord.run_id == "1")).one()
        assert old.status == "failed", f"重试明明又失败了,却标成了 {old.status}"
        assert "retry_ok" not in (old.detail or ""), old.detail


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




class Test抖音验证冷却期不许重试:
    """★ 2026-10-17:用户收到「**douyin_leads 近 24h 失败 9/24 次(连续 2 轮)**」告警。

    查原文判定是 `search_nil_info.search_nil_type=verify_check`(**平台要求过验证**),
    **不是**登录态过期 ⇒ 那条告警自己给的两个动作里,该做的是 **②「等 + 别再打」**。
    而 `retry_failed_runs` 每 30 分钟就把这个作业重跑一遍 —— 协议路在冷却里不发请求,
    **但浏览器兜底那一跳照样打 8 次抖音搜索**。
    ⚠️ 拿真实运行记录量的代价:那 6 次重试成功一共只换来 **3 个新群 / 0 条新搬**,
    却花了约 **72 次额外搜索** ⇒ 冷却期内不该重试。
    """

    def _setup(self, session, monkeypatch, *, cooldown: bool, switch: bool = True,
               detail: str = "douyin: 8 个关键词全部无结果"):
        import app.admin as admin_svc
        import app.db as appdb
        from app.services import douyin_protocol_source as dps

        monkeypatch.setattr(appdb, "get_session_local", lambda: (lambda: session))
        monkeypatch.setattr(session, "close", lambda: None)
        monkeypatch.setattr(dps, "in_verify_cooldown", lambda *a, **k: cooldown)

        class _S:
            douyin_retry_skip_in_cooldown = switch
            douyin_verify_cooldown_hours = 6.0

        monkeypatch.setattr("config.settings.get_settings", lambda: _S())
        session.add(User(id=1, username="a", email="a@b.c", password_hash="x", role="admin"))
        session.add(RunRecord(run_id="1", user_id=1, kind="douyin_leads", status="failed",
                              detail=detail, retry_count=0,
                              started_at=datetime.now() - timedelta(hours=2)))
        session.commit()
        calls: list[str] = []

        def _runner(db, uid, st=None, **kw):
            calls.append("ran")
            return {"platform": "douyin", "count": 0}

        from app.services import douyin_leads as dl
        monkeypatch.setattr(dl, "run_douyin_leads", _runner)
        return admin_svc, calls

    def test_冷却期内不重试(self, session, monkeypatch) -> None:
        admin_svc, calls = self._setup(session, monkeypatch, cooldown=True)
        assert admin_svc.retry_failed_runs(max_retry=3)["retried"] == 0
        assert calls == [], "冷却期内不该再打那 8 次搜索"
        run = session.scalar(select(RunRecord))
        assert run.status == "failed", "跳过 ≠ 恢复:记录必须留着,冷却结束还能再试"

    def test_风控空结果永不自动重试(self, session, monkeypatch) -> None:
        """★ 2026-10-19:风控空结果**连冷却结束后也不重试**。

        18:30 的自动重试把冷却推到 00:30,而 20:10 的定点轮因为"探针已被用掉"只能跳过。
        ⇒ 这类失败交给 4h 定点轮,自动重试不许偷走探针。
        """
        admin_svc, calls = self._setup(session, monkeypatch, cooldown=False)
        assert admin_svc.retry_failed_runs(max_retry=3)["retried"] == 0
        assert calls == [], "风控/空结果不是瞬时故障,重试只会再撞一次"

    def test_不在冷却期就照旧重试(self, session, monkeypatch) -> None:
        """非风控类失败(如 Playwright 退出码)仍按指数退避正常重试。"""
        admin_svc, calls = self._setup(
            session, monkeypatch, cooldown=False,
            detail="douyin 退出码 1:playwright Page.goto timeout")
        admin_svc.retry_failed_runs(max_retry=3)
        assert calls == ["ran"], "冷却结束了还不重试,就等于把这条链放弃了"

    def test_开关关掉就照旧重试(self, session, monkeypatch) -> None:
        admin_svc, calls = self._setup(
            session, monkeypatch, cooldown=True, switch=False,
            detail="douyin 退出码 1:playwright Page.goto timeout")
        admin_svc.retry_failed_runs(max_retry=3)
        assert calls == ["ran"], "可回退:配置置 False 就该恢复旧行为"
