"""代理封装单测。"""
from config.settings import Settings
from app.utils.proxy import get_proxies


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
