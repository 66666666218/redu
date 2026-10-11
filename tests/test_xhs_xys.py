# -*- coding: utf-8 -*-
"""小红书**完整 `XYS_` 签名**的守卫(2026-10-11)。

⚠️ 为什么值得为它写一组:`xhshow` 那套只产出 `x0..x4`,而站点源码里 `seccore_signv2`
是 **8 个字段**(多 `x5`/`x6`/`x7`)。实测:已登录请求**缺 `x6/x7`** 时服务端回
`-100 登录已过期`(而同一枚 cookie 在页面里明明是登录态)—— 所以这三个字段不是装饰。

代码逐字来自站点自己的 bundle(`vendor-dynamic.*.js`):
`S = nonce(4) ‖ sha1(ssk‖nonce) ‖ ssk[32:]`,`encSskSign = b64(sha1(u‖S))`。
这里用**独立的实现**把公式重算一遍对拍,防止哪天被"顺手改坏"。
"""
from __future__ import annotations

import base64
import hashlib
import json

from app.services import xhs_xys as X


# --------------------------------------------------------------- ① encSsk 公式


def test_build_enc_ssk_matches_the_documented_formula(monkeypatch):
    """★ 用**独立实现**重算一遍对拍。

    ⚠️ **时间也必须冻住**:nonce 里带毫秒时间戳,只要跨过一毫秒,重算就对不上 ——
    这测试第一版就是这么"偶发红"的(不是代码坏,是测试自己不确定)。
    """
    ssk = bytes(range(32)) + b"tail-bytes"        # 比 32 长,才能验证 ssk[32:] 那段
    u_hex = "f6a462b6b0226d11b4c461e220029951"
    monkeypatch.setattr(X.os, "urandom", lambda n: b"\x01\x02\x03\x04")
    monkeypatch.setattr(X.time, "time", lambda: 1_700_000_000.123)

    sign_v, ssk_v = X.build_enc_ssk(u_hex, ssk)

    # 独立重算:nonce = uint32LE(now + uint32LE(random4))
    r = int.from_bytes(b"\x01\x02\x03\x04", "little")
    nonce = ((int(1_700_000_000.123 * 1000) & 0xFFFFFFFF) + r & 0xFFFFFFFF).to_bytes(4, "little")
    payload = nonce + hashlib.sha1(ssk + nonce).digest() + ssk[32:]
    assert ssk_v == base64.b64encode(payload).decode()
    assert sign_v == base64.b64encode(hashlib.sha1(u_hex.encode() + payload).digest()).decode()
    assert len(base64.b64decode(ssk_v)) == 4 + 20 + len(ssk) - 32


def test_build_enc_ssk_rejects_short_ssk():
    """`ssk` 短于 32 字节 ⇒ 回空串(宁可不带 `x6/x7`,也不要发一个半截的)。"""
    assert X.build_enc_ssk("u", b"") == ("", "")
    assert X.build_enc_ssk("u", b"x" * 31) == ("", "")
    assert X.build_enc_ssk("u", b"x" * 32)[0] != ""


# --------------------------------------------------------------- ② content 拼法


def test_content_string_matches_the_js_recipe():
    """`content = 真实URL(含 query) + 紧凑 JSON body` —— 与源码逐字一致。"""
    assert X.content_string("/a", None, {"qr_type": 1}) == '/a{"qr_type":1}'
    assert X.content_string("/a", {"x": "1", "y": "a b"}, None) == "/a?x=1&y=a%20b"
    assert X.content_string("/a", {"x": None}, None) == "/a"        # None 不进 query
    assert X.content_string("/a", None, None) == "/a"


# --------------------------------------------------------------- ③ 整份签名


class _FakeSigner:
    """只提供 `xhs_xys` 用到的那几个方法(替身必须与真签名器同形,别再方言不同)。"""

    def sign_xs(self, method, uri, a1, payload=None):
        from xhshow import Xhshow

        self._lib = Xhshow()
        return self._lib.sign_xs(method, uri, a1, payload=payload)

    def decode_xs(self, xs):
        return self._lib.decode_xs(xs)

    def sign_xs_common(self, cookies):
        return self._lib.sign_xs_common(cookies)

    def get_b3_trace_id(self):
        return "b3"

    def get_xray_trace_id(self, **kw):
        return "xr"


