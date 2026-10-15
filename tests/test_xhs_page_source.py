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


class TestAccountPoolWiring:
    """★ **页面路与协议路共用一台状态机**(2026-10-12)。

    原先页面路自己一套"轮换游标 + 需验证名单",协议路只读**单枚** cookie ——
    两条路健康状态不互通:协议路撞上 `-104` 时**不知道**页面路早就标过"这号别用"。
    现在都从 `xhs_accounts` 这张池子进出(页面路报的是**档案**
    `profile:<按仓库根解析的绝对路径>`)。
    """

    def _profs(self, monkeypatch):
        profs = [x.Path("data/xhs_a"), x.Path("data/xhs_b")]
        monkeypatch.setattr(x, "_profiles", lambda s=None: profs)
        monkeypatch.setattr(x.Path, "exists", lambda self: True)
        return profs

    def test_频率风控要报进池子并换下一个(self, session, monkeypatch) -> None:
        """★ 认出「请勿频繁操作」后:①**报进池子**(拿一个**带时刻**的 3h 冷却);
        ②**换下一个号** —— 而不是把整轮打挂。

        ⚠️ 这一条补的是**本仓一个长期空档**:页面路早就认得出频率风控,
        但那时**没有地方存"要冷多久"**(旧注释里写着"要另加一个带时间戳的跳过机制"),
        于是每轮重新撞 —— 而**越撞越久**。
        """
        monkeypatch.setattr("app.db.get_session_local", lambda: (lambda: session))
        self._profs(monkeypatch)
        tried: list[str] = []

        def _fake(p_, prof, kws, wait, headed):
            tried.append(prof.name)
            if prof.name == "xhs_a":
                raise x.XhsPageError("请勿频繁操作,稍后重试", rate_limit=True)
            return [{"keyword": kws[0], "snippet": "好用的号", "uid": "", "name": "",
                     "url": "", "pan_link": ""}]

        monkeypatch.setattr(x, "_search_with_profile", _fake)
        rows = x.search(["甲"])
        assert tried == ["xhs_a", "xhs_b"], tried                    # 换了号
        assert rows and rows[0]["snippet"] == "好用的号"
        from app.services import xhs_accounts as pool
        assert any(p.endswith("xhs_a") for p in pool.cooling_profiles()), \
            "频率风控没被报进池子 ⇒ 下一轮还会继续撞它(越撞越久)"

    def test_池里在冷却的档案会被跳过(self, session, monkeypatch) -> None:
        """★ 池子里"还在冷却"的档案,页面路下一轮要**直接跳过** ——
        这才是"按时长退避"真正生效的地方。"""
        monkeypatch.setattr("app.db.get_session_local", lambda: (lambda: session))
        profs = self._profs(monkeypatch)
        from app.services import xhs_accounts as pool
        pool.report_profile(profs[0], pool.STATE_RATE_LIMITED, "请勿频繁操作")
        tried: list[str] = []

        def _fake(p_, prof, kws, wait, headed):
            tried.append(prof.name)
            return [{"keyword": kws[0], "snippet": "x", "uid": "", "name": "",
                     "url": "", "pan_link": ""}]

        monkeypatch.setattr(x, "_search_with_profile", _fake)
        x.search(["甲"])
        assert tried == ["xhs_b"], f"该跳过在冷却里的 xhs_a,实际试了 {tried}"

    def test_全在有害冷却里时不该硬试(self, session, monkeypatch) -> None:
        """★★ **全都在"越试越糟"的冷却里 ⇒ 直接失败,不要"反正都试一遍"**。

        ⚠️ 这是本仓用真账号换来的教训:**在已被风控的账号上反复重试,冷却会被推后**
        (服务端按账号/IP 记账 ⇒ 越试越久)。所以"全被跳过就退回全试一遍"这条兜底
        **必须**对这类状态关掉。
        """
        monkeypatch.setattr("app.db.get_session_local", lambda: (lambda: session))
        profs = self._profs(monkeypatch)
        from app.services import xhs_accounts as pool
        for p in profs:
            pool.report_profile(p, pool.STATE_RATE_LIMITED, "请勿频繁操作")

        called: list[str] = []

        def _fake(p_, prof, kws, wait, headed):
            called.append(prof.name)
            return []

        monkeypatch.setattr(x, "_search_with_profile", _fake)
        with pytest.raises(x.XhsPageError) as e:
            x.search(["甲"])
        assert called == [], f"不该真的去试(越试越久),实际试了 {called}"
        assert "冷却" in str(e.value) and "越试越久" in str(e.value), str(e.value)

    def test_需人验证的不算有害冷却_仍会放行试一轮(self, session, monkeypatch) -> None:
        """★ 反例(别把护栏开太大):`need_verify` 硬试一轮**没用但不会更糟** ——
        所以"全被跳过"时仍要放行;把这类也拦掉会变成"什么都没做就报错"。"""
        monkeypatch.setattr("app.db.get_session_local", lambda: (lambda: session))
        profs = self._profs(monkeypatch)
        from app.services import xhs_accounts as pool
        for p in profs:
            pool.report_profile(p, pool.STATE_NEED_VERIFY, "安全验证")

        tried: list[str] = []

        def _fake(p_, prof, kws, wait, headed):
            tried.append(prof.name)
            raise x.XhsPageError("要安全验证", needs_human=True)

        monkeypatch.setattr(x, "_search_with_profile", _fake)
        with pytest.raises(x.XhsPageError) as e:
            x.search(["甲"])
        assert tried == ["xhs_a", "xhs_b"], tried          # 两个都真去试了
        #: 报的是"逐个都不可用",**不是**"有害冷却所以不试"
        assert "所有小红书账号都不可用" in str(e.value), str(e.value)

    def test_成功会清掉池里的冷却(self, session, monkeypatch) -> None:
        """★ 另一半:成功了要把冷却**清掉** —— 否则好号会被无谓地一直跳过。
        (`search()` 成功那条路走的就是 `clear_need_verify`。)"""
        monkeypatch.setattr("app.db.get_session_local", lambda: (lambda: session))
        profs = self._profs(monkeypatch)
        from app.services import xhs_accounts as pool
        pool.report_profile(profs[0], pool.STATE_RATE_LIMITED, "请勿频繁操作")
        assert pool.cooling_profiles(), "前置条件:该档案应在冷却里"
        x.clear_need_verify(profs[0])
        assert pool.cooling_profiles() == set(), pool.cooling_profiles()


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

    def test_有号在频率冷却就报黄并说只需等(self, session, monkeypatch) -> None:
        """★ 2026-10-12 新增的一档:没有"要人"的,但有"在冷却"的。

        ⚠️ **必须与"需人工"分开报**:频率风控**只需等** —— 说成"需人工"会让人去
        重登/重试,而那正是**最不该做的事**(越试越久)。"""
        from app.services import chain_health as ch
        from app.services import xhs_accounts as pool

        monkeypatch.setattr("app.db.get_session_local", lambda: (lambda: session))
        profs = [x.Path("data/xhs_a"), x.Path("data/xhs_b")]
        monkeypatch.setattr(x, "_profiles", lambda s=None: profs)
        monkeypatch.setattr(x, "need_verify_profiles", lambda: set())
        pool.report_profile(profs[0], pool.STATE_RATE_LIMITED, "请勿频繁操作")

        items = ch.check_xhs_accounts()
        assert items[0]["level"] == ch.YELLOW, items[0]
        assert "冷却" in items[0]["detail"] and "xhs_a" in items[0]["detail"]
        assert "只需等" in items[0]["detail"], items[0]["detail"]
        assert "xhs_b" not in items[0]["detail"], "没冷却的号不该被点名"

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


