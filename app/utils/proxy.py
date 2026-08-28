"""代理 IP 封装(见 doc/dev.md §5.1)。

采用隧道代理方案:全局统一出口,从配置构造 `proxies` 字典,
避免在每个请求中硬编码代理地址。隧道代理自动轮换出口,无需维护 IP 池。
"""
from __future__ import annotations

from config import Settings


def get_proxies(settings: Settings) -> dict[str, str] | None:
    """构造作用于 requests 的 proxies 字典。

    规则(见 doc/dev.md §5.1):
    - `USE_PROXY=false` 时返回 `None`,走直连。
    - 否则按 `http/https` 构造隧道代理,带 `PROXY_USER/PROXY_PASS` 鉴权。

    返回:
        `dict[str, str]`,可传给 `requests.Session.proxies`;未启用代理时返回 `None`。
    """
    if not settings.use_proxy or not settings.proxy_url:
        return None

    auth = ""
    if settings.proxy_user:
        auth = f"{settings.proxy_user}:{settings.proxy_pass}@"

    proxy_url = f"http://{auth}{settings.proxy_url}"
    return {"http": proxy_url, "https": proxy_url}
