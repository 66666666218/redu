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


def test_quota_info_self_heals_captcha_on_400(monkeypatch) -> None:
    """⚠️ 配额接口(闸门的判据)**也要 captcha**:400 `captcha_invalid` 时必须自愈重试。

    否则 captcha 一过期,闸门就"什么都不知道"而放行 —— 实测漏过一次
    (3 条被无条件搬、撞空间不足)。注意 `captcha_invalid` 是 **400** 返回的,
    所以"只在 200 时解析 JSON"的写法会让异常永远不抛、自愈永远不触发。
    """
    from app.services import xunlei_transfer as xt

    calls = {"n": 0, "renew": 0}

    def fake_get(url, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeResp({"error": "captcha_invalid", "error_description": "验证码无效"},
                             status=400)
        return _FakeResp({"quota": {"limit": "1000", "usage": "950"}})

    monkeypatch.setattr(xt, "_credentials", lambda settings=None: {"access_token": "a"})
    monkeypatch.setattr(xt, "_drive_headers", lambda c: {})
    monkeypatch.setattr(xt.requests, "get", fake_get)
    monkeypatch.setattr(xt, "_renew_captcha",
                        lambda: (calls.__setitem__("renew", calls["renew"] + 1) or True))

    xt.reset_quota_cache()          # 探针有 60s 缓存,要"冷的"必须显式清(否则受测试顺序影响)
    info = xt.quota_info()
    assert info["ratio"] == 0.95, info
    assert calls["renew"] == 1


def test_trash_files_deletes_each_id_and_reports_partial(monkeypatch) -> None:
    """删除走 `DELETE /drive/v1/files/{id}`(**不是** `files/trash`,那条实测白试过三轮),
    且**没有批量接口** —— 逐个删,部分失败要如实报出来。"""
    from app.services import xunlei_transfer as xt

    seen: list[str] = []

    def fake_delete(url, **kw):
        fid = url.rsplit("/", 1)[-1]
        seen.append(fid)
        if fid == "BAD":
            return _FakeResp({"error": "file_not_found"}, status=404)
        return _FakeResp({})

    monkeypatch.setattr(xt, "_credentials", lambda settings=None: {"access_token": "a"})
    monkeypatch.setattr(xt, "_drive_headers", lambda c: {})
    monkeypatch.setattr(xt.requests, "delete", fake_delete)

    out = xt.trash_files(["OK1", "BAD", "OK2"])
    assert seen == ["OK1", "BAD", "OK2"]
    assert out["status"] == "ok" and out["deleted"] == 2 and len(out["errors"]) == 1
    assert xt.trash_files([])["status"] == "failed"          # 空输入别发请求


def test_list_files_filters_trashed(monkeypatch) -> None:
    """⚠️ 列表接口**默认把回收站条目一起返回**(删一棵树后实测混进 2000+ 项)——
    不过滤的话扫盘会把**已删除的文件当新资源**登记,还去给它建分享链(必失败)。"""
    from app.services import xunlei_transfer as xt

    monkeypatch.setattr(xt, "_credentials", lambda settings=None: {"access_token": "a"})
    monkeypatch.setattr(xt, "_drive_headers", lambda c: {})
    monkeypatch.setattr(xt.requests, "get", lambda url, **kw: _FakeResp({"files": [
        {"id": "A", "name": "活着"},
        {"id": "B", "name": "已删", "trashed": True},
        {"id": "C", "name": "也活着"}]}))

    assert [f["id"] for f in xt.list_files("")] == ["A", "C"]
    assert [f["id"] for f in xt.list_files("", include_trashed=True)] == ["A", "B", "C"]


# ---------------------------------------------------------------- 转存落点(2026-10-02)

def test_resolve_parent_id_finds_folder_by_name(monkeypatch) -> None:
    """按**名字**找落点目录(不写死 id):目录改名/重建都能跟上。

    用户口径:"以后都存进最全文件里面"。
    """
    from app.services import xunlei_transfer as xt

    monkeypatch.setattr(xt, "_parent_cache", {"name": "", "id": "", "at": 0.0})
    monkeypatch.setattr("config.settings.get_settings",
                        lambda: type("S", (), {"xunlei_transfer_parent": "最全文件",
                                               "xunlei_transfer_parent_id": ""})())
    calls = {"n": 0}

    def fake_list(*a, **k):
        calls["n"] += 1
        return [{"id": "F1", "name": "别的目录", "kind": "drive#folder"},
                {"id": "F2", "name": "最全文件", "kind": "drive#folder"},
                {"id": "F3", "name": "最全文件", "kind": "drive#file"}]   # 同名文件不算

    monkeypatch.setattr(xt, "list_files", fake_list)
    assert xt.resolve_parent_id() == "F2"
    assert xt.resolve_parent_id() == "F2" and calls["n"] == 1        # 第二次走缓存


def test_resolve_parent_id_missing_folder_falls_back_to_root(monkeypatch) -> None:
    """目录没找到 → 落根目录(返回 ""),**不能让整条链停摆**。"""
    from app.services import xunlei_transfer as xt

    monkeypatch.setattr(xt, "_parent_cache", {"name": "", "id": "", "at": 0.0})
    monkeypatch.setattr("config.settings.get_settings",
                        lambda: type("S", (), {"xunlei_transfer_parent": "最全文件",
                                               "xunlei_transfer_parent_id": ""})())
    monkeypatch.setattr(xt, "list_files", lambda *a, **k: [
        {"id": "F1", "name": "别的", "kind": "drive#folder"}])
    assert xt.resolve_parent_id() == ""


def test_transfer_sends_files_into_configured_parent(monkeypatch) -> None:
    """转存的 restore 请求里 `parent_id` 必须是解析出来的落点目录 id。"""
    from app.services import xunlei_transfer as xt

    bodies: list[dict] = []
    monkeypatch.setattr(xt, "_credentials", lambda settings=None: {
        "access_token": "a", "captcha_token": "c", "device_id": "d", "client_id": "x"})
    monkeypatch.setattr(xt, "_drive_headers", lambda c: {})
    monkeypatch.setattr(xt, "resolve_parent_id", lambda cred=None, **k: "PARENT_DIR")
    monkeypatch.setattr(xt, "list_files", lambda *a, **k: [])

    def fake_get(url, **kw):
        if url.endswith("/drive/v1/share"):
            return _FakeResp({"share_status": "OK", "pass_code_token": "t",
                              "files": [{"id": "F1"}]})
        return _FakeResp({"progress": 100,
                          "params": {"trace_file_ids": json.dumps({"F1": "N1"})}})

    def fake_post(url, **kw):
        bodies.append(kw.get("json") or {})
        if url.endswith("/restore"):
            return _FakeResp({"restore_task_id": "T1"})
        return _FakeResp({"share_url": "https://pan.xunlei.com/s/OUR", "pass_code": "9"})

    monkeypatch.setattr(xt.requests, "get", fake_get)
    monkeypatch.setattr(xt.requests, "post", fake_post)

    assert xt.transfer_and_share("https://pan.xunlei.com/s/S?pwd=p")["status"] == "ok"
    assert bodies[0]["parent_id"] == "PARENT_DIR"       # ← restore 那条


def test_resolve_parent_id_prefers_configured_id(monkeypatch) -> None:
    """⚠️ **优先用配置的 id**:实测「最全文件」明明存在(GET by id 返回 200),
    却不出现在根目录列表里 —— 名字查找靠不住,所以配了 id 就直接用、连目录都不用扫。"""
    from app.services import xunlei_transfer as xt

    monkeypatch.setattr(xt, "_parent_cache", {"name": "", "id": "", "at": 0.0})
    monkeypatch.setattr(xt, "list_files",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("配了 id 不该扫目录")))
    monkeypatch.setattr("config.settings.get_settings",
                        lambda: type("S", (), {"xunlei_transfer_parent": "最全文件",
                                               "xunlei_transfer_parent_id": "FIXED_ID"})())
    assert xt.resolve_parent_id() == "FIXED_ID"


