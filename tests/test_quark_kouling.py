"""夸克口令(U口令)链路单测(2026-10-06)。

⚠️ 这条链**依赖模拟器**(雷电 + 夸克已登录),所以单测里**一个 subprocess 都不许真跑** ——
全部 mock 掉,只验编排逻辑。

最要紧的两条:
  · **子进程超时必须当失败** —— 否则"模拟器卡住了"会被读成"这条口令没内容";
  · **一条失败不能拖垮其余** —— 待办队列是逐条独立处理的。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import models  # noqa: F401
from app.db.database import Base
from app.db.models import DiscoveredPanLink, DouyinLead, User
from app.services import quark_kouling as qk


@pytest.fixture
def session():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)()
    db.add(User(id=1, username="u1", email="u1@b.c", password_hash="x", enabled=True))
    db.commit()
    yield db
    db.close()


class _S:
    quark_kouling_enabled = True
    quark_kouling_per_run = 5


def _lead(session, aweme_id: str, title: str) -> DouyinLead:
    r = DouyinLead(user_id=1, aweme_id=aweme_id, mark="口令", title=title,
                   author="籽***", url=f"https://v.douyin.com/{aweme_id}/")
    session.add(r)
    session.commit()
    return r


class _FakeQt:
    """假的 QuarkTransfer:记下调用,返回固定分享链。"""

    def __init__(self, *a, **k):
        self.shared: list = []
        import time as _t
        self._recent = [{"fid": "NEWEST", "file_name": "第五人格美化包教程下载-",
                         "updated_at": str(int(_t.time() * 1000))}]

    def list_dir(self, fid: str = "0"):
        return [{"fid": "DIR1", "file_name": "来自：分享"}] if fid == "0" else []

    def list_recent(self, fid: str = "0", size: int = 30):
        return self._recent

    def search_files(self, keyword: str, size: int = 20):
        """⚠️ 现在是**搜索**而不是列目录 —— 起因是对着真账号踩到 `list_dir` 的 1 万条上限,
        「来自：分享」排在第 1 万条之外、**根本够不着**(详见 `_find_saved_fid` 的注释)。"""
        # 目录本身也要能"搜到" —— 兜底那条路靠它拿 fid(不能靠 list_dir,见实现里的注释)
        if keyword == "来自：分享":
            return [{"fid": "DIR1", "file_name": "来自：分享", "updated_at": "1"}]
        if "铸剑" in keyword:
            return [{"fid": "F0", "file_name": "铸剑纳贡（ForgeTax）", "updated_at": "100"},
                    {"fid": "F1", "file_name": "铸剑纳贡（ForgeTax）", "updated_at": "200"}]
        return []

    def share_fids(self, fids, title="", **kw):
        self.shared.append((list(fids), title))
        return {"share_url": f"https://pan.quark.cn/s/OUR{len(self.shared)}",
                "password": "abcd", "share_id": f"sid{len(self.shared)}"}


class TestResolveFailuresAreStructured:
    """★ **超时/异常必须返回 ok=False**,绝不能读成"这条口令没内容"。"""

    def test_子进程超时当失败(self, monkeypatch) -> None:
        import subprocess

        def _boom(*a, **k):
            raise subprocess.TimeoutExpired(cmd="x", timeout=1)
        monkeypatch.setattr(qk.subprocess, "run", _boom)
        r = qk.resolve("咐置铸剑上供叩苓")
        assert r["ok"] is False and "超时" in r["reason"], r

    def test_输出读不出结果也算失败(self, monkeypatch) -> None:
        class _R:
            stdout = b"\xe5\x95\x8a\xe5\x95\x8a (no result)"
        monkeypatch.setattr(qk.subprocess, "run", lambda *a, **k: _R())
        assert qk.resolve("x")["ok"] is False

    def test_空文本不发子进程(self, monkeypatch) -> None:
        def _boom(*a, **k):
            raise AssertionError("空文本不该跑子进程")
        monkeypatch.setattr(qk.subprocess, "run", _boom)
        assert qk.resolve("  ")["ok"] is False

    def test_正常输出能解析(self, monkeypatch) -> None:
        class _R:
            stdout = "结果: {'ok': True, 'title': '铸剑纳贡（ForgeTax）'}".encode()
        monkeypatch.setattr(qk.subprocess, "run", lambda *a, **k: _R())
        r = qk.resolve("x")
        assert r["ok"] is True and r["title"] == "铸剑纳贡（ForgeTax）"


class TestFindSavedFid:
    """⚠️ **卡片标题与保存后的文件名经常对不上**(2026-10-06 找到的真原因,实测):

        卡片:「第五人格美化包（先保存再下载）」
        盘里:「第五人格美化包教程下载-」        ← **互不包含**

    而按名字匹配的实现遇到这种就判"没找到文件",于是一条**明明搬成了**的线索被记成失败。
    所以:**先按名字找;找不到就取"刚刚新增的那个"**(App 刚存的一定是最新的),
    **但只认时间窗内的**(5 分钟)—— 不是无脑取最新,否则会误取别人的文件。
    """

    def test_按名字匹配优先(self) -> None:
        assert qk._find_saved_fid(_FakeQt(), "铸剑纳贡（ForgeTax）") == "F1"

    def test_名字对不上就取刚刚新增的(self, session) -> None:
        """★ 这是**真原因**:卡片的标题和盘里的文件名互不包含。"""
        assert qk._find_saved_fid(_FakeQt(), "完全不相干的资源", session, 1) == "NEWEST"

    def test_兜底只认时间窗内_不无脑取最新(self, session) -> None:
        """⚠️ 反向:如果"最新那个"是**一小时前**的(不是刚存的),就不该认 —— 那多半是别人的文件。"""
        class _Old(_FakeQt):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                import time as _t
                self._recent = [{"fid": "STALE", "file_name": "别人的文件",
                                 "updated_at": str(int((_t.time() - 3600) * 1000))}]
        assert qk._find_saved_fid(_Old(), "对不上的名字", session, 1) == ""

    def test_都没时间也返回空(self, session) -> None:
        class _NoTime(_FakeQt):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                self._recent = [{"fid": "X", "file_name": "y", "updated_at": "0"}]
        assert qk._find_saved_fid(_NoTime(), "对不上", session, 1) == ""


class TestDrain:
    def _patch(self, monkeypatch, results: dict):
        monkeypatch.setattr("app.services.cookie_store.get_cookie", lambda s, u, p: "CK")
        monkeypatch.setattr("app.services.quark_transfer.QuarkTransfer", _FakeQt)
        monkeypatch.setattr(qk, "resolve", lambda text, **k: results.get(text, {"ok": False,
                                                                               "reason": "mock"}))
        return results

    def test_没cookie就返回no_cookie(self, session, monkeypatch) -> None:
        monkeypatch.setattr("app.services.cookie_store.get_cookie", lambda s, u, p: "")
        assert qk.drain(session, 1, _S())["status"] == "no_cookie"

    def test_成功后写our_url并进资源库(self, session, monkeypatch) -> None:
        lead = _lead(session, "a1", "咐置铸剑上供叩苓")
        self._patch(monkeypatch, {"咐置铸剑上供叩苓": {"ok": True, "title": "铸剑纳贡（ForgeTax）"}})
        out = qk.drain(session, 1, _S())
        assert out["done"] == 1 and out["failed"] == 0, out
        session.refresh(lead)
        assert lead.our_url.startswith("https://pan.quark.cn/s/OUR"), lead.our_url
        # 进资源库那条路:status **必须是 ok** —— pan_discovery 只把 ok/skipped 当"已知",
        # 于是不会被重复转存;而 resource_library 读这张表,agent 才会看见它。
        rows = session.scalars(select(DiscoveredPanLink)).all()
        assert len(rows) == 1 and rows[0].status == "ok"
        assert rows[0].our_url == lead.our_url and rows[0].platform == "douyin-kouling"

    def test_一条失败不影响其余(self, session, monkeypatch) -> None:
        """★ 待办队列是**逐条独立**的:一条解不出来,后面的照跑。"""
        a = _lead(session, "a1", "解不出来的那条")
        b = _lead(session, "a2", "咐置铸剑上供叩苓")
        self._patch(monkeypatch, {"咐置铸剑上供叩苓": {"ok": True, "title": "铸剑纳贡（ForgeTax）"}})
        out = qk.drain(session, 1, _S())
        assert out["done"] == 1 and out["failed"] == 1, out
        session.refresh(a); session.refresh(b)
        assert a.our_url == "" and b.our_url != ""

    def test_已搬过的不再进待办(self, session, monkeypatch) -> None:
        lead = _lead(session, "a1", "咐置铸剑上供叩苓")
        lead.our_url = "https://pan.quark.cn/s/OLD"
        session.commit()
        self._patch(monkeypatch, {"咐置铸剑上供叩苓": {"ok": True, "title": "X"}})
        assert qk.drain(session, 1, _S())["tried"] == 0


class TestTriedMarker:
    """★ **失败也要留痕**(2026-10-06 实测逼出来的)。

    抖音线索里**绝大多数是迅雷形态**(《》里的群/分享口令),而夸克 App **对它们不弹卡片** ——
    同样两条标题:迅雷型失败、夸克型成功。不留痕的话,每轮都会拿同一批"匹配不了"的线索
    去烧模拟器时间(**一条 15–20 秒**)。所以:**无论成败都盖 `kouling_tried_at`**。
    """

    def _patch(self, monkeypatch, results=None):
        results = results or {}
        monkeypatch.setattr("app.services.cookie_store.get_cookie", lambda s, u, p: "CK")
        monkeypatch.setattr("app.services.quark_transfer.QuarkTransfer", _FakeQt)
        monkeypatch.setattr(qk, "resolve",
                            lambda text, **k: results.get(text, {"ok": False, "reason": "没弹卡片"}))

    def test_失败的也会盖上时间戳(self, session, monkeypatch) -> None:
        lead = _lead(session, "a1", "《三岁分享》#时代峰峻 #投票入口")
        self._patch(monkeypatch)
        qk.drain(session, 1, _S())
        session.refresh(lead)
        assert lead.kouling_tried_at is not None, "试过了必须留痕,否则每轮重试同一批"

    def test_试过的不再进待办(self, session, monkeypatch) -> None:
        _lead(session, "a1", "随便一条")
        self._patch(monkeypatch)
        assert qk.drain(session, 1, _S())["tried"] == 1
        assert qk.drain(session, 1, _S())["tried"] == 0, "第二轮不该再拿同一条去烧时间"

    def test_夸克形态优先于迅雷形态(self, session, monkeypatch) -> None:
        _lead(session, "x1", "《三岁分享》#时代峰峻")          # 迅雷形态,靠后
        _lead(session, "q1", "咐置铸剑上供叩苓")               # 夸克形态,应当先试

        class _S1(_S):
            quark_kouling_per_run = 1            # 只试一条 ⇒ 看它先挑谁

        seen: list[str] = []
        monkeypatch.setattr(qk, "resolve",
                            lambda text, **k: seen.append(text) or {"ok": False, "reason": "mock"})
        monkeypatch.setattr("app.services.cookie_store.get_cookie", lambda s, u, p: "CK")
        monkeypatch.setattr("app.services.quark_transfer.QuarkTransfer", _FakeQt)
        qk.drain(session, 1, _S1())
        assert seen and "咐置" in seen[0], f"应当优先试夸克形态的:{seen}"


class TestMarkOf:
    """★ **夸克口令帖没有《》** —— 而 `find_leads` 原来只认《》,
    于是这类帖**一条都进不来**(实测搜「夸克口令」一次捞到 15 条,全被挡在门外)。

    两种形态都是实测来的:`/~令牌~/`(App 拼文案写的,最可靠)与 `咐置…叩苓`(推广号的规避写法)。
    """

    def test_认出令牌形态(self) -> None:
        assert qk.mark_of("夸克秒懂！复制/~b6b53ZE3FV~:/，打开夸克直接看") == "/~b6b53ZE3FV~"

    def test_认出规避写法(self) -> None:
        assert qk.mark_of("咐置铸剑上供叩苓 forgetax下载教程解压即玩") == "咐置铸剑上供叩苓"
        assert qk.mark_of("咐置美化包教程叩苓｜你们要的第五人格美化包") == "咐置美化包教程叩苓"

    def test_迅雷型的不要误认(self) -> None:
        """⚠️ 反向:普通《》帖**不该**被当成夸克口令(否则白白烧模拟器时间)。"""
        assert qk.mark_of("《三岁分享》#时代峰峻 #投票入口") == ""
        assert qk.mark_of("iPad 都快用半年了 才学会设置动态壁纸壁纸教程在@《李白壁纸》") == ""

    def test_令牌优先于规避写法(self) -> None:
        t = "我用夸克网盘给你分享了「Diplay」…亝已闭枫五并闭岗乡哉哎忛 /~dda43bGyER~:/ 咐置试叩苓"
        assert qk.mark_of(t) == "/~dda43bGyER~"


class TestFindLeadsAcceptsQuark:
    def test_只有夸克标记的帖也能进库(self, monkeypatch) -> None:
        from app.services import douyin_leads as dl

        class _MC:
            @staticmethod
            def crawl(platform, keywords):
                import datetime as _dt
                # ⚠️ **必须带发布时间的近期值** —— `find_leads` 现在会按
                # `douyin_leads_min_publish_date`(默认 2026-10-01)过滤,缺时间的会被丢掉。
                return [{"uid": "v1", "name": "某号", "url": "https://v.douyin.com/AAA/",
                         "snippet": "咐置铸剑上供叩苓 forgetax下载教程", "keyword": "夸克口令",
                         "publish_at": int((_dt.datetime.now() - _dt.timedelta(days=1)).timestamp())}]
        monkeypatch.setattr("app.services.mediacrawler_source.crawl", _MC.crawl)
        out = dl.find_leads(["夸克口令"])
        assert out and out[0]["mark"] == "咐置铸剑上供叩苓", out


class TestSaveRetry:
    """★ **"解析出来了但没找到文件"要重试一次**(2026-10-06 实测会遇到)。

    那多半是**保存那一步偶发失败**(UI 点击落空 / 保存任务还没落盘),不是"口令没内容"。
    ⇒ **只重试这一种失败,且最多两次**:压根没解析出来的重试一百次也没用,
    给它翻倍烧模拟器时间(一条 15–20 秒)纯属浪费。
    """

    def _patch(self, monkeypatch, finder):
        monkeypatch.setattr("app.services.cookie_store.get_cookie", lambda s, u, p: "CK")
        monkeypatch.setattr("app.services.quark_transfer.QuarkTransfer", _FakeQt)
        monkeypatch.setattr(qk, "resolve", lambda text, **k: {"ok": True, "title": "铸剑纳贡（ForgeTax）"})
        monkeypatch.setattr(qk, "_find_saved_fid", finder)
        monkeypatch.setattr(qk.time, "sleep", lambda s: None)   # 别真睡

    def test_第一次找不到会再试一次(self, session, monkeypatch) -> None:
        lead = _lead(session, "a1", "咐置铸剑上供叩苓")
        calls: list[int] = []

        def _finder(qt, title, sess=None, uid=0):
            calls.append(1)
            return "" if len(calls) == 1 else "F1"     # 第二次才找到
        self._patch(monkeypatch, _finder)
        out = qk.drain(session, 1, _S())
        assert len(calls) == 2, f"应当重试一次,实际试了 {len(calls)} 次"
        assert out["done"] == 1, out
        session.refresh(lead)
        assert lead.our_url.startswith("https://pan.quark.cn/s/OUR")

    def test_两次都找不到才算失败(self, session, monkeypatch) -> None:
        _lead(session, "a1", "咐置铸剑上供叩苓")      # ⚠️ 没线索就没人试,失败的当然是 0
        self._patch(monkeypatch, lambda *a, **k: "")
        out = qk.drain(session, 1, _S())
        assert out["done"] == 0 and out["failed"] == 1, out

    def test_没解析出来的不重试(self, session, monkeypatch) -> None:
        """⚠️ **反向**:压根没弹卡片的,重试是白烧时间(一条 15–20 秒)。"""
        _lead(session, "a1", "《三岁分享》#投票入口")
        n = {"resolve": 0}

        def _res(text, **k):
            n["resolve"] += 1
            return {"ok": False, "reason": "没弹卡片"}
        monkeypatch.setattr("app.services.cookie_store.get_cookie", lambda s, u, p: "CK")
        monkeypatch.setattr("app.services.quark_transfer.QuarkTransfer", _FakeQt)
        monkeypatch.setattr(qk, "resolve", _res)
        qk.drain(session, 1, _S())
        assert n["resolve"] == 1, f"没解析出来的不该重试,实际跑了 {n['resolve']} 次"


class TestThreePanInterop:
    """★ **三盘互通**:同一个资源在**任意一个盘**已经有了,**就不再搬一份**(用户口径:
    「所有资源都走三盘互通」)。

    ⚠️ 这道门原来**只有 `pan_discovery` 一条链有**,而夸克口令 / 迅雷群 / 迅雷口令
    三条链**一律盲转** —— 同一个资源在三个盘里各存一份。
    """

    def test_命中互通就不跑UI不建链(self, session, monkeypatch) -> None:
        """★ 命中时**连解析都不该跑** —— 那是 15–20 秒的模拟器时间。"""
        _lead(session, "a1", "咐置铸剑上供叩苓")
        monkeypatch.setattr("app.services.cookie_store.get_cookie", lambda s, u, p: "CK")
        monkeypatch.setattr("app.services.quark_transfer.QuarkTransfer", _FakeQt)
        monkeypatch.setattr("app.services.pan_discovery.already_have",
                            lambda s, u, t: {"my_link": "https://pan.quark.cn/s/HAVE",
                                             "pan_url": "https://pan.baidu.com/s/x", "titles": ["铸剑"]})
        ran: list[int] = []
        monkeypatch.setattr(qk, "resolve", lambda text, **k: ran.append(1) or {"ok": True, "title": "X"})

        out = qk.drain(session, 1, _S())

        assert out["done"] == 1, out
        assert ran == [], "命中三盘互通就不该再去跑模拟器(那是 15–20 秒)"
        lead = session.scalars(select(DouyinLead)).one()
        assert lead.our_url == "https://pan.quark.cn/s/HAVE", "应当直接复用已有链"

    def test_没命中才照常搬(self, session, monkeypatch) -> None:
        _lead(session, "a1", "咐置铸剑上供叩苓")
        monkeypatch.setattr("app.services.cookie_store.get_cookie", lambda s, u, p: "CK")
        monkeypatch.setattr("app.services.quark_transfer.QuarkTransfer", _FakeQt)
        monkeypatch.setattr("app.services.pan_discovery.already_have", lambda s, u, t: None)
        monkeypatch.setattr(qk, "resolve", lambda text, **k: {"ok": True, "title": "铸剑纳贡（ForgeTax）"})
        out = qk.drain(session, 1, _S())
        assert out["done"] == 1, out


class TestSlowWorkOutsideTransaction:
    """★ **慢活绝不能在写事务里做**(2026-10-06 实测踩到,而且是当场打脸的那次)。

    `drain` 要在循环里跑**模拟器**(15–20 秒/条 ×8 ≈ 2.7 分钟),而 SQLite 是**单写者**。
    原来整轮都在一个写事务里 ⇒ **把别的作业全饿死**:14:00 那一分钟里
    `bili_account_scan` / `xunlei_sync` / `xunlei_group` **三个作业同时**报
    `database is locked`(它们的 `busy_timeout` 只有 30 秒)。
    """

    def test_跑模拟器之前先提交一次(self, session, monkeypatch) -> None:
        _lead(session, "a1", "咐置铸剑上供叩苓")
        monkeypatch.setattr("app.services.cookie_store.get_cookie", lambda s, u, p: "CK")
        monkeypatch.setattr("app.services.quark_transfer.QuarkTransfer", _FakeQt)
        monkeypatch.setattr("app.services.pan_discovery.already_have", lambda s, u, t: None)
        monkeypatch.setattr(qk, "_find_saved_fid", lambda *a, **k: "F1")

        order: list[str] = []
        real_commit = session.commit

        def _commit():
            order.append("commit")
            real_commit()
        monkeypatch.setattr(session, "commit", _commit)

        def _resolve(text, **k):
            order.append("ui")           # 慢活:模拟器
            return {"ok": True, "title": "铸剑纳贡（ForgeTax）"}
        monkeypatch.setattr(qk, "resolve", _resolve)

        qk.drain(session, 1, _S())

        assert "ui" in order and "commit" in order, order
        assert order.index("commit") < order.index("ui"), (
            "**必须先 commit 释放写锁,再去做慢活** —— 否则整轮占着单写者的库,"
            f"别的作业会 `database is locked`。实际顺序:{order}")


class TestEmptyTitleFallback:
    """★ **空标题是最常见的失败原因**(2026-10-06 实测)。

    日志一直在打「解出「**?**」但两次都找不到文件」—— 那个 `?` 就是**空标题**:
    卡片弹出来了(所以 `resolve` 判 `ok`),但**标题没抽出来**。
    而 `_find_saved_fid` 原来 `if not key: return ""` ⇒ **连"刚刚新增"的兜底都走不到**,
    一条明明搬成了的线索被判成失败。
    """

    def test_空标题走兜底而不是直接判失败(self, session) -> None:
        assert qk._find_saved_fid(_FakeQt(), "", session, 1) == "NEWEST"
        assert qk._find_saved_fid(_FakeQt(), None, session, 1) == "NEWEST"


@pytest.fixture(autouse=True)
def _no_real_snapshot(monkeypatch):
    """⚠️⚠️ **单测绝不碰模拟器**(2026-10-06 修)。

    `drain` 的失败分支会**真的去跑子进程截模拟器屏幕** —— 实测跑一遍单测生成 4 张
    `_quark_nosave_*.png`,一天堆了 **47 张**。慢、有副作用,而且**污染过诊断**:
    我当天正是拿这批**测试产物**当成了生产失败现场,一路推出一个错误根因。
    **先让证据干净,再谈归因。**
    """
    monkeypatch.setattr(qk, "_snapshot_failure", lambda: "")


class TestEnvFailureDoesNotBurnTheLead:
    """★ **环境故障不消耗线索**(2026-10-06)。

    实测:前台被全屏程序占着时,雷电窗口拿不到焦点 ⇒ 宿主剪贴板**同步不进安卓**
    ⇒ 夸克一条都收不到口令。而 `drain` 是**无论成败都盖 `kouling_tried_at`** 的
    (那是为"没内容的口令别无限重试"设计的)⇒ **一次环境抖动会把整批线索永久判死**。
    所以 `resolve` 用 `env=True` 标出这类失败,`drain` 据此**撤回那个章**。
    """

    def _patch(self, monkeypatch, res):
        monkeypatch.setattr("app.services.cookie_store.get_cookie", lambda s, u, p: "CK")
        monkeypatch.setattr("app.services.quark_transfer.QuarkTransfer", _FakeQt)
        monkeypatch.setattr(qk, "resolve", lambda text, **k: res)
        monkeypatch.setattr(qk, "_find_saved_fid", lambda *a, **k: "")

    def test_环境故障不盖章_下轮还能再试(self, session, monkeypatch) -> None:
        lead = _lead(session, "a1", "咐置铸剑上供叩苓")
        self._patch(monkeypatch, {"ok": False, "env": True, "reason": "雷电窗口拿不到焦点"})

        out = qk.drain(session, 1, _S())
        session.refresh(lead)
        assert out["failed"] == 1
        assert lead.kouling_tried_at is None, (
            "环境故障被盖成'试过'了 ⇒ 一次焦点问题会把整批线索**永久判死**"
            "(它们再也不会进待办队列)")
        assert qk.drain(session, 1, _S())["tried"] == 1, "下一轮应当还能再试这条"

    def test_内容型失败照旧盖章(self, session, monkeypatch) -> None:
        """⚠️ 反向:**别把闸门拆了** —— "这条口令没内容"必须留痕,否则每轮白烧模拟器时间。"""
        lead = _lead(session, "a1", "《三岁分享》#投票入口")
        self._patch(monkeypatch, {"ok": False, "reason": "没弹出剪贴板卡片(口令无效)"})

        qk.drain(session, 1, _S())
        session.refresh(lead)
        assert lead.kouling_tried_at is not None, "内容型失败必须留痕"
        assert qk.drain(session, 1, _S())["tried"] == 0
