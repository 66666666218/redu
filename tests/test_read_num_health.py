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


# ---------------------------------------------------------------------------
# ★★ 2026-10-08:单日全断的**一轮检测器**(覆盖率那条结构上抓不到)
# ---------------------------------------------------------------------------


def _run(session, detail: str, n: int = 1, status: str = "success") -> None:
    from app.db.models import RunRecord

    now = datetime.now()
    for i in range(n):
        session.add(RunRecord(user_id=1, run_id=f"r{n}-{i}-{detail[:8]}", kind="wechat_listen",
                              status=status, detail=detail,
                              started_at=now - timedelta(minutes=i)))
    session.commit()


class TestListRotationIsTheEarlyDetector:
    """★★ 为什么必须有这一条(而不是继续加宽覆盖率那条的窗口):

    `check_read_num_coverage` 判的是**近 3 天的覆盖率**。而**单日全断**时覆盖率还有约 **31%**
    (前两天的好数据在稀释它)⇒ 最多报黄,要衰减到 5% 红线得**三天**。
    2026-10-08 那次事故(轮转窗口被"没有 bookId 的号"占满 ⇒ 两条路**一次都没被调用过**)
    **恰恰是「当天全断、三天后才红」** —— 那个检测窗**结构上**抓不到它。

    ⇒ 换判据:**不看产出,看机制**。运行记录里那四个数就是机制本身,**一轮就能判**。
    """

    def test_谁都没问过要判红(self, session) -> None:
        """★ 事故当天的真实指纹。"""
        _run(session, "accounts=187 new=1 weread_list(ok=0 app=0 off=0 off_with_new=1 skipped=79)")
        row = ch.check_weread_list_rotation(session)[0]
        assert row["level"] == ch.RED, f"这个形状是「窗口坏了」,必须红,实际 {row['level']}"
        assert "谁都没问过" in row["detail"] and "_list_window" in row["detail"], row["detail"]

    def test_被额度挡是另一种红_且说明处置不同(self, session) -> None:
        """⚠️ 两种红的**修法完全不同**:窗口坏了要改代码;被挡是外因,改代码没用。"""
        _run(session, "weread_list(ok=0 app=0 off=25 off_with_new=0 skipped=0)")
        row = ch.check_weread_list_rotation(session)[0]
        assert row["level"] == ch.RED and "额度" in row["detail"], row["detail"]
        assert "窗口坏了" in row["detail"], "要说清它和「窗口坏了」不是一回事"

    def test_有一条路在跑就是绿(self, session) -> None:
        """反面对照 —— 不然它永远红,报告会被人忽略(本仓反复讲过的教训)。"""
        from app.db.models import RunRecord

        for detail in ("weread_list(ok=3 app=0 off=0 off_with_new=0 skipped=22)",
                       "weread_list(ok=0 app=5 off=0 off_with_new=0 skipped=20)"):
            session.query(RunRecord).delete()
            session.commit()
            _run(session, detail)
            row = ch.check_weread_list_rotation(session)[0]
            assert row["level"] == ch.GREEN, (detail, row)

    def test_记账格式变了不许判绿(self, session) -> None:
        """⚠️ 指标自己瞎掉时**不许绿**:格式飘了(本仓飘过)就该说"无从判断"。"""
        _run(session, "accounts=187 new=1 只说了新文数,没有 weread_list 记账")
        row = ch.check_weread_list_rotation(session)[0]
        assert row["level"] == ch.YELLOW and "记账" in row["detail"], row

    def test_最近没有轮次时黄而不是红(self, session) -> None:
        row = ch.check_weread_list_rotation(session)[0]
        assert row["level"] == ch.YELLOW, row