def test_is_dead_share_error_recognizes_banned_or_expired() -> None:
    """分享本身已死(分享者被封/过期/取消)—— 永远转不了,该标终态而不是反复 failed。"""
    from app.services import xunlei_transfer as xt

    assert xt.is_dead_share_error(
        "分享状态异常:{'error': 'get_share_user_banned', 'error_code': 7}")
    assert xt.is_dead_share_error("分享状态异常:{'share_status': 'share_overdue'}")
    assert not xt.is_dead_share_error("{'error': 'file_space_not_enough'}")
    assert not xt.is_dead_share_error("")


def _stub_request_path(monkeypatch, xt) -> None:
    """把"请求前"的凭据处理全打桩,让测试**真的走到那行 HTTP 判断**。

    ⚠️ 不打桩的话会先炸在 `_fresh_cred`(缺 refresh_token)上,测试**因错的原因通过** ——
    那只证明"会抛异常",不证明"非 200 被判成硬失败"。
    """
    monkeypatch.setattr(xt, "_credentials", lambda *a, **k: {"access_token": "x"})
    monkeypatch.setattr(xt, "_fresh_cred", lambda c: c)
    monkeypatch.setattr(xt, "_drive_headers", lambda *a, **k: {})


def test_list_files_raises_on_http_error(monkeypatch) -> None:
    """**非 200 = 硬失败**(空目录是 `200 + files: []`)。

    2026-10-03 修:此前非 200 直接 `return []`,于是凭据失效/上游故障被当成"目录是空的"。
    这里用**非 captcha** 的 500 走状态码分支(`captcha_invalid` 会先命中续期路径,见下一条)。
    """
    from app.services import xunlei_transfer as xt

    _stub_request_path(monkeypatch, xt)

    class _R:
        status_code = 500

        def json(self):
            return {"error": "server_error"}

    monkeypatch.setattr(xt.requests, "get", lambda *a, **k: _R())
    with pytest.raises(xt.XunleiDriveError) as ei:
        xt.list_files("")
    assert "HTTP 500" in str(ei.value)          # 证明走的是**状态码分支**,不是别的异常