class _FakeFirst:
    def __init__(self, href: str | None = None, text: str | None = None) -> None:
        self._href, self._text = href, text

    def get_attribute(self, name: str, timeout=None):
        return self._href if name == "href" else None

    def inner_text(self, timeout=None):
        """★ `text is None` = **这个节点不存在**(Playwright 的 `inner_text` 会等到超时再抛)。

        让替身**真的抛**,而不是回一个空串 —— 否则"节点没了"和"节点是空的"分不开,
        `_card_rows` 那条**回退**分支就永远测不到。
        """
        if self._text is None:
            raise TimeoutError("node not found")
        return self._text


class _FakeLocator:
    def __init__(self, href: str | None = None, text: str | None = None) -> None:
        self.first = _FakeFirst(href, text)


class _FakeCard:
    """一张笔记卡(或推荐栏)的替身。

    `author` / `title` 就是**真实 DOM 里那两个节点**(`a.author .name` / `a.title > span`);
    不传 = 节点不存在 ⇒ 走 `_card_rows` 的**行序兜底**分支。
    """

    def __init__(self, href: str | None, text: str,
                 author: str | None = None, title: str | None = None) -> None:
        self._href, self._text = href, text
        self._author, self._title = author, title

    def locator(self, sel: str) -> _FakeLocator:
        if sel == x.AUTHOR_SEL:
            return _FakeLocator(text=self._author)
        if sel == x.TITLE_SEL:
            return _FakeLocator(text=self._title)
        return _FakeLocator(href=self._href)

    def inner_text(self) -> str:
        return self._text


