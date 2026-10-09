"""小红书**页面渲染**源单测(2026-10-07)。

背景:MediaCrawler 直连小红书搜索接口回 `461 CAPTCHA`(`code 300011 检测到账号异常`),
上游"集成 xhshow 修好"要升级整个 MediaCrawler(本地是脱敏教学版、许可证禁商用)。
⇒ 改走**页面渲染**:让页面自己的 JS 带签名,只读 DOM(与闲鱼那条路同一个套路)。

⚠️ 这个文件里最要紧的一条是 **`test_被安全验证拦住要抛错而不是返回空`** ——
"被拦"和"真没有"混在一起,结论就会变成"小红书没热度"这种**假结论**。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest  # noqa: E402

from app.services import xhs_page_source as x  # noqa: E402


@pytest.fixture
def session():
    """内存库 + 一个用户(多账号的游标/标记都落在 system_config)。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db import models  # noqa: F401
    from app.db.database import Base
    from app.db.models import User

    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)()
    db.add(User(id=1, username="u1", email="u1@b.c", password_hash="x", enabled=True))
    db.commit()
    yield db
    db.close()


class TestVerifyNeeded:
    """判据用 **url / 标题**,不用"卡片数"。

    ⚠️ 我第一版就是看卡片数,于是把"被重定向到验证页"误判成"页面没 hydrate",
    **结论整个反了** —— 而那本该是这次排查的关键线索。
    """

    def test_重定向到验证页要判出来(self) -> None:
        assert x.verify_needed(
            "https://www.xiaohongshu.com/website-login/captcha?redirectPath=https%3A%2F%2F"
            "www.xiaohongshu.com%2Fexplore", "安全验证")

    def test_只有标题带安全验证也算(self) -> None:
        assert x.verify_needed("https://www.xiaohongshu.com/explore", "安全验证")

    def test_页面里有验证组件也算(self) -> None:
        assert x.verify_needed("https://www.xiaohongshu.com/explore", "小红书",
                               '<div class="xhsCaptcha_feedback-link">')

    def test_正常搜索页不算(self) -> None:
        """★ 反例:正常页**绝不能**被判成"被拦",否则整条链直接停摆。"""
        assert not x.verify_needed(
            "https://www.xiaohongshu.com/search_result?keyword=%E7%BD%91%E7%9B%98%E8%B5%84%E6%BA%90",
            "网盘资源 - 小红书搜索", '<section class="note-item">…</section>')


class TestProfile:
    def test_相对路径按仓库根解析(self) -> None:
        """不能依赖进程 CWD(调度器与脚本的启动位置不一定一样)。"""
        from pathlib import Path

        p = x._profile(type("S", (), {"xhs_browser_profile": ""})())
        assert isinstance(p, Path) and p.is_absolute()
        assert str(p).replace("\\", "/").endswith(
            "tools/MediaCrawler/browser_data/cdp_xhs_user_data_dir")

    def test_配了就用配的(self) -> None:
        p = x._profile(type("S", (), {"xhs_browser_profile": str("D:/tmp/xhs")})())
        assert str(p).replace("\\", "/").endswith("D:/tmp/xhs")


class TestSearchGuards:
    def test_空词表直接返回(self) -> None:
        assert x.search([]) == []

    def test_档案不存在要给可执行的修法(self) -> None:
        """⚠️ 报错必须告诉人**怎么做**,否则只会看到"小红书又不推了"。"""
        class _S:
            xhs_browser_profile = "D:/nonexistent/xhs_profile_zzz"
        with pytest.raises(x.XhsPageError) as e:
            x.search(["测试"], settings=_S())
        assert "xhs_pass_verify" in str(e.value)


