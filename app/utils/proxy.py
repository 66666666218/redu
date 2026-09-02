"""代理 IP 封装(见 doc/dev.md §5.1)。

支持两种代理模式:
1. **隧道代理**:单一网关 `host:port` + 账号密码(静态)。
2. **提取式代理池**:调用厂商 API(如巨量IP `getips`)拉取一批 `ip:port:user:pass`,
   每个 IP 约 3 分钟有效,自动轮换、失败换下一个。

`get_proxies(settings)` 统一入口:
- 配置了 `PROXY_EXTRACT_URL` → 走提取式代理池;
- 否则配置了 `USE_PROXY + PROXY_URL` → 走静态隧道;
- 否则返回 `None`(直连)。
"""
from __future__ import annotations

import random
import re
import threading
import time

import requests

from config import Settings

# 提取式代理池缓存(按提取 URL 隔离)。
_pool_cache: dict[str, "ProxyPool"] = {}
_pool_lock = threading.Lock()
_IP_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


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
    """返回单条静态隧道代理 URL(含鉴权),未启用时返回 `None`。"""
    if not settings.use_proxy or not settings.proxy_url:
        return None
    scheme, host_port = _normalize_proxy_url(settings.proxy_url)
    auth = f"{settings.proxy_user}:{settings.proxy_pass}@" if settings.proxy_user else ""
    return f"{scheme}://{auth}{host_port}"


def playwright_proxy(settings: Settings) -> dict[str, str] | None:
    """构造 Playwright `browser.launch(proxy=...)` 所需的参数字典。

    仅支持 http/https 隧道代理;未启用或为 socks5 时返回 `None`(交由浏览器直连)。
    """
    url = proxy_url(settings)
    if url is None or url.startswith("socks5://"):
        return None
    return {"server": url}


class ProxyPool:
    """提取式代理池。

    调用厂商提取 API 拉取 `ip:port:user:pass` 列表,`get_proxies()` 随机取出一个;
    超过 `refresh_seconds` 或池为空时自动刷新;拉取/解析失败的条目跳过。
    """

    def __init__(
        self,
        extract_url: str,
        refresh_seconds: int = 170,
        fetch: object | None = None,
    ) -> None:
        self._url = extract_url
        self._ttl = max(30, refresh_seconds)
        self._proxies: list[tuple[str, str, str, str]] = []
        self._last = 0.0
        self._lock = threading.Lock()
        self._fetch = fetch or self._default_fetch

    @staticmethod
    def _default_fetch(url: str) -> list[str]:
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        return resp.text.splitlines()

    @staticmethod
    def parse_line(line: str) -> tuple[str, str, str, str] | None:
        """解析代理行,返回 (ip, port, user, pass);非法则 `None`。

        兼容三种格式:
        - `ip:port`                  → 无鉴权(IP 白名单型,如熊猫代理)
        - `ip:port:user`             → 仅账号
        - `ip:port:user:pass`        → 账号+密码(大多数厂商)
        厂商额度到期时会返回一段 JSON(`{"code":405,"msg":"业务已到期..."}`),
        按冒号切开同样有 4 段,只看段数会拼出垃圾代理 URL 把所有采集一起带崩,
        所以必须校验 `ip` 为点分四段、`port` 为合法数字。
        """
        parts = line.strip().split(":")
        if len(parts) < 2:
            return None
        ip, port = parts[0].strip(), parts[1].strip()
        if not _IP_RE.match(ip) or not port.isdigit() or not 0 < int(port) < 65536:
            return None
        user = parts[2].strip() if len(parts) >= 3 else ""
        password = parts[3].strip() if len(parts) >= 4 else ""
        return ip, port, user, password

    def _refresh(self) -> None:
        try:
            for line in self._fetch(self._url):
                parsed = self.parse_line(line)
                if parsed:
                    self._proxies.append(parsed)
        except Exception:  # noqa: BLE001 - 拉取失败保留旧池,下次再刷新
            return
        self._last = time.time()

    def get_proxies(self) -> dict[str, str] | None:
        """返回一个随机代理的 requests proxies 字典;池空返回 `None`。"""
        with self._lock:
            if not self._proxies or time.time() - self._last > self._ttl:
                self._proxies = []
                self._refresh()
            if not self._proxies:
                return None
            ip, port, user, pwd = random.choice(self._proxies)
            # 无鉴权(IP 白名单型)时不要拼成 http://:@ip:port
            if user:
                url = f"http://{user}:{pwd}@{ip}:{port}"
            else:
                url = f"http://{ip}:{port}"
            return {"http": url, "https": url}


def _get_pool(settings: Settings) -> ProxyPool | None:
    """按提取 URL 取(或创建)代理池。"""
    if not settings.proxy_extract_url:
        return None
    url = settings.proxy_extract_url.strip()
    with _pool_lock:
        pool = _pool_cache.get(url)
        if pool is None:
            pool = ProxyPool(url, settings.proxy_refresh_seconds)
            _pool_cache[url] = pool
    return pool


def get_proxies(settings: Settings) -> dict[str, str] | None:
    """构造作用于 requests 的 proxies 字典(见模块 docstring 规则)。"""
    pool = _get_pool(settings)
    if pool is not None:
        return pool.get_proxies()
    url = proxy_url(settings)
    if url is None:
        return None
    return {"http": url, "https": url}