def _signer():
    s = _FakeSigner()
    s._lib = __import__("xhshow").Xhshow()
    return s


def test_sign_xys_has_all_eight_fields_with_ssk():
    """★ 带 `ssk` 时必须**八字段齐全**,而且 `x5` = md5(content)、`x6/x7` 按 appId 分键。"""
    sg = _signer()
    xs = X.sign_xys(sg, "POST", "/api/x", "a" * 52, payload={"qr_type": 1},
                    ssk=b"k" * 32)
    S = sg.decode_xs(xs)
    assert sorted(S) == ["x0", "x1", "x2", "x3", "x4", "x5", "x6", "x7"]
    assert S["x5"] == hashlib.md5(b'/api/x{"qr_type":1}').hexdigest()
    assert S["x4"] == "object" and S["x2"] == "Windows" and S["x0"] == "4.4.3"
    assert set(S["x6"]) == {"xhs-pc-web"} and set(S["x7"]) == {"xhs-pc-web"}


def test_sign_xys_without_ssk_omits_x6_x7():
    """没有 `ssk` ⇒ **不产出** `x6/x7`(匿名那条路服务端接受,实测 200)。"""
    S = _signer().decode_xs(X.sign_xys(_signer(), "POST", "/api/x", "a" * 52,
                                       payload={"qr_type": 1}))
    assert "x6" not in S and "x7" not in S and S["x5"]


def test_sign_xys_x4_follows_body_type():
    """`x4` = body 的类型(`object`/空),照源码的 `a ? typeof a : ""`。"""
    sg = _signer()
    assert sg.decode_xs(X.sign_xys(sg, "GET", "/api/x", "a" * 52))["x4"] == ""
    assert sg.decode_xs(X.sign_xys(sg, "POST", "/api/x", "a" * 52,
                                   payload={"a": 1}))["x4"] == "object"


def test_headers_carry_signature_and_trace():
    h = X.headers(_signer(), "GET", "/api/x", {"a1": "a" * 52}, ssk=b"k" * 32)
    for k in ("x-s", "x-t", "x-s-common", "x-b3-traceid", "x-xray-traceid", "x-mns"):
        assert h.get(k), f"少了 {k}"
    assert h["x-s"].startswith("XYS_")


# --------------------------------------------------------------- ④ ssk 存取


def _tmp_db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db.models import Base

    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng)()


def test_ssk_roundtrip_and_missing_is_empty():
    """存读成对;读不到一律回空字节(**不抛** —— 没 ssk 只是退化成匿名签名)。"""
    db = _tmp_db()
    try:
        assert X.load_ssk(db, 1) == b""                  # 没存过
        X.save_ssk(db, 1, base64.b64encode(b"s" * 32).decode())
        db.commit()
        assert X.load_ssk(db, 1) == b"s" * 32
        X.save_ssk(db, 1, base64.b64encode(b"t" * 32).decode())   # 覆盖写
        db.commit()
        assert X.load_ssk(db, 1) == b"t" * 32
    finally:
        db.close()


def test_ssk_key_is_per_user():
    """不同用户各存各的 —— 合成一个键会让两个号互相污染会话密钥。"""
    db = _tmp_db()
    try:
        X.save_ssk(db, 1, base64.b64encode(b"a" * 32).decode())
        X.save_ssk(db, 2, base64.b64encode(b"b" * 32).decode())
        db.commit()
        assert X.load_ssk(db, 1) == b"a" * 32 and X.load_ssk(db, 2) == b"b" * 32
    finally:
        db.close()


def test_json_is_compact_like_js():
    """`JSON.stringify` 是紧凑且不转义非 ASCII 的 —— 差一个空格签名就废。"""
    sg = _signer()
    S = sg.decode_xs(X.sign_xys(sg, "POST", "/api/x", "a" * 52, payload={"标题": "值"}))
    want = json.dumps({"标题": "值"}, separators=(",", ":"), ensure_ascii=False)
    assert S["x5"] == hashlib.md5(("/api/x" + want).encode()).hexdigest()