def test_list_files_raises_when_captcha_renew_fails(monkeypatch) -> None:
    """`captcha_invalid` 走"重铸→重试"路径;**续期也失败时同样是硬失败**,不能变空表。

    这条覆盖的是另一条分支:`_json()` 在状态码判断**之前**就先抛 `_CaptchaExpired`。
    """
    from app.services import xunlei_transfer as xt

    _stub_request_path(monkeypatch, xt)
    monkeypatch.setattr(xt, "_renew_captcha", lambda: False)     # 续期失败

    class _R:
        status_code = 403

        def json(self):
            return {"error": "captcha_invalid", "error_description": "captcha_invalid"}

    monkeypatch.setattr(xt.requests, "get", lambda *a, **k: _R())
    with pytest.raises(xt.XunleiDriveError) as ei:
        xt.list_files("")
    assert "captcha_invalid" in str(ei.value)


def test_list_files_returns_empty_for_truly_empty_dir(monkeypatch) -> None:
    """真·空目录(200 + files: [])必须仍返回空表 —— 别把"失败"和"空"一起变成异常。"""
    from app.services import xunlei_transfer as xt

    _stub_request_path(monkeypatch, xt)

    class _R:
        status_code = 200

        def json(self):
            return {"files": []}

    monkeypatch.setattr(xt.requests, "get", lambda *a, **k: _R())
    assert xt.list_files("") == []


