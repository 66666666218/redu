"""小红书**免扫码登录**(`--lt cookie`)单测(2026-10-06)。

## 为什么改
原来走 `--lt qrcode`:登录态一没就**弹二维码等人扫**,而扫码是无人值守流程里
**最脆的一环** —— 等 120s 没人扫就整轮失败,而失败长得像"接口挂了"。
用户口径:「小红书一直需要重新登入的能否解决」。

## 关键事实(读源码得到,不是猜)
MediaCrawler 的 `media_platform/xhs/login.py::login_by_cookies` **只认一个值**:

    for key, value in convert_str_cookie_to_dict(self.cookie_str).items():
        if key != "web_session": continue
        await self.browser_context.add_cookies([{..., 'domain': ".xiaohongshu.com"}])

⇒ 只要 `.env` 里有 `web_session`,就能跳过二维码。实测:`Login state result: True`。
取值方式见 `tools/xhs_export_cookie.py`(**它就用 Playwright 开那个 profile 读**)。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest  # noqa: E402

from app.services import mediacrawler_source as mc  # noqa: E402


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """把它的 base_config.py 指向临时文件 —— **单测不改工具目录里的真配置**。"""
    p = tmp_path / "base_config.py"
    p.write_text('KEYWORDS = "旧词"\nLOGIN_TYPE = "qrcode"\nCOOKIES = ""\n'
                 'SAVE_DATA_OPTION = "csv"\nPLATFORM = "xhs"\n', encoding="utf-8")
    monkeypatch.setattr(mc, "CONFIG", p)
    return p


class TestLoginCookie:
    def test_小红书有值_其他平台为空(self, monkeypatch) -> None:
        from config.settings import get_settings
        st = get_settings()
        monkeypatch.setattr(st, "xhs_web_session", "web_session=ABC123", raising=False)
        assert mc._login_cookie("xiaohongshu") == "web_session=ABC123"
        # ⚠️ 别的平台**不能**被塞小红书的 cookie(那会把它们的登录搞坏)
        assert mc._login_cookie("tieba") == ""
        assert mc._login_cookie("douyin") == ""


class TestWriteConfig:
    def test_有cookie就写cookie模式(self, cfg) -> None:
        mc._write_config(["网盘资源"], "web_session=ABC123")
        t = cfg.read_text(encoding="utf-8")
        assert 'LOGIN_TYPE = "cookie"' in t, t
        assert 'COOKIES = "web_session=ABC123"' in t, t
        assert 'KEYWORDS = "网盘资源"' in t and 'SAVE_DATA_OPTION = "jsonl"' in t

    def test_没cookie退回扫码模式(self, cfg) -> None:
        """⚠️ 反向:没配凭据时**不能**写成 cookie 模式 —— 那会让它拿空 cookie 去登,
        报一个和"没登录"分不清的错;退回 qrcode 至少给得出二维码。"""
        mc._write_config(["网盘资源"], "")
        t = cfg.read_text(encoding="utf-8")
        assert 'LOGIN_TYPE = "qrcode"' in t and 'COOKIES = ""' in t, t

    def test_其余行原样保留(self, cfg) -> None:
        """只改该改的几行 —— 整文件重写会把它别的配置抹掉。"""
        mc._write_config(["a"], "web_session=X")
        assert 'PLATFORM = "xhs"' in cfg.read_text(encoding="utf-8")
