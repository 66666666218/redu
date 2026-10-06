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

    def search_files(self, keyword: str, size: int = 20):
        """⚠️ 现在是**搜索**而不是列目录 —— 起因是对着真账号踩到 `list_dir` 的 1 万条上限,
        「来自：分享」排在第 1 万条之外、**根本够不着**(详见 `_find_saved_fid` 的注释)。"""
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
    def test_按资源名匹配_取最新(self) -> None:
        assert qk._find_saved_fid(_FakeQt(), "铸剑纳贡（ForgeTax）") == "F1"

    def test_匹配不上返回空_不瞎搬(self) -> None:
        """⚠️ 宁可少搬一条,也别搬错资源。"""
        assert qk._find_saved_fid(_FakeQt(), "完全不相干的资源") == ""

    def test_搜不到就返回空(self) -> None:
        class _Empty:
            def search_files(self, keyword, size=20):
                return []
        assert qk._find_saved_fid(_Empty(), "x") == ""


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
                return [{"uid": "v1", "name": "某号", "url": "https://v.douyin.com/AAA/",
                         "snippet": "咐置铸剑上供叩苓 forgetax下载教程", "keyword": "夸克口令"}]
        monkeypatch.setattr("app.services.mediacrawler_source.crawl", _MC.crawl)
        out = dl.find_leads(["夸克口令"])
        assert out and out[0]["mark"] == "咐置铸剑上供叩苓", out