# ---------------------------------------------------------------- 单个太大 ≠ 盘满
# 2026-10-04 用户口径:「**不一定是搬不动,有没有可能是你一次搬太多**」——
# 实测盘 30.10 TiB / 已用 79.9% / **剩 6.04 TiB**,而卡住的那个包迅雷自己报要 **6.88 TiB**。
# 所以"盘满"这个结论是错的,真相是**单个资源比剩余空间大**。这两件事的运维动作完全不同。
def test_human_bytes_reads_the_measured_numbers() -> None:
    """实测的两个数字要能原样读回来(它们是这份结论的证据)。"""
    from app.services.xunlei_transfer import human_bytes

    assert human_bytes(7563939414460) == "6.88 TiB"     # 迅雷报的 required_size
    assert human_bytes(6640745233489) == "6.04 TiB"     # 当时的剩余空间
    assert human_bytes(0) == "0.00 B"
    assert human_bytes(None) == "?" and human_bytes("x") == "?"   # 不知道就不编数
    # ⚠️ 缺口要**跟着需要量选单位**:自动单位会落到 `861.88 GiB`,读的人还得自己换算成 TiB
    assert human_bytes(925431280971, ref=7563939414460) == "0.84 TiB"
    assert human_bytes(925431280971) == "861.88 GiB"


def test_required_size_is_read_from_error_details() -> None:
    """迅雷在错里**自己给了** `required_size`(还是字符串)—— 用它,别拿使用率猜。"""
    from app.services.xunlei_transfer import required_size_from_error

    resp = {"details": [{"type": "DRIVE_SPACE_NOT_ENOUGH",
                         "data": {"required_size": "7563939414460"}}]}
    assert required_size_from_error(resp) == 7563939414460
    # ⚠️ 不知道 ≠ 满:别的错误类型 / 空响应 / 结构变了,一律返回 None
    assert required_size_from_error({}) is None
    assert required_size_from_error({"details": [{"type": "OTHER", "data": {}}]}) is None
    assert required_size_from_error({"details": "not-a-list"}) is None


def test_share_required_bytes_treats_folders_as_zero() -> None:
    """分享详情里各文件 size 之和;**文件夹恒报 0** ⇒ 只能当下限(见 docstring)。"""
    from app.services.xunlei_transfer import share_required_bytes

    assert share_required_bytes({"files": [{"size": 10}, {"size": 5}]}) == 15
    assert share_required_bytes({"files": [{"size": 0}]}) is None   # 全 0 → 拿不到,别当"0 字节"
    assert share_required_bytes({}) is None


def test_oversized_share_is_refused_without_even_attempting_restore(monkeypatch) -> None:
    """⚠️ **核心**:分享的 size 之和若已超过剩余空间,**连试都不试** —— 别白挨一次转存。"""
    from app.services import xunlei_transfer as xt

    posted: list[str] = []
    monkeypatch.setattr(xt, "_credentials", lambda settings=None: {
        "access_token": "a", "captcha_token": "c", "device_id": "d", "client_id": "x"})
    monkeypatch.setattr(xt, "_drive_headers", lambda c: {})
    monkeypatch.setattr(xt, "resolve_parent_id", lambda cred=None, **k: "P")
    monkeypatch.setattr(xt, "list_files", lambda *a, **k: [])
    monkeypatch.setattr(xt, "quota_info", lambda cred=None: {
        "usage": 26_454_214_882_191, "limit": 33_092_723_015_680, "ratio": 0.799})
    monkeypatch.setattr(xt.requests, "get", lambda url, **kw: _FakeResp(
        {"share_status": "OK", "pass_code_token": "t",
         "files": [{"id": "F1", "size": 7_563_939_414_460}]}))

    def fake_post(url, **kw):
        posted.append(url)
        return _FakeResp({"restore_task_id": "T1"})

    monkeypatch.setattr(xt.requests, "post", fake_post)

    out = xt.transfer_and_share("https://pan.xunlei.com/s/S?pwd=p")
    assert out["status"] == "failed" and out["code"] == "space_insufficient"
    assert posted == [], "已经确定装不下,不该再去打 restore"
    assert out["required_size"] == 7_563_939_414_460
    assert xt.space_insufficient(out) is True
    # ⚠️ 文案里必须带「空间不足」:上层 `is_space_error` 靠它走"可重试"分支
    assert xt.is_space_error(out["message"]) is True
    assert "6.88 TiB" in out["message"] and "6.04 TiB" in out["message"]


