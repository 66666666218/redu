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

import logging
import random
import re
import socket
import threading
import time
from urllib.parse import urlsplit

import requests

from config import Settings

logger = logging.getLogger(__name__)

# 提取式代理池缓存(按提取 URL 隔离)。
_pool_cache: dict[str, "ProxyPool"] = {}
_pool_lock = threading.Lock()
_IP_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")

#: 系统代理探活结果缓存(`url -> (是否可达, 采样时刻)`)。
#: ⚠️ 必须缓存:探活是一次 TCP 连接,而调用方可能每轮/每号都问一次;
#: 也不能缓存太久 —— 代理软件是**随时可能启停**的,缓存过了头就会拿着旧结论做事。
_ALIVE_TTL = 60.0
_alive_cache: dict[str, tuple[bool, float]] = {}
_alive_lock = threading.Lock()


def _proxy_host_port(url: str) -> tuple[str, int]:
    """`http://user:pass@127.0.0.1:7892` → `("127.0.0.1", 7892)`。

    没写 scheme 时按 http 处理(与 `_normalize_proxy_url` 同口径);
    没写端口时按 scheme 的默认端口(80/443/1080)——
    **不能当成"没有代理"**,否则死代理会被静默放过。
    """
    text = (url or "").strip()
    if not text:
        return "", 0
    if "://" not in text:
        text = "http://" + text
    try:
        parts = urlsplit(text)
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        return "", 0
    if not host:
        return "", 0
    if port is None:
        port = {"https": 443, "socks5": 1080, "socks5h": 1080,
                "socks4": 1080}.get((parts.scheme or "http").lower(), 80)
    return host, int(port)


def system_proxy_alive(url: str, *, timeout: float = 1.5) -> bool:
    """系统代理**现在**还有进程在听吗?空 URL 视为"没有代理"→ False。

    ## 为什么必须有这一条(2026-10-22 实测事故)
    Windows 上代理软件(Clash 等)退出时**不会还原注册表** `ProxyEnable=1`,
    于是系统里留着一条指向死端口(实测 `127.0.0.1:7892`)的代理:

        requests               → ProxyError
        Playwright/Chromium    → net::ERR_PROXY_CONNECTION_FAILED
        MediaCrawler(子进程)   → 整条平台采集退出码 1

    而 `disable_env_proxies()` 只 patch 得到**本进程里的 requests**;
    浏览器、子进程、以及 `xhs_net` 那条"故意读系统代理"的路**都不听它的**。
    ⇒ 需要一个能回答"这条代理还活着吗"的判据,让调用方**回落直连**而不是硬撞死端口。

    ⚠️ 探活失败 ≠ 代理一定不可用(也可能是本机防火墙慢),所以调用方只该用它
    决定"要不要显式改走直连",**不该据此报故障**;真正的失败仍由业务请求本身暴露。
    """
    host, port = _proxy_host_port(url)
    if not host or not port:
        return False
    key = f"{host}:{port}"
    now = time.time()
    with _alive_lock:
        hit = _alive_cache.get(key)
        if hit and now - hit[1] < _ALIVE_TTL:
            return hit[0]
    ok = False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            ok = True
    except OSError:
        ok = False
    with _alive_lock:
        _alive_cache[key] = (ok, now)
    return ok


def system_proxy_alive_now(url: str, *, timeout: float = 1.5) -> bool:
    """不做缓存地探一次(代理**刚启动/刚退出**时用它,别拿 60 秒前的结论)。"""
    host, port = _proxy_host_port(url)
    if not host or not port:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _first_proxy_url(server: str) -> str:
    """Windows `ProxyServer` 的两种写法 → 一条可探活的 URL。

    · `127.0.0.1:7892`;
    · `http=127.0.0.1:7892;https=127.0.0.1:7892`(按协议分段)。

    后一种取**第一个映射值**(通常就是 http/https 同一个端口);解不出返回空串。
    """
    text = (server or "").strip()
    if not text:
        return ""
    if "=" in text:
        for part in text.split(";"):
            if "=" in part:
                return part.split("=", 1)[1].strip()
        return ""
    return text


