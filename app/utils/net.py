"""外链抓取安全守卫:拒绝解析到内网/保留地址的 URL(SSRF 防护)。

背景:文章正文自抓/元信息解析的 URL 来自用户输入(开放注册),
不设防时 `http://169.254.169.254/...`、`http://192.168.x.x` 可被服务器
代为访问,响应体还会经文章 content 回显给用户(读内网)。
仅做最小防护:协议白名单 + 解析 IP 拒绝私有/回环/链路本地/保留段。
DNS 重绑定(TOCTOU)未覆盖——要彻底防需连接级钳制,超出本项目需要。
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


class UnsafeUrlError(ValueError):
    """URL 被安全守卫拒绝(协议不合法或解析到内网地址)。"""


def assert_public_url(url: str) -> None:
    """校验 URL 可被服务器安全抓取;不合法抛 UnsafeUrlError。"""
    parsed = urlparse(url or "")
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise UnsafeUrlError(f"仅允许 http/https 外链: {str(url)[:80]}")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:  # 域名不存在/本地 DNS 异常
        raise UnsafeUrlError(f"域名解析失败: {parsed.hostname}") from exc
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            raise UnsafeUrlError(f"拒绝访问内网/保留地址: {ip}")
