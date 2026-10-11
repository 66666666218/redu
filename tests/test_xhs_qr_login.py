# -*- coding: utf-8 -*-
"""小红书扫码登录的守卫(2026-10-11)。

守两类东西:
1. **签名用的字节 = 发出去的字节** —— GET 的 query 拼法必须与 `xhshow` 内部
   **逐字节一致**(x5 是拿它算的;上游原话:漏了 query "直接被网关拒"),
   POST 的 body 必须用库的 `build_json_body()`,不许自己 `json.dumps` 一遍。
2. **判据不许被放宽** —— 没拿到 `login_info` 就不算确认;`code_status==3` 就是过期;
   通道失败**必须**在 `error` 里现身(否则"签名挂了"会被伪装成"还在等扫码",
   而上游《login-flow》第五节把这条列为纪律)。
"""
from __future__ import annotations

import io
import re
from pathlib import Path

import pytest

from app.services import xhs_login as L


# --------------------------------------------------------------- ① query 拼法对拍


def test_query_matches_library_byte_for_byte():
    """★ 我们拼的 query 必须与库算签名时拼的**一模一样**(否则 x5 对不上)。"""
    from xhshow import Xhshow

    cases = [
        {"a": "1", "b": "2"},
        # base64 里的 `+` `/` `=` 必须被转义 —— 公钥参数正是这种形状
        {"client_public_key_base64": "AbC+d/ef==", "qr_id": "323181791698960973"},
        {"k": None, "e": ""},
        {"list": ["x", "y"]},
        {"code": "641540", "qr_id": "1"},
    ]
    for params in cases:
        ours = L._query(params)
        lib = Xhshow()._build_content_string("GET", "/x", params)
        assert lib == f"/x?{ours}", f"拼法不一致:{params} → 我们 {ours!r} vs 库 {lib!r}"


def test_query_escapes_base64_chars():
    """`+` `/` `=` 必须转义(base64 公钥里全有)。"""
    q = L._query({"k": "a+b/c="})
    assert "%2B" in q or "%2b" in q, q
    assert "%2F" in q or "%2f" in q, q
    assert "%3D" in q or "%3d" in q, q


def test_post_body_uses_library_serializer():
    """POST body 必须走库的 `build_json_body`(紧凑),源码里不许自己 `json.dumps` 出来发。"""
    src = io.open(Path(L.__file__), encoding="utf-8").read()
    assert "build_json_body" in src, "没用库的序列化器 —— 签名与发送会各序列化一次"
    assert not re.search(r"data=json\.dumps", src), "发送侧自己 json.dumps 了,byte 对不上"


# --------------------------------------------------------------- ② 身份与签名器


def test_new_identity_is_fresh_each_time():
    """每次都要**全新的 a1**(新设备);两个 a1 不能撞。"""
    c1, s1 = L.new_identity()
    c2, _ = L.new_identity()
    assert c1["a1"] and c2["a1"] and c1["a1"] != c2["a1"]
    assert c1["webId"] and c1["webId"] != c2["webId"]
    # 签名器必须是从**那一个门**出来的(带模板版本补丁)
    assert dict(s1.config.SIGNATURE_DATA_TEMPLATE)["x0"] == "4.4.3"


# --------------------------------------------------------------- ③ 轮询判据


class _FakeSigner:
    """占位签名器 —— 测试里 `_request` 被替换,这里只用来占位。"""

    def build_json_body(self, payload):  # pragma: no cover - 不走真路径
        return "{}"


def _ticket() -> L.QrTicket:
    return L.QrTicket(qr_id="123", code="456", url="https://x/y",
                      cookies={"a1": "a"}, signer=_FakeSigner())


def test_poll_does_not_invent_success(monkeypatch):
    """两条通道都回"还在等" ⇒ **不能**算确认。"""
    monkeypatch.setattr(L, "_request", lambda *a, **k: {"data": {"code_status": 0}})
    r = L.poll_once(_ticket())
    assert r.code_status == 0 and not r.confirmed and not r.expired and not r.error


def test_poll_takes_credentials_only_from_status_channel(monkeypatch):
    """凭据只认 `login_info`;`codeStatus==2` 单飞**不算**成功(凭据还没下发)。"""
    def fake(signer, cookies, *, method, path, **kw):
        if path == L.PATH_QR_STATUS:
            return {"data": {"code_status": 2, "login_info": {}}}
        return {"data": {"codeStatus": 2}}

    monkeypatch.setattr(L, "_request", fake)
    r = L.poll_once(_ticket())
    assert r.code_status == 2
    assert not r.login_info.get("session"), "没下发凭据就当成拿到了?"


def test_poll_reads_login_info(monkeypatch):
    def fake(signer, cookies, *, method, path, **kw):
        if path == L.PATH_QR_STATUS:
            return {"data": {"code_status": 2, "login_info": {"session": "040069xyz",
                                                              "user_id": "u1"}}}
        return {"data": {"codeStatus": 2}}

    monkeypatch.setattr(L, "_request", fake)
    r = L.poll_once(_ticket())
    assert r.confirmed and r.login_info["session"].startswith("04")


def test_poll_expiry_follows_userinfo(monkeypatch):
    """过期**以 `userinfo` 为准**(两条通道语义不同域)。"""
    def fake(signer, cookies, *, method, path, **kw):
        if path == L.PATH_QR_STATUS:
            return {"data": {"code_status": 0}}      # status 说"还没动"
        return {"data": {"codeStatus": 3}}           # userinfo 才说过期了

    monkeypatch.setattr(L, "_request", fake)
    assert L.poll_once(_ticket()).expired


