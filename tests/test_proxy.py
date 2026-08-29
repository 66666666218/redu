"""代理封装单测。"""
from config.settings import Settings
from app.utils.proxy import get_proxies, playwright_proxy, proxy_url


def test_disabled_returns_none() -> None:
    s = Settings(_env_file=None, use_proxy=False, proxy_url="proxy.example:8080")
    assert get_proxies(s) is None


def test_no_url_returns_none() -> None:
    s = Settings(_env_file=None, use_proxy=True, proxy_url="")
    assert get_proxies(s) is None


def test_with_auth() -> None:
    s = Settings(
        _env_file=None,
        use_proxy=True,
        proxy_url="proxy.example:8080",
        proxy_user="user",
        proxy_pass="secret",
    )
    proxies = get_proxies(s)
    assert proxies == {
        "http": "http://user:secret@proxy.example:8080",
        "https": "http://user:secret@proxy.example:8080",
    }


def test_without_auth() -> None:
    s = Settings(_env_file=None, use_proxy=True, proxy_url="proxy.example:8080")
    proxies = get_proxies(s)
    assert proxies["http"] == "http://proxy.example:8080"


def test_url_with_scheme_is_preserved() -> None:
    s = Settings(_env_file=None, use_proxy=True, proxy_url="http://proxy.example:8080", proxy_user="u", proxy_pass="i")
    assert proxy_url(s) == "http://u:i@proxy.example:8080"


def test_socks5_scheme() -> None:
    s = Settings(_env_file=None, use_proxy=True, proxy_url="socks5://proxy.example:1080")
    assert proxy_url(s) == "socks5://proxy.example:1080"
    proxies = get_proxies(s)
    assert proxies["http"] == "socks5://proxy.example:1080"
    # Playwright 不支持 socks5,应返回 None(浏览器直连)
    assert playwright_proxy(s) is None


def test_playwright_proxy_for_http() -> None:
    s = Settings(_env_file=None, use_proxy=True, proxy_url="proxy.example:8080", proxy_user="u", proxy_pass="p")
    assert playwright_proxy(s) == {"server": "http://u:p@proxy.example:8080"}
