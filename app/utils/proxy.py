"""代理 IP 封装(见 doc/dev.md §5.1)。

采用隧道代理方案:全局统一出口,从配置构造 `proxies` 字典,
避免在每个请求中硬编码代理地址。隧道代理自动轮换出口,无需维护 IP 池。
同时提供 Playwright 浏览器所需的 `proxy` 参数字典。
"""
from __future__ import annotations

from config import Settings


def _normalize_proxy_url(url: str) -> tuple[str, str]:
    """拆分代理地址为 (scheme, host:port)。

    支持 `host:port`、`http://host:port`、`https://host:port`、`socks5://host:port`。
    未带 scheme 时默认 `http`。
    """
    text = url.strip()
    if "://" in text:
        scheme, rest = text.split("://", 1)
        return scheme.lower(), rest.strip()
    return "http", text


def proxy_url(settings: Settings) -> str | None:
    """返回单条代理 URL 字符串(含鉴权),未启用代理时返回 `None`。"""
    if not settings.use_proxy or not settings.proxy_url:
        return None
    scheme, host_port = _normalize_proxy_url(settings.proxy_url)
    auth = f"{settings.proxy_user}:{settings.proxy_pass}@" if settings.proxy_user else ""
    return f"{scheme}://{auth}{host_port}"


def get_proxies(settings: Settings) -> dict[str, str] | None:
    """构造作用于 requests 的 proxies 字典。

    规则(见 doc/dev.md §5.1):
    - `USE_PROXY=false` 时返回 `None`,走直连。
    - 否则按 `http/https` 构造隧道代理,带 `PROXY_USER/PROXY_PASS` 鉴权。

    返回:
        `dict[str, str]`,可传给 `requests.Session.proxies`;未启用代理时返回 `None`。
    """
    url = proxy_url(settings)
    if url is None:
        return None
    return {"http": url, "https": url}


def playwright_proxy(settings: Settings) -> dict[str, str] | None:
    """构造 Playwright `browser.launch(proxy=...)` 所需的参数字典。

    仅支持 http/https 隧道代理;未启用或为 socks5 时返回 `None`(交由浏览器直连)。
    """
    url = proxy_url(settings)
    if url is None or url.startswith("socks5://"):
        return None
    return {"server": url}