def disable_dead_system_proxy(*, timeout: float = 1.5) -> bool:
    """★ **启动自愈**:系统代理指向死端口时,把 `ProxyEnable` 关掉。返回是否改过。

    ## 为什么必须在这里做,而不是逐个给 chromium 传直连参数
    2026-10-22 第二次踩同一个坑(第一次是 09-29)。`disable_env_proxies()` 只治
    **本进程的 requests**;而项目里 Playwright 的启动点有 **十几处**
    (`xhs_page_source` / `xunlei_captcha` / `xianyu_browser` / `douyin_cookie_export` /
    登录脚本……),MediaCrawler 还在**独立 venv 的子进程**里跑。
    逐个加 `--proxy-server=direct://` 一定会漏 —— 当天先修了小红书,迅雷 captcha 紧接着
    原地报 `ERR_PROXY_CONNECTION_FAILED`,就是证据。

    系统代理**指向一个没人监听的端口**时,它不保护任何请求,只会毒杀所有
    "自己不会显式绕开"的客户端。关掉它是**恢复净状态**,不是改变用户配置:
    代理软件下次启动通常会自己写回 `ProxyEnable=1`。

    ## 边界
    - 只在 Windows、只在**确实探不通**时改;探测失败/读不到注册表 → 什么都不做(返回 False);
    - 只写 `HKCU`(**不需要管理员**);
    - 不动 `ProxyServer` 的值 —— 用户重开代理软件时那条地址还在;
    - 失败不抛(启动流程不能被它带崩)。
    """
    import sys

    if sys.platform != "win32":
        return False
    try:
        import winreg
    except Exception:  # noqa: BLE001
        return False
    path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0,
                            winreg.KEY_READ | winreg.KEY_SET_VALUE) as key:
            try:
                enabled = int(winreg.QueryValueEx(key, "ProxyEnable")[0] or 0)
                server = str(winreg.QueryValueEx(key, "ProxyServer")[0] or "")
            except FileNotFoundError:
                return False
            if not enabled or not server:
                return False
            url = _first_proxy_url(server)
            if not url or system_proxy_alive_now(url, timeout=timeout):
                return False
            winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 0)
    except Exception as exc:  # noqa: BLE001 - 启动自愈失败只记一行,别挡住启动
        logger.warning("关闭死系统代理失败(不影响启动):%s", str(exc)[:120])
        return False
    logger.warning("系统代理 %s **无监听**(代理软件已退出?,注册表残留 ProxyEnable=1)"
                   " ⇒ 已关闭系统代理,浏览器/子进程改直连", url)
    return True


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
        # 先拉到本地新池再原子替换:旧写法假定调用方已 `self._proxies = []`,
        # 一旦厂商 getips 返 5xx/timeout/额度到期 JSON → except 分支 return,
        # 池已被清空 → 采集按"未启用代理"直连出口 IP,把服务器真身 IP 打进
        # 抖音/闲鱼风控(.env.example 里 DOUHOT_USE_PROXY 就是为这个开的)。
        new_list: list[tuple[str, str, str, str]] = []
        try:
            for line in self._fetch(self._url):
                parsed = self.parse_line(line)
                if parsed:
                    new_list.append(parsed)
        except Exception:  # noqa: BLE001 - 拉取失败保留旧池,下次再刷新
            return
        if new_list:
            self._proxies = new_list
            self._last = time.time()

    def get_proxies(self) -> dict[str, str] | None:
        """返回一个随机代理的 requests proxies 字典;池空返回 `None`。"""
        with self._lock:
            if not self._proxies or time.time() - self._last > self._ttl:
                self._refresh()  # 不再预先 self._proxies = [] —— _refresh 内部原子替换
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


def disable_env_proxies() -> None:
    """全局禁用 requests 读取环境变量/系统代理。

    Why: Windows 上代理软件(Clash 等)退出时可能不还原系统代理注册表项,
    ProxyEnable=1 指向已死端口;requests 默认 trust_env=True 会读到它,
    毒杀全部未显式指定 proxies 的请求(2026-09-29 事故:weread 81 号全失败,
    baidu/xianyu/续期同因,根因 127.0.0.1:7892 死代理)。
    本项目采集目标(weread/微博/百度/抖音/闲鱼/飞书/LLM)全部国内直连可达,
    且 wr Cookie 绑家宽出口 IP,系统代理本就有害。

    实现:patch `requests.utils.getproxies`(get_environ_proxies 的代理来源,
    Windows 上会读注册表 ProxyServer)恒返 `{}`。不用 `Session.trust_env=False`
    ——requests ≥2.33 已把 trust_env 改为**实例属性**(__init__ 里置 True),
    类属性 patch 会被覆盖(实测 2.34.2)。

    显式代理不受影响:get_proxies()/xianyu_proxy_url 走显式 proxies 参数,
    本函数只断"环境/注册表"这一条路,不改变显式传参的语义。
    """
    import requests.utils

    requests.utils.getproxies = lambda: {}