class Test两行体检不许给互相矛盾的动作:
    """★★★ 2026-10-15 真实事故:**同一个断供,两行体检给了相反的动作。**

    用户问「微信阅读还是没好没有阅读」时实跑体检,看到的是:

      · `公众号·精确阅读数`      🟡 —— 让人**去模拟器里重登微信读书**(`scripts/weread_app_login.py`);
      · `公众号·阅读数列表轮转`  🔴 —— **该等、别改代码**。

    而当时两条路都是 `-2041`(**时限性**的限,已自愈过三次)——
    ⇒ **那一行「去重登」的动作是错的**:重登修不了,还会把窗口推后
    (本仓 2026-10-14 正是为此写下「中途反复探测只会把窗口推后」)。

    ⇒ 纪律:**一个结论只能有一个出处**(与 `xhs_accounts.classify` 那条同源)。
    判据抽到 `chain_health._weread_list_family`,两行都从它取动作。
    """

    def _today(self, session) -> None:
        """今天库里的真实形状:两条路都被挡,而产出端一篇带阅读数的都没有。"""
        _run(session, "accounts=129 new=13 failed=0 weread_list(ok=0 app=0 app_fail=2 "
                      "app_blocked=25 off=1 off_with_new=13 skipped=65)")
        _seed(session, 100, 0)

    def test_被挡时两行都要说_等_且都不许叫人去重登(self, session) -> None:
        self._today(session)
        cov = ch.check_read_num_coverage(session)[0]
        rot = ch.check_weread_list_rotation(session)[0]
        assert rot["level"] == ch.RED and cov["level"] == ch.RED
        for row in (cov, rot):
            assert "weread_app_login" not in row["detail"], (
                f"又把人指去重登了 —— 当时的真因是 `-2041`(时限性),动作是等。{row['detail']}")
            assert "等" in row["detail"], f"必须说清动作是「等」:{row['detail']}"
        assert "被挡" in cov["detail"] or "挡" in cov["detail"], "要说清是哪一族"

    def test_窗口坏了时两行都要指向改代码(self, session) -> None:
        """反面对照:另一族的动作是**改代码** —— 两行同样不许打架。"""
        _run(session, "weread_list(ok=0 app=0 off=0 off_with_new=1 skipped=79)")
        _seed(session, 100, 0)
        cov = ch.check_read_num_coverage(session)[0]
        rot = ch.check_weread_list_rotation(session)[0]
        assert rot["level"] == ch.RED
        for row in (cov, rot):
            assert "_list_window" in row["detail"], row["detail"]

    def test_没有记账时给的是决策流程而不是单一动作(self, session) -> None:
        """⚠️ 什么都不记时**不能猜家族**:要给"先看哪个数、再决定动作"的流程。"""
        _seed(session, 100, 0)
        cov = ch.check_read_num_coverage(session)[0]
        assert "weread_list" in cov["detail"], "要教人先去看记账"
        assert "窗口坏了" in cov["detail"] and "重登" in cov["detail"], \
            "三种形状的处置都要列出来"


class Test断供时长与自愈史:
    """★ `-2041` 是**时限性**的 ⇒ 报告必须说得出「上次什么时候好的、自愈过几次」。

    ⚠️ 为什么(2026-10-15):报告里只有「🟡 覆盖率 14%」这一档
    ⇒ **「刚断两小时」与「断了一周」长得一模一样**,人没法判断"还该不该等"
    —— 本仓那条教训:一个会长期挂在同一档的指标,等于没有指标。
    """

    def test_写出上次有读数与自愈次数(self, session) -> None:
        from app.services.chain_health import _weread_downtime_note

        _run(session, "weread_list(ok=0 app=0 off=25 skipped=0)")   # 旧:被挡
        _run(session, "weread_list(ok=13 app=6 off=0 skipped=0)")   # 中间:好了(自愈 1 次)
        _run(session, "weread_list(ok=0 app=0 off=25 skipped=0)")   # 最新:又被挡
        note = _weread_downtime_note(session)
        assert "上次有读数" in note and "1 轮前" in note, note
        assert "自愈过 1 次" in note, note
        assert "别换 cookie" in note, "必须把「等」写成动作,否则又会有人去换凭据"

    def test_一直没好要说一次都没成过(self, session) -> None:
        from app.services.chain_health import _weread_downtime_note

        _run(session, "weread_list(ok=0 app=0 off=25 skipped=0)", n=3)
        assert "一次都没成过" in _weread_downtime_note(session)

    def test_最近一轮有读数就不用说这些(self, session) -> None:
        from app.services.chain_health import _weread_downtime_note

        _run(session, "weread_list(ok=0 app=0 off=25 skipped=0)")
        _run(session, "weread_list(ok=5 app=0 off=0 skipped=0)")
        assert _weread_downtime_note(session) == "最近一轮**有读数**"
