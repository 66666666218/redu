"""百度扫码登录的**纯逻辑守卫**(2026-10-27)。

只测不碰网络的部分 —— 扫码本身要人拿手机扫,没法进单测。但最容易出错的恰好是
"把**还在等**误读成**出错**"(`errno:1` 是长轮询的正常返回)与"确认后取码判据",
这两处必须钉死:
- `parse_status`:`errno:1` ⇒ `None`(继续等),不是异常;
- `missing_required`:缺 `STOKEN` 必须被认出来(半登录会「登录检查过、转存全失败」);
- `complete`:**唯一判据是会话里出现 `BDUSS`**,不是某个接口回 200。
"""
from __future__ import annotations

import requests
from requests.cookies import RequestsCookieJar, create_cookie

from app.services import baidu_qr_login as L


def test_parse_status_long_poll_no_change_is_none():
    #: 实测形状:没变化时服务端回 `{"errno":1}` —— 这是"继续等",绝不能当错误
    assert L.parse_status({"errno": 1}) is None


def test_parse_status_reads_channel_v_both_shapes():
    assert L.parse_status({"errno": 0, "channel_v": '{"status": 1}'}) == 1
    assert L.parse_status({"errno": 0, "channel_v": {"status": 2}}) == 2


def test_parse_status_garbage_is_none():
    assert L.parse_status(None) is None
    assert L.parse_status({}) is None
    assert L.parse_status({"errno": 0, "channel_v": "not-json"}) is None
    assert L.parse_status({"errno": 0, "channel_v": {"status": "x"}}) is None


def test_missing_required_points_at_stoken():
    assert L.missing_required("BDUSS=a; STOKEN=b") == []
    #: 只有 BDUSS 的「半登录」必须被拒
    assert L.missing_required("BDUSS=a") == ["STOKEN"]
    assert L.missing_required("") == ["BDUSS", "STOKEN"]


def test_wait_for_login_returns_on_confirm(monkeypatch):
    seq = iter([None, 0, 1, 2])
    monkeypatch.setattr(L, "poll", lambda ticket, timeout=0: (next(seq), ""))
    seen: list[int | None] = []
    ticket = L.QrTicket(session=None, sign="s", img_url="u", gid="g")  # type: ignore[arg-type]
    assert L.wait_for_login(ticket, on_status=lambda s, d: seen.append(s),
                            deadline_s=5) == 2
    assert seen == [None, 0, 1, 2]


def test_wait_for_login_expired(monkeypatch):
    monkeypatch.setattr(L, "poll", lambda ticket, timeout=0: (3, ""))
    ticket = L.QrTicket(session=None, sign="s", img_url="u", gid="g")  # type: ignore[arg-type]
    try:
        L.wait_for_login(ticket, deadline_s=5)
    except L.BaiduQrError as exc:
        assert exc.kind == "expired"
    else:  # pragma: no cover
        raise AssertionError("过期必须抛错,不能当成功")


class _Resp:
    def __init__(self, status_code: int = 302, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


class _Session:
    """假的 requests.Session:第一次取码就把 BDUSS 塞进 cookie jar。

    ⚠️ 只模拟「**会话里出现 BDUSS**」这一条判据 —— 那正是 `complete` 唯一认的。
    """

    def __init__(self) -> None:
        self.cookies = RequestsCookieJar()
        self.calls: list[str] = []

    def request(self, method, url, **kwargs):  # noqa: ANN001, ANN003
        self.calls.append(f"{method} {url}")
        if len(self.calls) == 1:
            self.cookies.set_cookie(create_cookie("BDUSS", "bduss-value", domain=".baidu.com"))
            self.cookies.set_cookie(create_cookie("STOKEN", "stoken-value", domain=".baidu.com"))
            self.cookies.set_cookie(create_cookie("OTHER", "x", domain="example.com"))
        return _Resp()


def test_complete_hit_is_bduss_not_http_200():
    sess = _Session()
    ticket = L.QrTicket(session=sess, sign="s", img_url="u", gid="g")  # type: ignore[arg-type]
    cookie = L.complete(ticket)
    assert "BDUSS=bduss-value" in cookie
    assert "STOKEN=stoken-value" in cookie
    #: 非 baidu.com 域的 cookie 不许混进来
    assert "OTHER=" not in cookie
    assert ticket.grant_hit


def test_complete_fails_when_no_bduss():
    class _NoBduss(_Session):
        def request(self, method, url, **kwargs):  # noqa: ANN001, ANN003
            self.calls.append(f"{method} {url}")
            return _Resp(200, '{"errno":0}')

    ticket = L.QrTicket(session=_NoBduss(), sign="s", img_url="u", gid="g")  # type: ignore[arg-type]
    try:
        L.complete(ticket)
    except L.BaiduQrError as exc:
        assert exc.kind == "grant"
    else:  # pragma: no cover
        raise AssertionError("没取到 BDUSS 必须报错,不能返回半截 cookie")


def test_verify_rejects_empty_without_network():
    got = L.verify("")
    assert got["ok"] is False