def test_poll_surfaces_channel_errors(monkeypatch):
    """★ 通道失败**必须**露出来 —— 不许伪装成"还在等扫码"。"""
    def boom(*a, **k):
        raise L.XhsQrLoginError("签名被拒(HTTP 406)", kind="sign")

    monkeypatch.setattr(L, "_request", boom)
    r = L.poll_once(_ticket())
    assert "406" in r.error and not r.confirmed


def test_poll_keeps_credentials_when_userinfo_fails(monkeypatch):
    """userinfo 挂了但 status 已经给了凭据 ⇒ 仍然算成功(凭据才是真东西)。"""
    def fake(signer, cookies, *, method, path, **kw):
        if path == L.PATH_QR_STATUS:
            return {"data": {"login_info": {"session": "040069xyz"}}}
        raise L.XhsQrLoginError("网络抖了一下", kind="network")

    monkeypatch.setattr(L, "_request", fake)
    r = L.poll_once(_ticket())
    assert r.confirmed and not r.error


# --------------------------------------------------------------- ④ 等待循环


def test_poll_marks_busy_when_all_rounds_are_471(monkeypatch):
    """471(服务端忙)是**正常波动** —— 单列成 `busy`,不按"链坏了"告警。"""
    def busy(*a, **k):
        raise L.XhsQrLoginError("服务端忙/限频(HTTP 471)", kind="busy")

    monkeypatch.setattr(L, "_request", busy)
    r = L.poll_once(_ticket())
    assert r.busy and r.error and not r.confirmed


def test_poll_not_busy_when_one_channel_is_a_real_error(monkeypatch):
    """只要有一路是**真错**(不是 471),就不能标成 busy —— 否则会把签名失效说成"忙"。"""
    def fake(signer, cookies, *, method, path, **kw):
        if path == L.PATH_QR_STATUS:
            raise L.XhsQrLoginError("服务端忙/限频(HTTP 471)", kind="busy")
        raise L.XhsQrLoginError("签名被拒(HTTP 461)", kind="sign")

    monkeypatch.setattr(L, "_request", fake)
    r = L.poll_once(_ticket())
    assert r.error and not r.busy


def test_wait_rides_through_busy_rounds(monkeypatch):
    """★ 忙的轮次要**继续等**,不能被当成失败中止(否则一次 471 就白等一次)。"""
    seq = [L.PollResult(error="服务端忙(471)", busy=True),
           L.PollResult(error="服务端忙(471)", busy=True),
           L.PollResult(code_status=2, login_info={"session": "040069ok"})]
    it = iter(seq)
    monkeypatch.setattr(L, "poll_once", lambda t: next(it))
    assert L.wait_for_login(_ticket(), deadline_s=5, interval_s=0)["session"] == "040069ok"


def test_wait_raises_on_expiry(monkeypatch):
    monkeypatch.setattr(L, "poll_once", lambda t: L.PollResult(code_status=3, expired=True))
    with pytest.raises(L.XhsQrLoginError) as e:
        L.wait_for_login(_ticket(), deadline_s=5, interval_s=0)
    assert e.value.kind == "expired"


def test_wait_raises_on_timeout(monkeypatch):
    monkeypatch.setattr(L, "poll_once", lambda t: L.PollResult(code_status=0))
    with pytest.raises(L.XhsQrLoginError) as e:
        L.wait_for_login(_ticket(), deadline_s=0, interval_s=0)
    assert e.value.kind == "timeout"


def test_wait_returns_login_info(monkeypatch):
    monkeypatch.setattr(L, "poll_once",
                        lambda t: L.PollResult(code_status=2, login_info={"session": "040069"}))
    assert L.wait_for_login(_ticket(), deadline_s=5, interval_s=0)["session"] == "040069"


# --------------------------------------------------------------- ⑤ 会话落地


def test_adopt_session_writes_web_session():
    t = _ticket()
    ck = L.adopt_session(t, {"session": "040069abc"})
    assert ck["web_session"] == "040069abc" and ck["a1"] == "a" and "web_session" not in t.cookies


def test_adopt_session_refuses_empty():
    with pytest.raises(L.XhsQrLoginError):
        L.adopt_session(_ticket(), {})


def test_create_qr_rejects_bad_response(monkeypatch):
    monkeypatch.setattr(L, "_request", lambda *a, **k: {"code": -1, "msg": "拒绝"})
    with pytest.raises(L.XhsQrLoginError):
        L.create_qr()


def test_create_qr_no_fallback_on_business_error(monkeypatch):
    """★ 只有**签名被拒**才降级试下一种格式;业务错降级毫无意义(只会多打一次)。"""
    calls: list[str] = []

    def fake(signer, cookies, *, method, path, **kw):
        calls.append(str(kw.get("sign_format")))
        return {"code": -1, "msg": "业务拒绝"}

    monkeypatch.setattr(L, "_request", fake)
    with pytest.raises(L.XhsQrLoginError):
        L.create_qr()
    assert calls == ["xyw"], f"业务错也去试了别的格式:{calls}"


def test_sign_format_ladder_degrades_once(monkeypatch):
    """`xyw` 被签名拒 ⇒ 试 `xys`,并把**成的那一个**记进 ticket(后续同款)。"""
    seen: list[str] = []

    def fake(signer, cookies, *, method, path, **kw):
        seen.append(str(kw.get("sign_format")))
        if kw.get("sign_format") == "xyw":
            raise L.XhsQrLoginError("签名被拒(HTTP 461)", kind="sign")
        return {"code": 0, "data": {"qr_id": "9", "code": "8", "url": "https://x/y"}}

    monkeypatch.setattr(L, "_request", fake)
    t = L.create_qr()
    assert seen == ["xyw", "xys"] and t.sign_format == "xys"
