"""未定义名称(NameError)回归测试。

背景:线上反复出现「服务器内部错误(NameError)」——函数体引用了**未导入**的模型/函数
(`WechatBenchmark`、`select`、`escalate_days`),只有用户点到那个接口才炸;
更隐蔽的是被 `try/except` 吞掉的那些(maintenance 的公众号分级清理**从未真正执行过**)。
根因是同一类:缺少 import,而静态检查此前没有任何关卡。

两层防线:
1. `test_app_has_no_undefined_names` —— 提交前扫 `app/`+`config/`,新增未定义名称直接失败;
2. 行为回归 —— 直打真实 HTTP 路由 / 直调服务函数,证明此前 500 的路径已恢复。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.database import Base
from app.db import models  # noqa: F401
from app.db.models import User, WechatArticle, WechatBenchmark

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def session():
    # StaticPool + check_same_thread=False:TestClient 在子线程跑请求,内存库需共享同一连接
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    yield db
    db.close()
    engine.dispose()


# --------------------------------------------------------------------------
# 防线 1:静态扫描
# --------------------------------------------------------------------------
class _UndefinedNameCollector:
    """只收集 pyflakes 的 "undefined name" 诊断(其余风格类告警不阻塞)。"""

    def __init__(self) -> None:
        self.hits: list[str] = []

    def unexpectedError(self, filename: str, msg: str) -> None:  # noqa: N802 - pyflakes 接口
        self.hits.append(f"{filename}: {msg}")

    def syntaxError(self, filename: str, msg: str, lineno, offset, text) -> None:  # noqa: N802
        self.hits.append(f"{filename}:{lineno}: 语法错误 {msg}")

    def flake(self, message) -> None:
        if "undefined name" in str(message):
            self.hits.append(str(message))


def test_app_has_no_undefined_names() -> None:
    """运行时代码里不允许存在未定义名称——它 100% 会在某个时刻变成 NameError。

    这些名称只能是漏掉的 import;静态就能发现,不该等到线上点出 500 才暴露。
    """
    pytest.importorskip("pyflakes", reason="静态检查依赖 pyflakes(开发依赖,见 requirements.txt)")
    from pyflakes.api import checkPath

    collector = _UndefinedNameCollector()
    for pkg in ("app", "config"):
        for path in sorted((ROOT / pkg).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            checkPath(str(path), collector)

    assert not collector.hits, "发现未定义名称(会运行时报 NameError):\n" + "\n".join(collector.hits)


# --------------------------------------------------------------------------
# 防线 2:行为回归(直打真实路由)
# --------------------------------------------------------------------------
@pytest.fixture
def api(session):
    """只装配受影响路由的最小 App,并注入测试会话与当前用户。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import dashboard as dashboard_api
    from app.api import wechat as wechat_api
    from app.auth import get_current_user
    from app.db import get_db

    user = User(username="t", password_hash="x")
    session.add(user)
    session.commit()
    session.refresh(user)

    app = FastAPI()
    app.include_router(dashboard_api.router)
    app.include_router(wechat_api.router)
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_user] = lambda: user
    # 让异常变成 500 响应而非测试内抛出,便于断言"接口不再 500"
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def _benchmark(session, user_id: int = 1, active: bool = True) -> WechatBenchmark:
    row = WechatBenchmark(user_id=user_id, nickname="对标号", anchor_url="u", active=active)
    session.add(row)
    session.commit()
    return row


