"""代理封装单测。"""
from config.settings import Settings
from app.utils.proxy import get_proxies, playwright_proxy, proxy_url, ProxyPool


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


# ---- 提取式代理池 ----
def test_parse_line() -> None:
    assert ProxyPool.parse_line("1.2.3.4:8080:user:pass") == ("1.2.3.4", "8080", "user", "pass")
    assert ProxyPool.parse_line("bad-line") is None
    assert ProxyPool.parse_line("") is None


def test_parse_line_rejects_vendor_error_json() -> None:
    """厂商额度到期返回的 JSON 冒号切开也有 4 段,必须靠 ip:port 形状挡掉。"""
    assert ProxyPool.parse_line('{"code":405,"msg":"业务已到期，请先续费","data":null}') is None
    assert ProxyPool.parse_line("host.example:8080:u:p") is None  # 非 IP
    assert ProxyPool.parse_line("1.2.3.4:abc:u:p") is None        # 端口非数字
    assert ProxyPool.parse_line("1.2.3.4:99999:u:p") is None      # 端口越界


def test_pool_empty_when_all_lines_invalid() -> None:
    """整批都非法(如接口报错)时应退回直连,而不是给出垃圾代理。"""
    pool = ProxyPool("http://fake/getips", fetch=lambda url: ['{"code":405,"msg":"业务已到期","data":null}'])
    assert pool.get_proxies() is None


def _fake_fetch(url: str) -> list[str]:  # type: ignore[no-untyped-def]
    return ["1.2.3.4:8080:u1:p1", "5.6.7.8:9090:u2:p2", "garbage"]


def test_pool_rotates_valid_proxy() -> None:
    pool = ProxyPool("http://fake/getips", refresh_seconds=170, fetch=_fake_fetch)
    proxies = pool.get_proxies()
    assert proxies is not None
    url = proxies["http"]
    assert url.startswith("http://")
    assert "@1.2.3.4:8080" in url or "@5.6.7.8:9090" in url
    assert "garbage" not in url


def test_pool_refreshes_when_expired() -> None:
    calls = {"n": 0}

    def fetch(url: str) -> list[str]:  # type: ignore[no-untyped-def]
        calls["n"] += 1
        return ["9.9.9.9:8080:u:p"]

    pool = ProxyPool("http://fake/getips", refresh_seconds=170, fetch=fetch)
    pool.get_proxies()
    assert calls["n"] == 1
    pool._last = 0  # 强制过期,下一次 get_proxies 应触发刷新
    pool.get_proxies()
    assert calls["n"] == 2
