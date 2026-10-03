"""数据源健康中心单测:三态判定(HEALTHY/DEGRADED/CIRCUIT_OPEN)。"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db.models import GroupMember, RunRecord, User, UserCookie, UserSchedule, WeiboHotItem, WechatArticle
from app.services import health


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(User(id=1, username="a", email="a@b.c", password_hash="x"))
    db.commit()
    yield db
    db.close()


def _settings():
    import types
    return types.SimpleNamespace(xianyu_cooldown_minutes=30, xianyu_proxy_url="")


def test_healthy_when_recent_success(session):
    now = datetime.now()
    session.add(UserSchedule(user_id=1, section="weibo", interval_minutes=60, enabled=True))
    session.add(UserCookie(user_id=1, platform="weibo",
                           cookie=__import__("app.security", fromlist=["encrypt_cookie"]).encrypt_cookie("ck=1")))
    session.add(WeiboHotItem(user_id=1, title="热点词样本", heat=100, rank=1, captured_at=now))
    session.add(RunRecord(run_id="1", user_id=1, kind="weibo", status="success", started_at=now))
    session.commit()
    rows = health.source_health(session, 1, _settings())
    weibo = next(r for r in rows if r["section"] == "weibo")
    assert weibo["health"] == "HEALTHY" and not weibo["problems"]


def test_open_when_cookie_missing(session):
    now = datetime.now()
    session.add(UserSchedule(user_id=1, section="weibo", interval_minutes=60, enabled=True))
    session.add(RunRecord(run_id="1", user_id=1, kind="weibo", status="success", started_at=now))
    session.add(WeiboHotItem(user_id=1, title="热点词样本", heat=100, rank=1, captured_at=now))
    session.commit()
    weibo = next(r for r in health.source_health(session, 1, _settings()) if r["section"] == "weibo")
    assert weibo["health"] == "CIRCUIT_OPEN" and any("Cookie" in p for p in weibo["problems"])


def test_degraded_on_freshness_and_fails(session):
    now = datetime.now()
    session.add(UserSchedule(user_id=1, section="weibo", interval_minutes=60, enabled=True))
    session.add(UserCookie(user_id=1, platform="weibo",
                           cookie=__import__("app.security", fromlist=["encrypt_cookie"]).encrypt_cookie("ck=1")))
    session.add(WeiboHotItem(user_id=1, title="热点词样本", heat=100, rank=1,
                             captured_at=now - timedelta(hours=10)))  # 间隔 1h,停滞 10h
    for i in range(4):  # 24h 失败 4 次(≥3)
        session.add(RunRecord(run_id=str(i), user_id=1, kind="weibo", status="failed",
                              started_at=now - timedelta(hours=i)))
    session.commit()
    weibo = next(r for r in health.source_health(session, 1, _settings()) if r["section"] == "weibo")
    assert weibo["health"] == "DEGRADED"
    assert any("停滞" in p for p in weibo["problems"]) and any("失败" in p for p in weibo["problems"])


def test_circuit_open_on_xianyu_cooldown(session):
    now = datetime.now()
    session.add(UserSchedule(user_id=1, section="xianyu", interval_minutes=60, enabled=True))
    session.add(UserCookie(user_id=1, platform="goofish",
                           cookie=__import__("app.security", fromlist=["encrypt_cookie"]).encrypt_cookie("ck=1")))
    session.add(RunRecord(run_id="1", user_id=1, kind="xianyu", status="failed",
                          detail="XianyuVerify: 滑块", started_at=now))
    session.commit()
    xy = next(r for r in health.source_health(session, 1, _settings()) if r["section"] == "xianyu")
    assert xy["health"] == "CIRCUIT_OPEN" and any("冷却" in p for p in xy["problems"])


def test_wechat_stats_use_listen_sync_kinds(session):
    """公众号监听按 wechat_listen/wechat_sync 落库,健康度须按细粒度 kind 统计而非 section。

    (修前 source_health 用 kind=="wechat" 查 → 最后成功/失败次数恒空,公众号板块假装"从未运行")
    """
    now = datetime.now()
    session.add(UserSchedule(user_id=1, section="wechat", interval_minutes=120, enabled=True))
    session.add(UserCookie(user_id=1, platform="weread",
                           cookie=__import__("app.security", fromlist=["encrypt_cookie"]).encrypt_cookie("ck=1")))
    session.add(WechatArticle(user_id=1, title="盘文 夸克", url="https://mp.weixin.qq.com/s/x",
                              source="listen", created_at=now))
    session.add(RunRecord(run_id="1", user_id=1, kind="wechat_listen", status="success", started_at=now))
    session.add(RunRecord(run_id="2", user_id=1, kind="wechat_sync", status="failed", started_at=now))
    session.commit()
    wx = next(r for r in health.source_health(session, 1, _settings()) if r["section"] == "wechat")
    assert wx["last_success_at"] is not None   # 命中 wechat_listen 成功记录
    assert wx["fails_24h"] == 1                # 命中 wechat_sync 失败记录


def test_check_optional_containers(monkeypatch) -> None:
    """可选容器探活(v2.8.0):在线返回空;挂了列出名字;不含未配置的 WeRSS。"""
    from app.services import health as h

    class _Resp:
        def __init__(self, code):
            self.status_code = code

    # 都在线
    monkeypatch.setattr(h, "__name__", h.__name__)
    import curl_cffi.requests as creq
    monkeypatch.setattr(creq, "get", lambda *a, **kw: _Resp(200))
    from config.settings import Settings
    st = Settings(_env_file=None, wechat_werss_url="http://127.0.0.1:8001")
    assert h.check_optional_containers(st) == []

    # newsnow 挂(连接异常)
    def _boom(base, **kw):
        if "4444" in base:
            raise ConnectionError("refused")
        return _Resp(200)
    monkeypatch.setattr(creq, "get", _boom)
    down = h.check_optional_containers(st)
    assert down == ["newsnow 热榜源"]

    # 未配 WeRSS → 不探它
    st2 = Settings(_env_file=None, wechat_werss_url="")
    down2 = h.check_optional_containers(st2)
    assert down2 == ["newsnow 热榜源"]


def test_pan_section_uses_xunlei_cred_and_resource_freshness(session) -> None:
    """新增的「网盘资源」板块(2026-10-02):凭据看**迅雷**、数据看**三条资源链任一**。

    此前这些作业(群采集/扫盘/线索/发现/热度)**一条运行记录都不写**,健康页完全看不到它们
    —— 静默失败没人知道。这个板块就是给它们开的入口。
    """
    from app.db.models import XunleiGroupShare

    pan = next(r for r in health.source_health(session, 1, _settings())
               if r["section"] == "pan")
    assert pan["label"] == "网盘资源"
    assert pan["health"] == "CIRCUIT_OPEN"                 # 没凭据 → 断路
    assert any("Cookie" in p for p in pan["problems"])

    now = datetime.now()
    from app.security import encrypt_cookie

    session.add(UserCookie(user_id=1, platform="xunlei",
                           cookie=encrypt_cookie('{"refresh_token":"r"}')))
    session.add(XunleiGroupShare(user_id=1, group_id="g", share_id="s1", title="资源甲",
                                 synced_at=now))
    session.add(RunRecord(run_id="1", user_id=1, kind="xunlei_group", status="success",
                          started_at=now))
    session.commit()
    pan = next(r for r in health.source_health(session, 1, _settings())
               if r["section"] == "pan")
    assert pan["health"] == "HEALTHY", pan
    assert pan["last_success_age_h"] is not None           # 运行记录认到了


def test_sections_filtered_by_scheduler_role(session) -> None:
    """**分体部署的现实**(2026-10-02 修):作业本来就按角色过滤(本机 wechat 角色
    **不跑**微博/抖音/百度),可健康页却在报它们"数据停滞 80 小时" —— 天天假警报、
    白耗注意力。本实例不跑的板块一律标 N/A 且**不计问题**。
    """
    import types

    st = types.SimpleNamespace(xianyu_cooldown_minutes=30, xianyu_proxy_url="",
                               scheduler_role="wechat")
    out = {r["section"]: r for r in health.source_health(session, 1, st)}
    assert out["weibo"]["health"] == "N/A" and out["douhot"]["health"] == "N/A"
    assert out["baidu"]["health"] == "N/A"
    assert any("本实例不跑" in p for p in out["weibo"]["problems"])
    assert out["wechat"]["health"] != "N/A"      # 公众号是本实例的活,照常体检
    assert out["pan"]["health"] != "N/A"         # 网盘资源也归 wechat 侧

    # 热点实例反过来:公众号/网盘标 N/A,微博照常
    st2 = types.SimpleNamespace(xianyu_cooldown_minutes=30, xianyu_proxy_url="",
                                scheduler_role="hotspot")
    out2 = {r["section"]: r for r in health.source_health(session, 1, st2)}
    assert out2["wechat"]["health"] == "N/A" and out2["weibo"]["health"] != "N/A"


# ---------------------------------------------------------------- 跨实例可见(2026-10-03)

def test_peer_status_unconfigured_is_noop() -> None:
    """没配 `PEER_HEALTH_URL` 就**不探、不报错** —— 默认零行为,不给出网请求。"""
    import types
    out = health.peer_status(types.SimpleNamespace(peer_health_url=""))
    assert out == {"configured": False, "online": False, "url": ""}


def test_peer_status_reports_online_and_reads_version(monkeypatch) -> None:
    """探对端**已有的公开 `/healthz`**(不新增暴露面),把"远端整机失联"与"源没数据"分开。"""
    import types

    import requests

    class _R:
        status_code = 200

        def json(self):
            return {"status": "ok", "version": "2.14.0", "time": "2026-10-03T14:33:24"}

    monkeypatch.setattr(requests, "get", lambda *a, **k: _R())
    out = health.peer_status(types.SimpleNamespace(peer_health_url="https://peer.example/"))
    assert out["configured"] and out["online"] and out["version"] == "2.14.0"
    assert out["url"] == "https://peer.example"          # 结尾斜杠要去掉,别拼成 //healthz


def test_peer_status_offline_on_error(monkeypatch) -> None:
    """对端挂了/超时 → `online=False` 并带原因(**不抛** —— 探活失败本身就是结论)。"""
    import types

    import requests

    def _boom(*a, **k):
        raise requests.ConnectionError("connection refused")

    monkeypatch.setattr(requests, "get", _boom)
    out = health.peer_status(types.SimpleNamespace(peer_health_url="https://peer.example"))
    assert out["configured"] and out["online"] is False and "ConnectionError" in out["error"]


def test_health_card_titles_and_counts_bad_sources(session) -> None:
    """健康卡:全部正常→绿;有熔断/24h失败≥3→红并计数(远端每天推这张到管理员群)。"""
    import types

    now = datetime.now()
    for i in range(3):
        session.add(RunRecord(user_id=1, kind="weibo", status="failed", run_id=f"r{i}",
                              started_at=now - timedelta(minutes=i)))
    session.commit()
    st = types.SimpleNamespace(xianyu_cooldown_minutes=30, xianyu_proxy_url="",
                               scheduler_role="hotspot")
    card = health.health_card(st, session, 1)
    title = card["header"]["title"]["content"]
    assert "hotspot" in title and "需处理" in title and card["header"]["template"] == "red"


def test_health_push_skips_without_admin_group(monkeypatch) -> None:
    """⚠️ **没配管理员群就跳过,不回落客户主群** —— 采集源健康是运维噪音,
    推进客户群是事故(`webhook_for('admin')` 的回落行为在这里不能用)。"""
    import types

    out = health.health_push_tick(types.SimpleNamespace(
        health_push_enabled=True, feishu_webhook_admin="", health_push_cron="20 9 * * *"))
    assert out == 0


def test_health_card_renders_missing_age_as_dash(session) -> None:
    """数据龄为 None 的板块**不能渲染成 `Noneh`**。

    实测(2026-10-03):远端第一张卡里出现过 `· 数据 Noneh` —— 那些是"本实例不跑"的板块
    (远端卡里是闲鱼/公众号/网盘资源),数据龄本来就该是空。残字很显眼,直接钉住。
    """
    import types

    st = types.SimpleNamespace(xianyu_cooldown_minutes=30, xianyu_proxy_url="",
                               scheduler_role="hotspot")
    card = health.health_card(st, session, 1)
    body = card["elements"][0]["text"]["content"]
    assert "Noneh" not in body
    assert "—" in body                      # 没有数据龄的板块渲染成破折号