def test_restore_space_error_carries_both_numbers(monkeypatch) -> None:
    """预判漏掉时(文件夹 size 恒 0 ⇒ 下限),转存失败那一步也要把两个数带出来。"""
    from app.services import xunlei_transfer as xt

    monkeypatch.setattr(xt, "_credentials", lambda settings=None: {
        "access_token": "a", "captcha_token": "c", "device_id": "d", "client_id": "x"})
    monkeypatch.setattr(xt, "_drive_headers", lambda c: {})
    monkeypatch.setattr(xt, "resolve_parent_id", lambda cred=None, **k: "P")
    monkeypatch.setattr(xt, "list_files", lambda *a, **k: [])
    monkeypatch.setattr(xt, "quota_info", lambda cred=None: {
        "usage": 26_454_214_882_191, "limit": 33_092_723_015_680, "ratio": 0.799})
    monkeypatch.setattr(xt.requests, "get", lambda url, **kw: _FakeResp(
        {"share_status": "OK", "pass_code_token": "t", "files": [{"id": "F1", "size": 0}]}))
    monkeypatch.setattr(xt.requests, "post", lambda url, **kw: _FakeResp({
        "error": "file_space_not_enough", "error_description": "空间不足",
        "details": [{"type": "DRIVE_SPACE_NOT_ENOUGH",
                     "data": {"required_size": "7563939414460"}}]}))

    out = xt.transfer_and_share("https://pan.xunlei.com/s/S?pwd=p")
    assert out["code"] == "space_insufficient"
    assert "6.88 TiB" in out["message"] and "6.04 TiB" in out["message"]
    assert "还差 0.84 TiB" in out["message"], "缺口要算出来(6.88 - 6.04),这才是用户要的答案"


def test_unknown_space_does_not_block_a_transfer(monkeypatch) -> None:
    """⚠️ **不知道 ≠ 满**:配额探针拿不到时**照常转**,不许拿"不知道"去挡(与 `admit_transfer` 同源)。"""
    from app.services import xunlei_transfer as xt

    monkeypatch.setattr(xt, "_credentials", lambda settings=None: {
        "access_token": "a", "captcha_token": "c", "device_id": "d", "client_id": "x"})
    monkeypatch.setattr(xt, "_drive_headers", lambda c: {})
    monkeypatch.setattr(xt, "resolve_parent_id", lambda cred=None, **k: "P")
    monkeypatch.setattr(xt, "list_files", lambda *a, **k: [])
    monkeypatch.setattr(xt, "quota_info", lambda cred=None: {})      # 探针失效 → 剩余空间未知

    def fake_get(url, **kw):
        # 分享详情 / 转存任务轮询走同一个 GET,要分开(任务必须一次就 progress=100,否则会 sleep)
        if "/tasks/" in url:
            return _FakeResp({"progress": 100,
                              "params": {"trace_file_ids": json.dumps({"F1": "N1"})}})
        return _FakeResp({"share_status": "OK", "pass_code_token": "t",
                          "files": [{"id": "F1", "size": 9_999_999_999_999}]})

    monkeypatch.setattr(xt.requests, "get", fake_get)

    def fake_post(url, **kw):
        if url.endswith("/restore"):
            return _FakeResp({"restore_task_id": "T1"})
        return _FakeResp({"share_url": "https://pan.xunlei.com/s/OUR", "pass_code": "9"})

    monkeypatch.setattr(xt.requests, "post", fake_post)

    out = xt.transfer_and_share("https://pan.xunlei.com/s/S?pwd=p")
    assert out["status"] == "ok", "探针拿不到时必须放行,不能把'不知道'当'满'"


# ------------------------------------------------- 配额探针必须限流(别每条都打)
def test_quota_probe_is_shared_within_its_ttl(monkeypatch) -> None:
    """⚠️ `/drive/v1/about` **要 captcha**、会走自愈重试 —— 打密了既费 captcha 也吃限流。

    配额是**粗判据**(比 0.9 / 算剩余空间),秒级陈旧无害,所以进程内缓存。
    """
    from app.services import xunlei_transfer as xt

    calls = {"n": 0}

    def fake_get(url, **kw):
        calls["n"] += 1
        return _FakeResp({"quota": {"limit": "1000", "usage": "800"}})

    monkeypatch.setattr(xt, "_credentials", lambda settings=None: {"access_token": "a"})
    monkeypatch.setattr(xt, "_drive_headers", lambda c: {})
    monkeypatch.setattr(xt.requests, "get", fake_get)

    xt.reset_quota_cache()
    assert xt.quota_info()["ratio"] == 0.8
    assert xt.quota_info()["ratio"] == 0.8
    assert xt.free_bytes() == 200
    assert calls["n"] == 1, f"TTL 内应共用一次探针,实际打了 {calls['n']} 次"

    xt.reset_quota_cache()                     # 清了才会真打
    xt.quota_info()
    assert calls["n"] == 2


