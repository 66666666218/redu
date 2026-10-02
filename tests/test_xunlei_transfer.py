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