class _FakeCards:
    def __init__(self, items: list[_FakeCard]) -> None:
        self._items = items

    def count(self) -> int:
        return len(self._items)

    def nth(self, i: int) -> _FakeCard:
        return self._items[i]


class TestKickedOffline:
    """★ **「未登录」有两种,修法相反** —— 混成一句"去重登"会让人陷入自我加剧的循环。

    实测(2026-10-12):同一台机器连登 4 个号 ⇒ 小红书按**设备**限制同时在线的账号数,
    刚登完几分钟 `04` 就被降级成 `03`、`webSsk` 归零。此时**重登是最坏的动作** ——
    每登一个新号就再触发一次设备风控,把新号也踢掉。
    """

    def _mk(self, tmp_path, name, ws):
        d = tmp_path / "data"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"xhs_session_{name}.json").write_text(
            '{"cookies": {"web_session": "%s"}}' % ws, encoding="utf-8")

    def test_存档是真登录却被判未登录_算被踢下线(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(x, "_root", lambda: tmp_path)
        self._mk(tmp_path, "a", "040069b7366765643da334f0f9384b0899c555")
        assert x._had_real_session("a") is True

    def test_存档本来就是游客_不算被踢(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(x, "_root", lambda: tmp_path)
        self._mk(tmp_path, "b", "030037adffffffffffffffffffffffffffff")
        assert x._had_real_session("b") is False

    def test_没有存档_不算被踢(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(x, "_root", lambda: tmp_path)
        assert x._had_real_session("没这个档案") is False

    def test_存档坏了也不炸(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(x, "_root", lambda: tmp_path)
        (tmp_path / "data").mkdir(parents=True, exist_ok=True)
        (tmp_path / "data" / "xhs_session_c.json").write_text("{坏 JSON",
                                                              encoding="utf-8")
        assert x._had_real_session("c") is False

    def test_被踢时给rate_limit而不是叫人重登(self, tmp_path, monkeypatch) -> None:
        """★ 要害:被踢下线时若报 `needs_human=True`,会叫人去重登 ⇒ 越修越糟。"""
        monkeypatch.setattr(x, "_root", lambda: tmp_path)
        self._mk(tmp_path, "a", "040069b7366765643da334f0f9384b0899c555")
        monkeypatch.setattr(x, "_had_real_session", lambda p: True)
        try:
            x._raise_not_logged_in("a", x.Path("data/a"))
        except x.XhsPageError as e:
            assert getattr(e, "rate_limit", False) is True, "被踢应判成「只需等」"
            assert getattr(e, "needs_human", False) is False, "不该叫人去重登"
            assert "重登" in str(e) and "等" in str(e)
        else:
            raise AssertionError("应当抛错")


class TestCardRows:
    """★ 笔记卡解析(2026-10-12 修的那一处)。

    原来 `uid` / `name` / `url` **全是空串**,而且"大家都在搜"的推荐栏会**混进来当第 0 条**。
    """

    def test_推荐栏没有笔记链接要被滤掉(self) -> None:
        cards = _FakeCards([
            _FakeCard(None, "大家都在搜 附近的美食 美食家常菜"),   #: 推荐栏
            _FakeCard("/explore/6a7582270000000006004c7c",
                      "大人今天也吃点小孩菜😋\n胃口特派员\n08-07\n4.5万"),
        ])
        rows = x._card_rows(cards, "美食")
        assert len(rows) == 1, "推荐栏不该进结果"
        assert rows[0]["uid"] == "6a7582270000000006004c7c"

    def test_真笔记要提出_uid_与完整链接(self) -> None:
        cards = _FakeCards([
            _FakeCard("/explore/6a9936d8000000002601bb58?xsec_token=ABC",
                      "天冷来一碗！\n牛牛吃个牛\n2025-11-05\n18.8万"),
        ])
        r = x._card_rows(cards, "美食")[0]
        assert r["uid"] == "6a9936d8000000002601bb58"      #: 查询串要剥掉
        assert r["url"] == "https://www.xiaohongshu.com/explore/6a9936d8000000002601bb58"
        assert "天冷来一碗" in r["snippet"]

    def test_次行当作者(self) -> None:
        cards = _FakeCards([
            _FakeCard("/explore/aaa", "标题一\n作者甲\n08-07\n123"),
        ])
        assert x._card_rows(cards, "k")[0]["name"] == "作者甲"

    def test_作者从_DOM_结构取_不看行序(self) -> None:
        """★★ 2026-10-15 那个 bug 的回归守卫。

        实测输出里**至少 3/20 条**的「作者」其实是**日期**(`08-13` / `6天前` /
        `2025-08-03`)—— 有些卡片**没有作者那一行**,行序整体错位。
        抓真实 DOM 后确认:作者在 `a.author .name` 里。
        """
        cards = _FakeCards([
            _FakeCard("/explore/aaa", "考研网盘在哪里找\n6天前\n7",
                      author="小红薯6AA33EA1", title="考研网盘在哪里找"),
        ])
        r = x._card_rows(cards, "k")[0]
        assert r["name"] == "小红薯6AA33EA1", "又回去按行序取作者了"
        assert "6天前" not in r["name"]

    def test_没有作者节点时回退到行序(self) -> None:
        """老卡片 / 节点没渲染出来时**不能变空** —— 兜底必须留着(上面那条不能把兜底删掉)。"""
        cards = _FakeCards([_FakeCard("/explore/bbb", "标题一\n作者甲\n08-07\n123")])
        assert x._card_rows(cards, "k")[0]["name"] == "作者甲"

    def test_作者节点存在但为空时回退行序(self) -> None:
        cards = _FakeCards([_FakeCard("/explore/ccc", "标题\n日期行\n7", author="")])
        assert x._card_rows(cards, "k")[0]["name"] == "日期行"

    def test_作者节点读失败不拖垮整批(self) -> None:
        class _NoAuthor:
            def locator(self, sel):
                if sel == x.AUTHOR_SEL:
                    raise RuntimeError("节点炸了")
                return _FakeLocator(href="/explore/ddd")

            def inner_text(self):
                return "标题\n作者丁"

        class _Cards:
            def count(self):
                return 1

            def nth(self, i):
                return _NoAuthor()

        assert x._card_rows(_Cards(), "k")[0]["name"] == "作者丁"

    def test_只有一行时作者留空不炸(self) -> None:
        cards = _FakeCards([_FakeCard("/explore/bbb", "只有标题")])
        r = x._card_rows(cards, "k")[0]
        assert r["name"] == "" and r["snippet"] == "只有标题"

    def test_关键词要带在每行上(self) -> None:
        cards = _FakeCards([_FakeCard("/explore/ccc", "t\na")])
        assert x._card_rows(cards, "网盘资源")[0]["keyword"] == "网盘资源"

    def test_空卡片跳过(self) -> None:
        cards = _FakeCards([_FakeCard("/explore/ddd", "   ")])
        assert x._card_rows(cards, "k") == []

    def test_超上限只取_limit_条(self) -> None:
        cards = _FakeCards([_FakeCard(f"/explore/{i}", f"标题{i}\n作者{i}")
                            for i in range(50)])
        assert len(x._card_rows(cards, "k", limit=7)) == 7

    def test_单个卡片抛异常不拖垮整批(self) -> None:
        class _Boom:
            def locator(self, sel):
                raise RuntimeError("卡片炸了")

            def inner_text(self):
                return ""

        class _Cards:
            def count(self):
                return 3

            def nth(self, i):
                return _Boom() if i == 1 else _FakeCard("/explore/ok", "好卡\n作者")
        rows = x._card_rows(_Cards(), "k")
        assert len(rows) == 2, "坏卡片要跳过,其余照收"


class TestBrowserStealth:
    """★ **采集时的浏览器不能自报"我是脚本"**(2026-10-13 实测补上)。

    之前所有指纹自检查的都是**协议路**(存档 UA、curl_cffi 的 `impersonate`),
    **没人查过页面渲染路真跑起来的浏览器**。一查就出事:

        "webdriver": true
        "ua": "... HeadlessChrome/154.0.0.0 ... Edg/154.0.0.0"

    `navigator.webdriver === true` 是最低级、最好抓的自动化标记;
    UA 里的 `HeadlessChrome` 更是**自报家门** —— 会话是**有头**浏览器登录签发的,
    采集却用**无头** ⇒ **登录设备 ≠ 采集设备**。
    这正是本项目在协议路上修过一次、还记进 MEMORY 的同一个错配,**换了个位置又犯了一遍**。

    社区口径一致(16k stars 的 `xpzouying/xiaohongshu-mcp`、RedBookC):
    **Cookie 离开原始浏览器环境就会"设备特征对不上"** ⇒ 采集应尽量与登录
    处在同一个浏览器环境里。
    """

    #: 登录脚本(有头)抓到的真 UA
    REAL_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36 Edg/154.0.0.0")

    @staticmethod
    def _mk(tmp_path, name="prof1", ua=None):
        """落一份会话存档(登录脚本就是这么写的)。`ua=None` ⇒ 老存档,没有 UA 字段。"""
        d = tmp_path / "data"
        d.mkdir(parents=True, exist_ok=True)
        (d / name).mkdir(parents=True, exist_ok=True)
        import json

        meta = {"cookies": {"web_session": "04abc"}}
        if ua is not None:
            meta["ua"] = ua
        (d / f"xhs_session_{name}.json").write_text(
            json.dumps(meta), encoding="utf-8")
        return d / name

    def test_读得到登录时那份UA(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(x, "_root", lambda: tmp_path)
        p = self._mk(tmp_path, ua=self.REAL_UA)
        assert x._launch_ua(p) == self.REAL_UA

    def test_老存档没有UA时不硬编(self, tmp_path, monkeypatch) -> None:
        """⚠️ 拿不到就回空 ⇒ **不覆盖**。覆盖成一个错的比不覆盖更糟。"""
        monkeypatch.setattr(x, "_root", lambda: tmp_path)
        p = self._mk(tmp_path, ua=None)
        assert x._launch_ua(p) == ""

    def test_存档文件不存在也不炸(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(x, "_root", lambda: tmp_path)
        assert x._launch_ua(tmp_path / "data" / "nope") == ""

    def test_无头要用登录时那份UA覆盖(self, tmp_path, monkeypatch) -> None:
        """★ **要害**:无头 Chromium 默认会写成 `HeadlessChrome/`,必须被覆盖掉。"""
        monkeypatch.setattr(x, "_root", lambda: tmp_path)
        p = self._mk(tmp_path, ua=self.REAL_UA)
        kw = x._launch_kwargs(p, headed=False)
        assert kw["user_agent"] == self.REAL_UA
        assert x._HEADLESS_UA_TOKEN not in kw["user_agent"]
        assert kw["headless"] is True

    def test_有头就不必覆盖(self, tmp_path, monkeypatch) -> None:
        """有头时浏览器自己那份就是真的 ⇒ 仍然带上也无害,但要保持 `headed` 语义。"""
        monkeypatch.setattr(x, "_root", lambda: tmp_path)
        p = self._mk(tmp_path, ua=self.REAL_UA)
        kw = x._launch_kwargs(p, headed=True)
        assert kw["headless"] is False
        assert kw["user_agent"] == self.REAL_UA

    def test_没有存档UA时不塞user_agent(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(x, "_root", lambda: tmp_path)
        p = self._mk(tmp_path, ua=None)
        assert "user_agent" not in x._launch_kwargs(p, headed=False)

    def test_要关掉自动化控制特性(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(x, "_root", lambda: tmp_path)
        p = self._mk(tmp_path, ua=self.REAL_UA)
        assert x._NO_AUTOMATION_ARG in x._launch_kwargs(p, False)["args"]

    def test_注入脚本要藏住webdriver(self) -> None:
        """⚠️ 这条最容易**悄悄退化**:脚本还在、但没生效(注入时机晚于页面创建)。"""
        js = x._STEALTH_JS
        assert "webdriver" in js
        assert "defineProperty" in js
        #: 只藏 webdriver 不够 —— 真浏览器还有 `window.chrome.runtime`
        assert "chrome" in js and "runtime" in js

    def test_注入脚本不能伪造出矛盾特征(self) -> None:
        """前人的警告:**伪造出一堆互相矛盾的特征反而更像假环境**
        (Windows + Safari、分辨率与 GPU 不匹配)。⇒ 这里坚持"只做标准那三条"。
        """
        js = x._STEALTH_JS.lower()
        for bad in ("safari", "firefox", "iphone", "macintel", "webgl", "canvas"):
            assert bad not in js, f"伪造了 {bad} ⇒ 容易组合出矛盾的指纹"


class TestBrowserStealth:
    """★ **采集时的浏览器不能自报"我是脚本"**(2026-10-13 实测补上)。

    之前所有指纹自检查的都是**协议路**(存档 UA、curl_cffi 的 `impersonate`),
    **没人查过页面渲染路真跑起来的浏览器**。一查就出事:

        "webdriver": true
        "ua": "... HeadlessChrome/154.0.0.0 ... Edg/154.0.0.0"

    `navigator.webdriver === true` 是最低级、最好抓的自动化标记;
    UA 里的 `HeadlessChrome` 更是**自报家门** —— 会话是**有头**浏览器登录签发的,
    采集却用**无头** ⇒ **登录设备 ≠ 采集设备**。
    这正是本项目在协议路上修过一次、还记进 MEMORY 的同一个错配,**换了个位置又犯了一遍**。

    社区口径一致(16k stars 的 `xpzouying/xiaohongshu-mcp`、RedBookC):
    **Cookie 离开原始浏览器环境就会"设备特征对不上"** ⇒ 采集应尽量与登录
    处在同一个浏览器环境里。
    """

    #: 登录脚本(有头)抓到的真 UA
    REAL_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36 Edg/154.0.0.0")

    @staticmethod
    def _mk(tmp_path, name="prof1", ua=None):
        """落一份会话存档(登录脚本就是这么写的)。`ua=None` ⇒ 老存档,没有 UA 字段。"""
        d = tmp_path / "data"
        d.mkdir(parents=True, exist_ok=True)
        (d / name).mkdir(parents=True, exist_ok=True)
        import json

        meta = {"cookies": {"web_session": "04abc"}}
        if ua is not None:
            meta["ua"] = ua
        (d / f"xhs_session_{name}.json").write_text(
            json.dumps(meta), encoding="utf-8")
        return d / name

    def test_读得到登录时那份UA(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(x, "_root", lambda: tmp_path)
        p = self._mk(tmp_path, ua=self.REAL_UA)
        assert x._launch_ua(p) == self.REAL_UA

    def test_老存档没有UA时不硬编(self, tmp_path, monkeypatch) -> None:
        """⚠️ 拿不到就回空 ⇒ **不覆盖**。覆盖成一个错的比不覆盖更糟。"""
        monkeypatch.setattr(x, "_root", lambda: tmp_path)
        p = self._mk(tmp_path, ua=None)
        assert x._launch_ua(p) == ""

    def test_存档文件不存在也不炸(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(x, "_root", lambda: tmp_path)
        assert x._launch_ua(tmp_path / "data" / "nope") == ""

    def test_无头要用登录时那份UA覆盖(self, tmp_path, monkeypatch) -> None:
        """★ **要害**:无头 Chromium 默认会写成 `HeadlessChrome/`,必须被覆盖掉。"""
        monkeypatch.setattr(x, "_root", lambda: tmp_path)
        p = self._mk(tmp_path, ua=self.REAL_UA)
        kw = x._launch_kwargs(p, headed=False)
        assert kw["user_agent"] == self.REAL_UA
        assert x._HEADLESS_UA_TOKEN not in kw["user_agent"]
        assert kw["headless"] is True

    def test_有头就不必覆盖(self, tmp_path, monkeypatch) -> None:
        """有头时浏览器自己那份就是真的 ⇒ 仍然带上也无害,但要保持 `headed` 语义。"""
        monkeypatch.setattr(x, "_root", lambda: tmp_path)
        p = self._mk(tmp_path, ua=self.REAL_UA)
        kw = x._launch_kwargs(p, headed=True)
        assert kw["headless"] is False
        assert kw["user_agent"] == self.REAL_UA

    def test_没有存档UA时不塞user_agent(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(x, "_root", lambda: tmp_path)
        p = self._mk(tmp_path, ua=None)
        assert "user_agent" not in x._launch_kwargs(p, headed=False)

    def test_要关掉自动化控制特性(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(x, "_root", lambda: tmp_path)
        p = self._mk(tmp_path, ua=self.REAL_UA)
        assert x._NO_AUTOMATION_ARG in x._launch_kwargs(p, False)["args"]

    def test_注入脚本要藏住webdriver(self) -> None:
        """⚠️ 这条最容易**悄悄退化**:脚本还在、但没生效(注入时机晚于页面创建)。"""
        js = x._STEALTH_JS
        assert "webdriver" in js
        assert "defineProperty" in js
        #: 只藏 webdriver 不够 —— 真浏览器还有 `window.chrome.runtime`
        assert "chrome" in js and "runtime" in js

    def test_注入脚本不能伪造出矛盾特征(self) -> None:
        """前人的警告:**伪造出一堆互相矛盾的特征反而更像假环境**
        (Windows + Safari、分辨率与 GPU 不匹配)。⇒ 这里坚持"只做标准那三条"。
        """
        js = x._STEALTH_JS.lower()
        for bad in ("safari", "firefox", "iphone", "macintel", "webgl", "canvas"):
            assert bad not in js, f"伪造了 {bad} ⇒ 容易组合出矛盾的指纹"
