"""外链抓取安全守卫:拒绝解析到内网/保留地址的 URL(SSRF 防护)。

背景:文章正文自抓/元信息解析的 URL 来自用户输入(开放注册),
不设防时 `http://169.254.169.254/...`、`http://192.168.x.x` 可被服务器
代为访问,响应体还会经文章 content 回显给用户(读内网)。
仅做最小防护:协议白名单 + 解析 IP 拒绝私有/回环/链路本地/保留段。
DNS 重绑定(TOCTOU)未覆盖——要彻底防需连接级钳制,超出本项目需要。
"""
from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlparse


# 代理凭证掩码:走带鉴权代理(http://user:pass@host)时,底层 urllib3/curl_cffi 的
# 连接异常 repr 会把整条含账密的代理 URL 写进异常文本。这些文本会流向两处闸口:
# 1) RunRecord.detail(健康页全体租户可见 + 飞书采集失败告警);2) 采集/榜单 HTTP 500 响应。
# 统一用本函数抹掉 ://user:pass@ 段,保留主机端口便于排障。
_PROXY_CRED_RE = re.compile(r"://[^/@\s]+@")


def redact_proxy_creds(text: str) -> str:
    """把文本中 `://user:pass@host` 的用户名/密码段替换为 `://***@`。"""
    return _PROXY_CRED_RE.sub("://***@", text or "")


class UnsafeUrlError(ValueError):
    """URL 被安全守卫拒绝(协议不合法或解析到内网地址)。"""


def assert_public_url(url: str) -> None:
    """校验 URL 可被服务器安全抓取;不合法抛 UnsafeUrlError。"""
    parsed = urlparse(url or "")
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise UnsafeUrlError(f"仅允许 http/https 外链: {str(url)[:80]}")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    _assert_public_host(parsed.hostname, port)


def assert_public_host(host: str, port: int) -> None:
    """校验 SMTP 等"非 URL 但由服务器主动外连"的目标主机可公开访问。

    与 `assert_public_url` 同一判定,只是入口是 (host, port) 二元组——用户可控的
    SMTP host 若原样喂给 smtplib.SMTP_SSL,容器即可被当作内网端口探测跳板
    (`host=mysql:3306`、`host=169.254.169.254:80` 等),连接失败的 banner/errno
    还会经 ERROR 日志形成弱读回通道。
    """
    return _assert_public_host(host, port)


def _assert_public_host(host: str | None, port: int) -> None:
    if not host:
        raise UnsafeUrlError("主机名为空")
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeUrlError(f"域名解析失败: {host}") from exc
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            raise UnsafeUrlError(f"拒绝访问内网/保留地址: {ip}")
