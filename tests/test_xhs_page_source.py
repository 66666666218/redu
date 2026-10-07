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
                raise x.XhsPageError("小红书要**安全验证**(被重定向到 captcha 页)")
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
            raise x.XhsPageError("小红书要**安全验证**(被重定向到 captcha 页)")

        monkeypatch.setattr(x, "_search_with_profile", _boom)
        with pytest.raises(x.XhsPageError) as e:
            x.search(["甲"])
        assert "xhs_pass_verify" in str(e.value) and "xhs_a" in str(e.value)
