"""阅读数**断供**的三处「假绿/静默」修复单测(2026-10-06)。

## 用户的提问
「**为什么不带阅读数了**」—— 查下来阅读数从 09-28 起实际上全断(近 3 天 163 篇里只有 2 篇),
而**每一层监控都在放行**:

1. 体检的 `微信读书(公众号阅读数)` 报 **🟢 验活 ✓(142 个号)** —— 但它验的是 `shelf`,
   **根本不碰列表接口**,而阅读数走的正是列表接口 ⇒ **一个绿灯去管它管不着的事**;
2. `公众号·精确阅读数` 报 **🟡 覆盖率 1%,偏低** —— 判红的条件是"**恰好一篇都没有**",
   侥幸漏进来 2 篇就掉到黄档。**一个会长期挂在黄档的指标等于没有指标**;
3. App 兜底路(10-05 为"把断了一周的阅读数接回来"而加)**一次都没成功过**:
   它每轮都"自愈"一次,而重取回来的是**同一个 token**,重试必然同样失败 ——
   最后那次失败还被上层的宽 `except` 吞成一行 **DEBUG**(生产日志级别是 INFO)。
"""
import os
import sys
from datetime import datetime, timedelta

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.db.database import Base  # noqa: E402
from app.db import models  # noqa: E402,F401
from app.db.models import WechatArticle  # noqa: E402
from app.services import chain_health as ch  # noqa: E402
from app.services import wechat_monitor  # noqa: E402,F401   # 先导门面
from app.services.weread_app_client import (  # noqa: E402
    WereadAppAuthError, WereadAppClient,
)


@pytest.fixture
def session():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)()
    yield db
    db.close()


def _seed(session, total: int, withnum: int) -> None:
    now = datetime.now()
    for i in range(total):
        session.add(WechatArticle(
            user_id=1, author="某号", title=f"t{i}", url=f"https://mp.weixin.qq.com/s/{i}",
            read_num=1234 if i < withnum else 0, created_at=now - timedelta(hours=1)))
    session.commit()


class TestCoverageIsRedWhenNearlyZero:
    def test_侥幸漏进两篇也要判红(self, session) -> None:
        """★ **这条就是 8 天没人发现的直接原因**:163 篇里 2 篇 → 覆盖率 1% → 原来报 🟡「偏低」。"""
        _seed(session, 163, 2)
        row = ch.check_read_num_coverage(session)[0]
        assert row["level"] == ch.RED, (
            f"覆盖率 1% 报成了 {row['level']} —— 侥幸漏进来两篇就掉到黄档,"
            "而黄档是最容易被忽略的那一档;用户就是这么被瞒了 8 天")
        assert "换新会话也没用" in row["detail"], "判红要直接说清两条路和怎么修"

    def test_一篇都没有当然判红(self, session) -> None:
        _seed(session, 100, 0)
        assert ch.check_read_num_coverage(session)[0]["level"] == ch.RED

    def test_四成覆盖是绿(self, session) -> None:
        """反向:窗口轮转正常时窗口外的号本来就该显示"—",别把正常的绿成黄。"""
        _seed(session, 100, 40)
        assert ch.check_read_num_coverage(session)[0]["level"] == ch.GREEN

    def test_一成是黄(self, session) -> None:
        _seed(session, 100, 10)
        assert ch.check_read_num_coverage(session)[0]["level"] == ch.YELLOW


class TestCredentialRowDoesNotOversell:
    def test_微信读书那行明说只验书架(self, session) -> None:
        """★ **假绿**:它写着"公众号阅读数"、报着 🟢,而验的只是 `shelf`。"""
        rows = ch.check_credentials(session)
        w = [r for r in rows if "微信读书" in r["name"]]
        assert w, rows
        assert "阅读数" not in w[0]["name"], (
            f"标签 {w[0]['name']!r} 让人以为这一行保证阅读数是好的 —— 而它验的只是书架")
        assert "只验书架" in w[0]["detail"], "必须在 detail 里点名它**没有**覆盖列表接口"