def test_failed_probe_is_cached_too(monkeypatch) -> None:
    """⚠️ **失败也要缓存**:一次 captcha 失效若在同一条链里被反复重撞,那正是把 captcha 打爆的写法。
    代价是恢复慢 ≤ TTL —— 比"20 条资源各撞一次"划算得多。"""
    from app.services import xunlei_transfer as xt

    calls = {"n": 0}

    def fake_get(url, **kw):
        calls["n"] += 1
        raise RuntimeError("网络炸了")

    monkeypatch.setattr(xt, "_credentials", lambda settings=None: {"access_token": "a"})
    monkeypatch.setattr(xt, "_drive_headers", lambda c: {})
    monkeypatch.setattr(xt.requests, "get", fake_get)

    xt.reset_quota_cache()
    assert xt.quota_info() == {} and xt.quota_ratio() is None
    assert xt.quota_info() == {}
    assert calls["n"] == 1, f"失败结果也该缓存,实际打了 {calls['n']} 次"


def test_prejudgment_does_not_probe_quota_per_resource(monkeypatch) -> None:
    """⚠️ **回归守卫**:加了"按剩余空间预判"之后,最容易犯的错就是**每条资源各探一次配额** ——
    `transfer_pending` 开头明明写过"只查一次,整批共用(别每条都打一次配额)"。
    这里跑 3 条转存,断言 `/drive/v1/about` **只被打了一次**。"""
    from app.services import xunlei_transfer as xt

    probe_calls = {"n": 0}

    monkeypatch.setattr(xt, "_credentials", lambda settings=None: {
        "access_token": "a", "captcha_token": "c", "device_id": "d", "client_id": "x"})
    monkeypatch.setattr(xt, "_drive_headers", lambda c: {})
    monkeypatch.setattr(xt, "resolve_parent_id", lambda cred=None, **k: "P")
    monkeypatch.setattr(xt, "list_files", lambda *a, **k: [])

    def fake_get(url, **kw):
        if url.endswith("/drive/v1/about"):
            probe_calls["n"] += 1
            return _FakeResp({"quota": {"limit": str(33_092_723_015_680),
                                        "usage": str(26_454_214_882_191)}})
        if "/tasks/" in url:
            return _FakeResp({"progress": 100,
                              "params": {"trace_file_ids": json.dumps({"F1": "N1"})}})
        return _FakeResp({"share_status": "OK", "pass_code_token": "t",
                          "files": [{"id": "F1", "size": 1024}]})

    def fake_post(url, **kw):
        if url.endswith("/restore"):
            return _FakeResp({"restore_task_id": "T1"})
        return _FakeResp({"share_url": "https://pan.xunlei.com/s/OUR", "pass_code": "9"})

    monkeypatch.setattr(xt.requests, "get", fake_get)
    monkeypatch.setattr(xt.requests, "post", fake_post)

    xt.reset_quota_cache()
    for _ in range(3):
        assert xt.transfer_and_share("https://pan.xunlei.com/s/S?pwd=p")["status"] == "ok"
    assert probe_calls["n"] == 1, f"3 条资源打了 {probe_calls['n']} 次配额探针 —— 又变成每条各打一次了"


