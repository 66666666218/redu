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