class _FakeFetch:
    """把 `_fetch` 换成可控桩:按脚本抛错或返回。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def __call__(self, book_id, count, offset):
        self.calls += 1
        item = self.script.pop(0) if self.script else RuntimeError("没脚本了")
        if isinstance(item, Exception):
            raise item
        return item


class TestAppSelfHealIsNotAFreePass:
    def test_重取到同一个token就不该重试(self, monkeypatch) -> None:
        """★ **空转**:模拟器里的值没变,重取必然拿到同一个 —— 重试是白费,还假装"自愈过"。"""
        c = WereadAppClient("TOK", "439862397", on_auth_error=lambda: ("TOK", "439862397"))
        calls = _FakeFetch([WereadAppAuthError("-2012 登录超时")])
        monkeypatch.setattr(c, "_fetch", calls)

        with pytest.raises(WereadAppAuthError) as ei:
            c.articles("MP_WXS_1")

        assert calls.calls == 1, "token 没变还去重试,就是空转"
        assert "重新登录" in str(ei.value), f"要给**人要做的那一步**:{ei.value}"
        assert c.refreshed is False, "没自愈成,不该标成自愈过"

    def test_换到新token才重试并且还失败要喊出来(self, monkeypatch, caplog) -> None:
        c = WereadAppClient("OLD", "439862397", on_auth_error=lambda: ("NEW", "439862397"))
        calls = _FakeFetch([WereadAppAuthError("-2012 登录超时"),
                            WereadAppAuthError("-2012 登录超时")])
        monkeypatch.setattr(c, "_fetch", calls)

        import logging
        with caplog.at_level(logging.WARNING):
            with pytest.raises(WereadAppAuthError):
                c.articles("MP_WXS_1")

        assert calls.calls == 2, "换了新 token 应当重试一次"
        assert c.refreshed is True
        assert any("仍然" in r.message for r in caplog.records), (
            "重试仍失败必须留痕 —— 此前它被上层的宽 except 吞成 DEBUG,"
            "生产日志级别是 INFO ⇒ 这条路全断在日志里一个字都没有")

    def test_重试成功就正常返回(self, monkeypatch) -> None:
        c = WereadAppClient("OLD", "439862397", on_auth_error=lambda: ("NEW", "439862397"))
        calls = _FakeFetch([WereadAppAuthError("-2012"), [{"readNum": 7}]])
        monkeypatch.setattr(c, "_fetch", calls)
        assert c.articles("MP_WXS_1") == [{"readNum": 7}]
        assert c.refreshed is True


class TestWhichAppFailuresPageAHuman:
    """★ **只有"等多久都不会自己好"的才打扰人**(与 `chain_health._classify_source_error` 同一条纪律)。

    限流/网络抖动推飞书,只会训练人忽略告警 —— 而告警一旦被忽略,
    真出事那次也就没人看了(本仓反复吃这个亏)。
    """

    def _f(self, exc):
        import importlib
        m = importlib.import_module("app.services.wechat._listen")
        return m._app_auth_needs_human(exc)

    def test_登录超时要人重登(self) -> None:
        assert self._f(RuntimeError("App 登录态问题:-2012 登录超时 ⇒ 重新取 token")) is True

    def test_同token空转的报错也要人重登(self) -> None:
        """这正是 2026-10-06 那条:**模拟器里登录态失效**,修法是"在雷电里打开 App"。"""
        assert self._f(RuntimeError(
            "重取到的 token 与原来**完全相同**(8 字符)⇒ **模拟器里微信读书的登录态已失效**")) is True

    def test_限流不该打扰人(self) -> None:
        """⚠️ 反向:限流会自愈,推飞书就是噪音。"""
        assert self._f(RuntimeError("appmsgpublish 频率限制(200013)")) is False
        assert self._f(RuntimeError("微信读书接口不可用/被拦截(-2041)")) is False

    def test_网络抖动不该打扰人(self) -> None:
        assert self._f(TimeoutError("Connection timed out")) is False