def test_fresh_probe_bypasses_the_cache(monkeypatch) -> None:
    """⚠️ **给人按的那个按钮必须拿到新鲜数**:他刚清完盘再点"刷新盘况",
    返回 60 秒前的缓存会被读成"清理没生效" —— 那就是**把缓存伪装成现状**。"""
    from app.services import xunlei_transfer as xt

    calls = {"n": 0}
    usage = {"v": 800}

    def fake_get(url, **kw):
        calls["n"] += 1
        return _FakeResp({"quota": {"limit": "1000", "usage": str(usage["v"])}})

    monkeypatch.setattr(xt, "_credentials", lambda settings=None: {"access_token": "a"})
    monkeypatch.setattr(xt, "_drive_headers", lambda c: {})
    monkeypatch.setattr(xt.requests, "get", fake_get)

    xt.reset_quota_cache()
    assert xt.quota_info()["ratio"] == 0.8
    usage["v"] = 500                        # 盘被清了一些
    assert xt.quota_info()["ratio"] == 0.8, "缓存内应复用(这是「整批共用一次」的基础)"
    assert xt.quota_info(fresh=True)["ratio"] == 0.5, "fresh=True 必须看到新数"
    assert calls["n"] == 2
    assert xt.quota_info()["ratio"] == 0.5, "fresh 的结果要顺手写回缓存"


# ---------------------------------------------------------------- refresh_token 轮换
def test_refresh_writes_back_the_rotated_refresh_token(monkeypatch, session) -> None:
    """★★ **迅雷刷新会轮换 `refresh_token`,必须写回**(2026-10-05 实测踩到)。

    症状:今晚 **00:20 自动刷新成功、00:40 整条迅雷链就开始 `invalid_grant (4126)`**
    —— 因为兑过一次后旧的 refresh_token 即失效,而我们**没把新的存回去**。

    ⚠️ 项目里 `xunlei_captcha` 早就知道这个坑(docstring:"那次兑换会轮换 refresh_token,
    而我们不一定接得住,2026-10-02 踩过"),**但直接调 token 端点这条捷径没处理**。
    """
    import json

    from app.db.models import User
    from app.services import cookie_store
    from app.services import xunlei_transfer as xt

    session.add(User(id=1, username="u", password_hash="x", enabled=True))
    session.commit()
    old = {"access_token": "OLD", "refresh_token": "RT-OLD", "client_id": "web"}
    cookie_store.set_cookie(session, 1, "xunlei", json.dumps(old))

    monkeypatch.setattr(xt.requests, "post", lambda *a, **k: _FakeResp(
        {"access_token": "NEW", "refresh_token": "RT-NEW", "expires_in": 43200}))
    # `_save_credentials` 自己开 session,这里把工厂指到同一个引擎
    monkeypatch.setattr(xt, "_save_credentials",
                        lambda patch: cookie_store.set_cookie(
                            session, 1, "xunlei",
                            json.dumps({**old, **{k: v for k, v in patch.items() if v}})))

    assert xt._refresh_access_token("RT-OLD", "web") == "NEW"

    raw = cookie_store.get_cookie(session, 1, "xunlei")
    got = json.loads(raw)
    assert got["refresh_token"] == "RT-NEW", f"轮换后的 refresh_token 没写回:{got}"
    assert got["access_token"] == "NEW"


def test_refresh_keeps_old_token_when_server_does_not_rotate(monkeypatch) -> None:
    """★ 服务端**未必每次都轮换** —— 回的 refresh_token 与旧的相同时,不该多写一遍
    (但也不能把它写成空)。"""
    from app.services import xunlei_transfer as xt

    saved: list[dict] = []
    monkeypatch.setattr(xt.requests, "post", lambda *a, **k: _FakeResp(
        {"access_token": "NEW2", "expires_in": 43200}))       # 没回 refresh_token
    monkeypatch.setattr(xt, "_save_credentials", lambda patch: saved.append(patch))

    assert xt._refresh_access_token("RT-KEEP", "web") == "NEW2"
    assert saved and "refresh_token" not in saved[0], "没轮换就不该动 refresh_token"

@pytest.fixture()
def session():
    """这个文件原本没有 DB fixture;写回那条链要查库。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db.database import Base
    from app.db import models as _models       # 建表要靠它注册模型

    assert _models is not None
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    yield db
    db.close()