class TestWiring:
    def test_小红书走页面路而不是_MediaCrawler(self, monkeypatch) -> None:
        from app.services import resource_presence as rp

        called: dict = {}

        def _fake_search(names):
            called["names"] = list(names)
            return [{"keyword": names[0], "snippet": "笔记标题", "uid": "", "name": "",
                     "url": "", "pan_link": ""}]

        monkeypatch.setattr(x, "search", _fake_search)
        out = rp._crawl_platform("xiaohongshu", ["网盘资源"])
        assert called["names"] == ["网盘资源"] and out[0]["snippet"] == "笔记标题"

    def test_页面路被拦要转成平台硬失败(self, monkeypatch) -> None:
        """★ **必须冒泡**:静默跳过会让"小红书被拦"长得像"小红书没热度"。"""
        from app.services import resource_presence as rp
        from app.services.mediacrawler_source import MediaCrawlerError

        def _boom(names):
            raise x.XhsPageError("要安全验证")

        monkeypatch.setattr(x, "search", _boom)
        with pytest.raises(MediaCrawlerError) as e:
            rp._crawl_platform("xiaohongshu", ["网盘资源"])
        assert "xiaohongshu" in str(e.value) and "安全验证" in str(e.value)


class TestMultiAccount:
    """★ **多账号轮换 + 失败自动切换**(用户口径「小红书我给你多个账号」)。

    为什么:风控是**账号级**的(实测码 `300011 检测到账号异常`)——
    单账号频率除以账号数,是最直接的一条降险手段。
    """

    def test_没配就用单账号(self) -> None:
        class _S:
            xhs_browser_profiles = ""
            xhs_browser_profile = ""
        assert len(x._profiles(_S())) == 1

    def test_配了多个就按顺序(self) -> None:
        class _S:
            xhs_browser_profiles = "data/xhs_a, data/xhs_b ,data/xhs_c"
            xhs_browser_profile = ""
        got = [p.name for p in x._profiles(_S())]
        assert got == ["xhs_a", "xhs_b", "xhs_c"]

    def test_游标会轮换(self, session, monkeypatch) -> None:
        """★ 连续几轮必须落在**不同账号**上 —— 否则"多账号"只是摆设。"""
        monkeypatch.setattr("app.db.get_session_local", lambda: (lambda: session))
        seen = [x._cursor_index(3) for _ in range(6)]
        assert seen == [0, 1, 2, 0, 1, 2], seen

    def test_单账号时不碰游标(self, monkeypatch) -> None:
        called: list = []
        monkeypatch.setattr("app.db.get_session_local",
                            lambda: called.append(1) or (lambda: None))
        assert x._cursor_index(1) == 0 and called == []

    def test_被拦就换下一个账号(self, session, monkeypatch) -> None:
        """★★ 最要紧的一条:**一个号被安全验证拦住,要自动换下一个**,不用等人。"""
        monkeypatch.setattr("app.db.get_session_local", lambda: (lambda: session))
        profs = [x.Path("data/xhs_a"), x.Path("data/xhs_b")]
        monkeypatch.setattr(x, "_profiles", lambda s=None: profs)
        monkeypatch.setattr(x.Path, "exists", lambda self: True)
        tried: list[str] = []

        def _fake(p_, prof, kws, wait, headed):
            tried.append(prof.name)
            if prof.name == "xhs_a":
                # ⚠️ 新契约:`needs_human=True` 才触发"标记 + 换号" ——
                # 抛不带标记的 XhsPageError 表示"换个号也没用",会被直接上抛。
                raise x.XhsPageError("小红书要**安全验证**", needs_human=True)
            return [{"keyword": kws[0], "snippet": "好用的号", "uid": "", "name": "",
                     "url": "", "pan_link": ""}]

        monkeypatch.setattr(x, "_search_with_profile", _fake)
        rows = x.search(["甲"])
        assert len(tried) == 2 and "xhs_b" in tried      # 换了号
        assert rows and rows[0]["snippet"] == "好用的号"
        # 被拦的那个号要**被标记**,下一轮轮换直接跳过它(别继续烧风控)
        # ⚠️ 存的是**完整路径**,所以用子串判断 —— `"xhs_a" in {set}` 是成员判断,会假红
        assert any("xhs_a" in s for s in x.need_verify_profiles()),             x.need_verify_profiles()

    def test_已知需验证的账号直接跳过(self, session, monkeypatch) -> None:
        monkeypatch.setattr("app.db.get_session_local", lambda: (lambda: session))
        profs = [x.Path("data/xhs_a"), x.Path("data/xhs_b")]
        monkeypatch.setattr(x, "_profiles", lambda s=None: profs)
        monkeypatch.setattr(x.Path, "exists", lambda self: True)
        monkeypatch.setattr(x, "need_verify_profiles", lambda: {str(profs[0])})
        tried: list[str] = []

        def _fake(p_, prof, kws, wait, headed):
            tried.append(prof.name)
            return [{"keyword": kws[0], "snippet": "x", "uid": "", "name": "",
                     "url": "", "pan_link": ""}]

        monkeypatch.setattr(x, "_search_with_profile", _fake)
        x.search(["甲"])
        assert tried == ["xhs_b"], f"该跳过 xhs_a,实际试了 {tried}"

    def test_全都不可用要给出可执行的修法(self, session, monkeypatch) -> None:
        monkeypatch.setattr("app.db.get_session_local", lambda: (lambda: session))
        monkeypatch.setattr(x, "_profiles",
                            lambda s=None: [x.Path("data/xhs_a"), x.Path("data/xhs_b")])
        monkeypatch.setattr(x.Path, "exists", lambda self: True)
        monkeypatch.setattr(x, "need_verify_profiles", lambda: set())

        def _boom(p_, prof, kws, wait, headed):
            raise x.XhsPageError("小红书要**安全验证**", needs_human=True)

        monkeypatch.setattr(x, "_search_with_profile", _boom)
        with pytest.raises(x.XhsPageError) as e:
            x.search(["甲"])
        assert "xhs_pass_verify" in str(e.value) and "xhs_a" in str(e.value)