def test_wechat_status_returns_benchmark_count(api, session) -> None:
    """公众号监听页状态条:此接口因未导入 WechatBenchmark 直接 NameError 500。

    前端一进「公众号监听」页就会调它/还有首页概览,用户看到的即"服务器内部错误"。
    """
    _benchmark(session)
    _benchmark(session, active=False)  # 停用不计入
    user = session.scalar(select(User))
    session.add(WechatArticle(user_id=user.id, title="文", pan_types="夸克"))
    session.commit()

    resp = api.get("/api/wechat/status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["benchmarks"] == 1
    assert body["pan_articles"] == 1


def test_dashboard_overview_counts_benchmarks(api, session) -> None:
    """首页 /api/dashboard 概览同样用到 WechatBenchmark,漏导入会整体 500。"""
    _benchmark(session)
    resp = api.get("/api/dashboard")
    assert resp.status_code == 200, resp.text
    assert resp.json()["wechat_overview"]["benchmarks"] == 1


def test_rewrite_backfill_lookup_does_not_raise_nameerror(monkeypatch, api, session) -> None:
    """AI 改写补拉分支:正文不足时会查 WechatBenchmark,漏导入即 500。

    正文不足这条路应在查表后由业务逻辑拒绝(400「正文不足」),而不是 NameError(500)。
    用假 key + 不配 weread Cookie,确保不会真的发网络请求。
    """
    from config.settings import Settings

    monkeypatch.setattr("config.settings.get_settings",
                        lambda: Settings(_env_file=None, deepseek_api_key="fake-key"))

    bm = _benchmark(session)  # 无 weread_book_id → 不会触发补拉网络请求
    art = WechatArticle(user_id=1, title="短", content="太短", benchmark_id=bm.id, url="http://x")
    session.add(art)
    session.commit()

    resp = api.post(f"/api/wechat/articles/{art.id}/rewrite")
    assert resp.status_code == 400, resp.text
    assert "正文不足" in resp.text


def test_cleanup_wechat_article_tiering_actually_runs(session) -> None:
    """公众号文章分级清理此前被 `except Exception` 静默吞掉(NameError),等于从未执行。

    这里断言它**真的删了**:无盘链超 60 天删、带盘链保留 180 天。
    """
    from app.db.maintenance import cleanup_old_data
    from config.settings import Settings

    old90 = datetime.now() - timedelta(days=90)
    old200 = datetime.now() - timedelta(days=200)
    session.add_all([
        WechatArticle(user_id=1, title="无盘链旧文", pan_urls="", created_at=old90),
        WechatArticle(user_id=1, title="带盘链较新", pan_urls="https://pan", created_at=old90),
        WechatArticle(user_id=1, title="带盘链超期", pan_urls="https://pan", created_at=old200),
    ])
    session.commit()

    res = cleanup_old_data(Settings(_env_file=None, data_retention_days=30), db=session)
    assert res.get("wechat_articles_tiered") == 2
    assert session.scalar(select(WechatArticle).where(WechatArticle.title == "无盘链旧文")) is None
    assert session.scalar(select(WechatArticle).where(WechatArticle.title == "带盘链较新")) is not None
    assert session.scalar(select(WechatArticle).where(WechatArticle.title == "带盘链超期")) is None


def test_collect_failures_escalates_long_term(monkeypatch, session) -> None:
    """采集失败告警的「长期」升级分支此前引用了未定义的 escalate_days → NameError。

    该分支只在"持续失败且久未成功"时命中,平时测不到,所以一直没暴露。
    """
    from app.db.models import RunRecord
    from app.services import alert_service
    from app.services import feishu_client
    from config.settings import Settings

    # 近 24h 失败 3 次,且最近一次成功在 5 天前(> 3 天升级阈值)
    session.add(RunRecord(user_id=1, run_id="ok", kind="douhot", status="success",
                          started_at=datetime.now() - timedelta(days=5)))
    for i in range(3):
        session.add(RunRecord(user_id=1, run_id=f"f{i}", kind="douhot", status="failed",
                              started_at=datetime.now() - timedelta(hours=i + 1)))
    session.commit()

    sent: list[str] = []
    monkeypatch.setattr(feishu_client, "FeishuClient",
                        lambda w, s: type("F", (), {"send": lambda self, t: (sent.append(t), True)[1]})())

    n = alert_service.check_collect_failures(
        Settings(_env_file=None, fail_alert_threshold=3, health_escalate_days=3,
                 feishu_webhook="https://open.feishu.cn/open-apis/bot/v2/hook/test"), db=session)
    assert n == 1
    assert "🔴" in sent[0] and "长期" in sent[0] and "5 天" in sent[0]


def test_agent_learn_job_runs_for_all_users(monkeypatch, session) -> None:
    """每日 06:00 的苗头自学习此前漏导入 select → 整个作业 NameError 静默中断。"""
    import app.db as appdb
    from app.db.models import User as _User
    from app.services import agent_learning, scheduler

    for name in ("u1", "u2"):
        session.add(_User(username=name, password_hash="x"))
    session.commit()
    wanted = set(session.scalars(select(_User.id)).all())

    test_engine = sessionmaker(bind=session.get_bind())
    monkeypatch.setattr(appdb, "get_session_local", lambda: test_engine)
    called: list[int] = []
    monkeypatch.setattr(agent_learning, "backtest_and_learn",
                        lambda db, uid, settings: called.append(uid))

    scheduler._agent_learn_all()  # 修复前此处 NameError: name 'select' is not defined
    assert set(called) == wanted
