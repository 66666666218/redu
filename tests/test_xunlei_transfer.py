"""迅雷转存单测(2026-10-02):签名纯函数、分享链解析、JWT 读过期。

签名函数是**纯函数**,值可以固化下来 —— 一旦有人改动算法,这里立刻红,防止
"改了一个字符导致所有请求 401"这类难查问题。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")


def test_captcha_sign_is_stable_and_pure() -> None:
    """captcha 签名:同输入必同输出,且**换 client_id 就变**(这是"自取 captcha 失败"的根因)。"""
    from app.services.xunlei_transfer import _sign_with_timestamp

    a = _sign_with_timestamp("dev123", "1790912366778")
    b = _sign_with_timestamp("dev123", "1790912366778")
    assert a == b and a.startswith("1.") and len(a) == 34
    assert _sign_with_timestamp("dev123", "1790912366778", "OtherClient") != a
    assert _sign_with_timestamp("dev123", "1790912366779") != a     # 时间戳变→签名变


def test_device_sign_shape() -> None:
    """设备签名:`div101.` 前缀 + device_id,MD5 部分 32 位。"""
    from app.services.xunlei_transfer import _device_sign

    s = _device_sign("abc123")
    assert s.startswith("div101.abc123")
    assert len(s) == len("div101.") + 6 + 32


def test_extract_share_id_with_and_without_pwd() -> None:
    """`pan.xunlei.com/s/<id>?pwd=xxxx` → (id, 提取码);没有 pwd 时提取码为空。"""
    from app.services.xunlei_transfer import _extract_share_id

    assert _extract_share_id("https://pan.xunlei.com/s/VOnAjISnQi6fuqpFWROV4gnnA1?pwd=abcd") \
        == ("VOnAjISnQi6fuqpFWROV4gnnA1", "abcd")
    assert _extract_share_id("https://pan.xunlei.com/s/XYZ") == ("XYZ", "")
    assert _extract_share_id("https://pan.xunlei.com/s/XYZ?pwd=")[0] == "XYZ"


def test_jwt_exp_and_sub_read_from_payload() -> None:
    """从 JWT payload 里读过期时间与用户 id(access_token 的有效期判断靠它)。"""
    import base64
    import json

    from app.services.xunlei_transfer import _jwt_exp, _jwt_sub

    def _mk(payload: dict) -> str:
        enc = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        return f"head.{enc}.sig"

    tok = _mk({"exp": 1790955516, "sub": "1050028885"})
    assert _jwt_exp(tok) == 1790955516
    assert _jwt_sub(tok) == "1050028885"
    assert _jwt_exp("garbage") == 0.0          # 解析不了 → 0(当作已过期,会去刷新)


def test_transfer_without_credentials_fails_gracefully(monkeypatch) -> None:
    """没凭据时返回结构化失败,不抛异常(上层要拿它决定是否回落原链推送)。"""
    from app.services import xunlei_transfer as xt

    monkeypatch.setattr(xt, "_credentials", lambda settings=None: {})
    out = xt.transfer_and_share("https://pan.xunlei.com/s/ABC")
    assert out["status"] == "failed" and "凭据" in out["message"]
    assert xt.verify()["ok"] is False


def test_trace_file_ids_handles_multiple_shapes() -> None:
    """任务结果里文件 id 可能是数组、JSON 串、或顶层 file_id —— 三种都要兜住。"""
    from app.services.xunlei_transfer import _trace_file_ids

    assert _trace_file_ids({"file_ids": ["a", "b"]}) == ["a", "b"]
    assert _trace_file_ids({"trace_file_ids": '["c"]'}) == ["c"]
    assert _trace_file_ids({"file_id": "d"}) == ["d"]
    assert _trace_file_ids({}) == []


# ---------------------------------------------------------------- captcha 失效自愈(2026-10-02)

import json  # noqa: E402

import pytest  # noqa: E402


class _FakeResp:
    def __init__(self, payload, status: int = 200):
        self._p, self.status_code = payload, status

    def json(self):
        return self._p


def test_json_raises_expired_on_captcha_invalid() -> None:
    """盘接口回 `captcha_invalid` 时要抛专用异常,交给上层续期重试(其它错误照常返回)。"""
    from app.services.xunlei_transfer import _CaptchaExpired, _json

    with pytest.raises(_CaptchaExpired):
        _json(_FakeResp({"error": "captcha_invalid", "error_description": "验证码无效"}))
    assert _json(_FakeResp({"error": "file_not_found"}))["error"] == "file_not_found"


def test_with_captcha_retry_renews_once_then_gives_up() -> None:
    """只重试一次:第一次抛 → 重铸 → 再跑;再抛就往外抛(说明续期本身有问题)。"""
    from app.services.xunlei_transfer import (_CaptchaExpired, _with_captcha_retry)
    import app.services.xunlei_transfer as xt

    tries = {"n": 0, "renew": 0}

    def flaky():
        tries["n"] += 1
        if tries["n"] == 1:
            raise _CaptchaExpired("x")
        return "ok"

    orig = xt._renew_captcha
    xt._renew_captcha = lambda: (tries.__setitem__("renew", tries["renew"] + 1) or True)
    try:
        assert _with_captcha_retry(flaky) == "ok"
        assert (tries["n"], tries["renew"]) == (2, 1)

        def always():
            raise _CaptchaExpired("x")

        with pytest.raises(_CaptchaExpired):
            _with_captcha_retry(always)
    finally:
        xt._renew_captcha = orig


def test_transfer_renews_captcha_and_succeeds(monkeypatch) -> None:
    """端到端关键路径:详情第一次回 captcha_invalid → **自动重铸** → 重试成功。"""
    from app.services import xunlei_transfer as xt

    calls = {"renew": 0, "detail": 0}
    detail_ok = {"share_status": "OK", "pass_code_token": "t", "files": [{"id": "F1"}]}
    task_ok = {"progress": 100, "params": {"trace_file_ids": json.dumps({"F1": "NEW1"})}}
    share_ok = {"share_url": "https://pan.xunlei.com/s/OUR", "pass_code": "9"}

    def fake_get(url, **kw):
        if url.endswith("/drive/v1/share"):
            calls["detail"] += 1
            if calls["detail"] == 1:
                return _FakeResp({"error": "captcha_invalid", "error_description": "验证码无效"})
            return _FakeResp(detail_ok)
        return _FakeResp(task_ok)                       # /drive/v1/tasks/xxx

    def fake_post(url, **kw):
        if url.endswith("/drive/v1/share/restore"):
            return _FakeResp({"restore_task_id": "T1"})
        return _FakeResp(share_ok)

    monkeypatch.setattr(xt, "_credentials", lambda settings=None: {
        "access_token": "a", "captcha_token": "c", "device_id": "d", "client_id": "x"})
    monkeypatch.setattr(xt, "_drive_headers", lambda cred: {})
    monkeypatch.setattr(xt.requests, "get", fake_get)
    monkeypatch.setattr(xt.requests, "post", fake_post)
    monkeypatch.setattr(xt, "_renew_captcha",
                        lambda: (calls.__setitem__("renew", calls["renew"] + 1) or True))

    out = xt.transfer_and_share("https://pan.xunlei.com/s/S?pwd=p")
    assert out["status"] == "ok", out
    assert out["share_url"].endswith("pwd=9") and out["fid"] == "NEW1"
    assert calls["renew"] == 1


def test_transfer_says_rescan_when_renewal_fails(monkeypatch) -> None:
    """续期也失败时,要给出**明确可执行**的话(重新扫码),而不是丢个 captcha_invalid。"""
    from app.services import xunlei_transfer as xt

    def always_invalid(url, **kw):
        return _FakeResp({"error": "captcha_invalid", "error_description": "验证码无效"})

    monkeypatch.setattr(xt, "_credentials", lambda settings=None: {
        "access_token": "a", "captcha_token": "c", "device_id": "d", "client_id": "x"})
    monkeypatch.setattr(xt, "_drive_headers", lambda cred: {})
    monkeypatch.setattr(xt.requests, "get", always_invalid)
    monkeypatch.setattr(xt, "_renew_captcha", lambda: False)

    out = xt.transfer_and_share("https://pan.xunlei.com/s/S?pwd=p")
    assert out["status"] == "failed" and "重新扫码" in out["message"]


def test_retry_uses_renewed_credentials(monkeypatch) -> None:
    """⚠️ 续期写回库后,重试**必须换成新凭据** —— 继续吃闭包里的旧快照等于白续一次。

    这是真实踩过的坑:自愈跑了 28 秒(浏览器都开起来了),重试却仍用旧 captcha,
    于是又失败一次。这里把"第一次用旧、第二次用新"钉死。
    """
    from app.services import xunlei_transfer as xt

    seen: list[str] = []
    store = [{"access_token": "a", "captcha_token": "stale", "device_id": "d1", "client_id": "x"},
             {"access_token": "a", "captcha_token": "fresh", "device_id": "d2", "client_id": "x"}]
    monkeypatch.setattr(xt, "_credentials", lambda settings=None: store[min(len(seen), 1)])
    monkeypatch.setattr(xt, "_drive_headers",
                        lambda c: (seen.append(c["captcha_token"]) or {}))
    monkeypatch.setattr(xt, "_renew_captcha", lambda: True)

    def fake_get(url, **kw):
        if url.endswith("/drive/v1/share"):
            if len(seen) < 2:                       # 第一次(旧 captcha)→ 失效
                return _FakeResp({"error": "captcha_invalid"})
            return _FakeResp({"share_status": "OK", "pass_code_token": "t",
                              "files": [{"id": "F1"}]})
        return _FakeResp({"progress": 100,
                          "params": {"trace_file_ids": json.dumps({"F1": "N1"})}})

    monkeypatch.setattr(xt.requests, "get", fake_get)
    monkeypatch.setattr(xt.requests, "post", lambda url, **kw: _FakeResp(
        {"restore_task_id": "T1"} if url.endswith("/restore")
        else {"share_url": "https://pan.xunlei.com/s/OUR", "pass_code": "9"}))

    out = xt.transfer_and_share("https://pan.xunlei.com/s/S?pwd=p")
    assert out["status"] == "ok", out
    assert seen[0] == "stale" and seen[-1] == "fresh", seen