class TestNotLoggedIn:
    """★ **"未登录" 必须报错,不能返回空**(2026-10-07 抓到的假阴性)。

    实测:号 2 的 cookie 注进去了但站点不认,搜索页写着「登录后查看搜索结果」;
    那时 `section.note-item` 是 0、`search-empty-wrapper` 也是 0 ——
    于是旧代码走进"这次没搜到"的分支,**静默返回空**。
    日志里它长得像"小红书没热度",而不是"账号掉线了"。
    """

    def test_判据认得出未登录(self) -> None:
        assert x.needs_login("登录后查看搜索结果 扫码成功")
        assert x.needs_login("请在手机上确认")

    def test_真没有结果不算未登录(self) -> None:
        """★ 反例:空态页**绝不能**被判成未登录,否则正常的"没结果"会变成告警。"""
        assert not x.needs_login("网盘资源 - 小红书搜索 没有找到相关结果")

    def test_异常带需要人工的标记(self) -> None:
        assert x.XhsPageError("x", needs_human=True).needs_human is True
        assert x.XhsPageError("x").needs_human is False

    def test_未登录会标记该号并换下一个(self, session, monkeypatch) -> None:
        """未登录的号要**被标记 + 换下一个**,而不是把"空"当成结果交上去。"""
        monkeypatch.setattr("app.db.get_session_local", lambda: (lambda: session))
        profs = [x.Path("data/xhs_a"), x.Path("data/xhs_b")]
        monkeypatch.setattr(x, "_profiles", lambda s=None: profs)
        monkeypatch.setattr(x.Path, "exists", lambda self: True)
        tried: list[str] = []

        def _fake(p_, prof, kws, wait, headed):
            tried.append(prof.name)
            if prof.name == "xhs_a":
                raise x.XhsPageError("账号未登录", needs_human=True)
            return [{"keyword": kws[0], "snippet": "b 号的结果", "uid": "", "name": "",
                     "url": "", "pan_link": ""}]

        monkeypatch.setattr(x, "_search_with_profile", _fake)
        rows = x.search(["甲"])
        assert tried == ["xhs_a", "xhs_b"] and rows[0]["snippet"] == "b 号的结果"
        assert any("xhs_a" in s for s in x.need_verify_profiles())

    def test_被踢下线也算未登录(self) -> None:
        """⚠️ 实测:账号 1 是被账号 2 **踢下线**的(「电脑设备登录超限,请重新登录」)。
        这句话不在词表里的话,就会**静默返回空** —— 又一次假阴性。"""
        assert x.needs_login("电脑设备登录超限，请重新登录")
        assert x.needs_login("登录已过期，请重新登录")


class TestAccountHealth:
    """★ 小红书账号的可用性要**每天能看见**(2026-10-07)。

    它最大的失败模式是"**某个号被踢下线**"(实测:登了号 2 之后号 1 变成
    「电脑设备登录超限,请重新登录」),而这件事**不会自己好**。
    不主动报的话,表现是"这条链悄悄不产出",要等人来问才发现。
    """

    def test_有待处理的号就报红并给修法(self, session, monkeypatch) -> None:
        from app.services import chain_health as ch

        monkeypatch.setattr("app.db.get_session_local", lambda: (lambda: session))
        profs = [x.Path("data/xhs_a"), x.Path("data/xhs_b")]
        monkeypatch.setattr(x, "_profiles", lambda s=None: profs)
        monkeypatch.setattr(x, "need_verify_profiles", lambda: {str(profs[0])})
        items = ch.check_xhs_accounts()
        assert items[0]["level"] == ch.RED
        assert "xhs_a" in items[0]["detail"] and "xhs_pass_verify" in items[0]["detail"]

    def test_都正常就报绿(self, session, monkeypatch) -> None:
        from app.services import chain_health as ch

        monkeypatch.setattr(x, "_profiles", lambda s=None: [x.Path("data/xhs_a")])
        monkeypatch.setattr(x, "need_verify_profiles", lambda: set())
        assert ch.check_xhs_accounts()[0]["level"] == ch.GREEN

    def test_一个档位都没配要说出来(self, monkeypatch) -> None:
        from app.services import chain_health as ch

        monkeypatch.setattr(x, "_profiles", lambda s=None: [])
        items = ch.check_xhs_accounts()
        assert items[0]["level"] == ch.YELLOW and "XHS_BROWSER_PROFILES" in items[0]["detail"]


class TestHotRankCardAllSources:
    """★ **每个源各取自己最新那一轮**(2026-10-07 用户当场看出来的 bug)。

    老写法取的是"**全表最新的那一刻**",而各源是**不同作业在不同时刻**写的
    ⇒ 那一刻只有那一批源在,卡上于是只剩十几个(用户:「不是 38 个平台吗,怎么就这几个」)。
    实测:库里 41 个源,老写法当场只能拿到 **1** 个。
    """

    @staticmethod
    def _session():
        from datetime import datetime, timedelta

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from app.db import models  # noqa: F401
        from app.db.database import Base
        from app.db.models import HotSourceItem, User

        eng = create_engine("sqlite://")
        Base.metadata.create_all(eng)
        db = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)()
        db.add(User(id=1, username="u1", email="u1@b.c", password_hash="x", enabled=True))
        now = datetime.now()
        # 两个源在**不同时刻**采集 —— 这正是老写法翻车的地方
        db.add(HotSourceItem(user_id=1, source="cankoxiaoxi", rank=1, title="参考消息头条",
                             captured_at=now - timedelta(hours=3)))
        db.add(HotSourceItem(user_id=1, source="hupu", rank=1, title="虎扑热帖",
                             captured_at=now))
        db.commit()
        return db

    def test_两个源即使采集时刻不同也都要上卡(self, monkeypatch) -> None:
        from app.services import hot_sources as hs
        from app.services.feishu_client import FeishuClient

        sent: dict = {}

        class _F:
            def __init__(self, *a, **k):
                pass

            def send(self, text):
                sent["text"] = text
                return True

        db = self._session()
        try:
            monkeypatch.setattr("app.db.get_session_local", lambda: (lambda: db))
            monkeypatch.setattr(hs, "webhook_for", lambda *a, **k: "hook", raising=False)
            monkeypatch.setattr(FeishuClient, "send", _F("x", "").send, raising=False)
            monkeypatch.setattr("app.services.feishu_client.FeishuClient", _F)
            monkeypatch.setattr("app.services.feishu_client.webhook_for",
                                lambda *a, **k: "hook")
            class _S:
                feishu_secret = ""
            hs.push_hot_rank_card_all_users(settings=_S())
            assert "参考消息头条" in sent["text"], "采得早的那个源被丢了"
            assert "虎扑热帖" in sent["text"]
        finally:
            db.close()

    def test_没有数据时不发空卡(self, monkeypatch) -> None:
        """★ 我改坏判据那次,卡上只剩标题一行 —— **一张空卡比不发更糟**
        (读的人以为"今天没热点")。没内容就别发,并说清原因。"""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from app.db import models  # noqa: F401
        from app.db.database import Base
        from app.db.models import User
        from app.services import hot_sources as hs

        eng = create_engine("sqlite://")
        Base.metadata.create_all(eng)
        db = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)()
        db.add(User(id=1, username="u1", email="u1@b.c", password_hash="x", enabled=True))
        db.commit()
        sent: list = []

        class _F:
            def __init__(self, *a, **k):
                pass

            def send(self, text):
                sent.append(text)
                return True

        try:
            monkeypatch.setattr("app.db.get_session_local", lambda: (lambda: db))
            monkeypatch.setattr("app.services.feishu_client.FeishuClient", _F)
            monkeypatch.setattr("app.services.feishu_client.webhook_for",
                                lambda *a, **k: "hook")

            class _S:
                feishu_secret = ""
            assert hs.push_hot_rank_card_all_users(settings=_S()) == 0
            assert sent == [], "没有数据却把空卡发出去了"
        finally:
            db.close()


# ---------------------------------------------------------------------------
# ★★ 2026-10-09:「需验证」记号是一条**单向陷阱**(路径写法对不上就永远摘不掉)
# ---------------------------------------------------------------------------


def _tmp_db(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db.database import Base
    from app.db import models  # noqa: F401
    from app.db.models import SystemConfig

    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    SystemConfig.__table__.create(eng, checkfirst=True)
    sm = sessionmaker(bind=eng)
    import app.db as _db

    monkeypatch.setattr(_db, "get_session_local", lambda: sm)
    return sm


def test_需验证记号_相对与绝对路径必须都能摘掉(monkeypatch, tmp_path) -> None:
    """★★ 实测踩到:原来 `mark`/`clear` 两边都直接用 `str(profile)` ——
    **存的是绝对路径、摘的时候传相对路径就永远对不上**,于是:

      ① 记号一旦写下,除了「轮次成功」那一条路,**谁也别想摘掉**;
      ② 而小红书当时整条链是坏的(协议 `-104` + 页面路不可用)⇒ **那一轮永远等不到**;
      ③ ⇒ 体检上挂了一条**永远红的假红灯**,而假红灯会训练人忽略整份报告。

    判据:同一台机器上**同一个档案**,不管用哪种写法传,都必须是同一个身份。
    """
    _tmp_db(monkeypatch)
    from pathlib import Path

    from app.services import xhs_page_source as xp

    prof = Path("data/xhs_test")
    xp.mark_need_verify(prof.resolve())                    # 存**绝对**
    assert xp.need_verify_profiles(), "标记没写进去"
    xp.clear_need_verify(Path("data/xhs_test"))            # 摘**相对** —— 必须能摘
    assert xp.need_verify_profiles() == set(), "相对路径摘不掉绝对路径写的记号(单向陷阱)"


def test_需验证记号_能清掉历史遗留的相对路径写法(monkeypatch) -> None:
    """兼容:以前可能已经把**相对路径**写进库里了,现在按归一化身份也要能清掉。"""
    import json

    _tmp_db(monkeypatch)
    from pathlib import Path

    from app.db import get_session_local
    from app.db.models import SystemConfig
    from app.services import xhs_page_source as xp

    with get_session_local()() as db:
        db.add(SystemConfig(key="xhs_account_need_verify",
                            value=json.dumps(["data/xhs_legacy"])))
        db.commit()
    xp.clear_need_verify(Path("data/xhs_legacy"))
    assert xp.need_verify_profiles() == set(), "历史遗留的相对路径写法清不掉"
