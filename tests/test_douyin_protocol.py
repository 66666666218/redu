"""抖音纯协议源的**离线**测试(2026-10-07)。

⚠️ 这里**一个请求都不发**。它守的是"我们自己写错"的那一类错(字段映射、翻页契约、
错误归类),**不能**证明合同本身对不对 —— 那只能靠实跑,见
`doc/抖音纯协议-链路拆解.md` 与 `tools/douyin_probe.py`。

**重点守三条**(都是本仓栽过的坑):
1. 被风控的响应**必须抛错**,不许表现成「没搜到」;
2. 拿不到的指标**不许填 0**(0 = 没人看,缺 = 平台没给,两回事);
3. 翻页要用**上一屏的 logid** 当 `search_id`(与小红书"共用一个 id"**正好相反**)。
"""
import pytest

from app.services import douyin_protocol_source as dp


# ---------------------------------------------------------------------------
# 合成的接口响应(照抖音 web 搜索的已知形状手写;**不是**抓包留档)
# ---------------------------------------------------------------------------


def _resp(items=None, logid="20261007120000A1B2C3", **extra):
    d = {"status_code": 0, "extra": {"logid": logid}}
    if items is not None:
        d["data"] = items
    d.update(extra)
    return d


def _video(aweme_id="7380308675841297704", desc="野鹅敢死队 经典影片 国语配音",
           nickname="某某影视", sec_uid="MS4wLjABAAAAxxxx", create_time=1759800000,
           statistics=None):
    return {"type": 1, "aweme_info": {
        "aweme_id": aweme_id, "desc": desc, "create_time": create_time,
        "author": {"nickname": nickname, "sec_uid": sec_uid},
        "statistics": statistics if statistics is not None else
        {"digg_count": 1200, "comment_count": 33, "share_count": 88,
         "play_count": 45000, "collect_count": 7},
        "share_url": "https://www.iesdouyin.com/share/video/%s/" % aweme_id,
    }}


# ---------------------------------------------------------------------------
# 请求构造
# ---------------------------------------------------------------------------


def test_设备指纹参数齐全():
    """少任何一个"埋点"字段都可能让风控把请求当成机器人 —— 逐个点名。"""
    p = dp.build_params("网盘资源", ms_token="t", webid="w")
    for key in ("device_platform", "aid", "channel", "version_code", "pc_client_type",
                "cookie_enabled", "browser_language", "browser_platform", "browser_name",
                "browser_version", "engine_name", "engine_version", "os_name", "os_version",
                "cpu_core_num", "device_memory", "platform", "screen_width",
                "screen_height", "effective_type", "round_trip_time", "webid", "msToken"):
        assert key in p, f"少了 {key}"
    assert p["search_channel"] == "aweme_general"
    assert p["count"] == str(dp.PAGE_SIZE)


def test_首屏_search_id_为空串而非缺字段():
    """缺字段与空串在 `search_id` 上**不是一回事**:缺了可能被当成参数不合法。"""
    p = dp.build_params("x", ms_token="t", webid="w")
    assert p["search_id"] == ""


def test_默认不加_a_bogus():
    """**这是有意的默认值**:真在跑的 MediaCrawler 对这条接口就不加(见模块 docstring)。

    断言的是"默认值",不是"抖音不需要它" —— 后者要实跑才敢说。
    """
    url = dp.build_url(dp.build_params("x", ms_token="t", webid="w"))
    assert "a_bogus" not in url
    assert url.startswith(dp.API_BASE + dp.URI_SEARCH + "?")


def test_能按开关加_a_bogus(monkeypatch):
    """开关是真的能生效 —— 防的是"给了参数但没人用"(本仓吃过 `getattr` 拼错名的亏)。"""
    from app.services.douyin_sign import ABogus

    def fake_generate(self, params, body=""):
        return (f"{params}&a_bogus=FAKE", "FAKE", "", "")

    monkeypatch.setattr(ABogus, "generate_abogus", fake_generate)
    url = dp.build_url(dp.build_params("x", ms_token="t", webid="w"), use_abogus=True)
    assert "a_bogus=FAKE" in url


def test_缺少_uifid_时不给_uifid_头():
    """没有就别硬塞空值 —— 空 `uifid` 与不传是两个不同的请求。"""
    h = dp.build_headers("a=1", {"a": "1"}, "网盘资源")
    assert "uifid" not in h
    h2 = dp.build_headers("UIFID=x", {"UIFID": "x"}, "网盘资源")
    assert h2["uifid"] == "x"


def test_uifid_认_UIFID_TEMP_这个别名():
    """没登录时网页给的是 `UIFID_TEMP`;少认一个名字,那条路就永远 403。"""
    assert dp._uifid({"UIFID_TEMP": "tmp1"}) == "tmp1"
    assert dp._uifid({"UIFID": "real"}) == "real"
    assert dp._uifid({"UIFID": "", "UIFID_TEMP": "tmp2"}) == "tmp2"


def test_argus_头必须在():
    """缺它 → 403 `Blocked by ArgusSecurityPlugin ... Signature Not Found`。"""
    assert dp.build_headers("", {}, "x")["x-tt-argus"] == "1"


def test_referer_带搜索词():
    h = dp.build_headers("", {}, "网盘资源")
    assert "douyin.com/search/" in h["Referer"]


def test_webid_是_19_位数字():
    for _ in range(20):
        w = dp._webid()
        assert len(w) == 19 and w.isdigit()


def test_msToken_有真的就不用假的():
    """cookie 里带 `msToken` 时必须用它 —— 造假的会与登录态对不上账。"""
    assert dp._ms_token({"msToken": "REAL_TOKEN"}) == "REAL_TOKEN"
    fake = dp._ms_token({})
    assert fake.endswith("==") and len(fake) == 184


# ---------------------------------------------------------------------------
# 响应解析
# ---------------------------------------------------------------------------


def test_只取视频条_跳过用户与直播():
    """搜索结果里混着 type=4 的用户卡、没有 aweme_info 的条目 —— 跳过是正常的,不是错误。"""
    rows, _ = dp._parse(_resp([_video(), {"type": 4, "user_info": {"nickname": "x"}},
                               {"type": 1}]), "网盘资源")
    assert len(rows) == 1
    r = rows[0]
    assert r["aweme_id"] == "7380308675841297704"
    assert r["name"] == "某某影视"                 # ★ 真名(教学版这里是 `某***`)
    assert r["url"] == "https://www.douyin.com/video/7380308675841297704"
    assert r["publish_at"] == 1759800000
    assert r["keyword"] == "网盘资源"


def test_链接形状能被_aweme_id_正则认出():
    """**跨模块契约**:`douyin_leads._aweme_id` 认 `/(video|note)/<id>`,
    这里换了链接形状它就会静默地把去重键全变空 —— 那是会丢线索的。"""
    from app.services.douyin_leads import _aweme_id

    rows, _ = dp._parse(_resp([_video()]), "x")
    assert _aweme_id(rows[0]["url"]) == "7380308675841297704"


def test_拿不到的指标不填_0():
    """**本仓的核心纪律**:平台没给的字段不能伪装成 0(0 会被读成"没人看")。"""
    rows, _ = dp._parse(_resp([_video(statistics={"digg_count": 5})]), "x")
    assert rows[0]["metrics"]["liked_count"] == 5
    assert "share_count" not in rows[0]["metrics"]
    # 但兼容字段 `share_count` 仍要给下游一个数(它的列本来就这么定义)
    assert rows[0]["share_count"] == 0


def test_统计字段缺失时_metrics_为空而非全零():
    rows, _ = dp._parse(_resp([_video(statistics={})]), "x")
    assert rows[0]["metrics"] == {}


def test_没有_desc_也不该整条丢掉():
    """标题空的帖子照样是线索(口令可能在别处)—— 丢它等于丢线索。"""
    rows, _ = dp._parse(_resp([_video(desc="")]), "x")
    assert len(rows) == 1 and rows[0]["snippet"] == ""


def test_没有_aweme_id_的条目丢掉():
    bad = _video()
    bad["aweme_info"]["aweme_id"] = ""
    rows, _ = dp._parse(_resp([bad]), "x")
    assert rows == []


def test_解析返回_logid_供翻页():
    _, logid = dp._parse(_resp([_video()], logid="LOGID-XYZ"), "x")
    assert logid == "LOGID-XYZ"


# ---------------------------------------------------------------------------
# 失败必须响亮(不许表现成"没搜到")
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, status=200, text="", payload=None):
        self.status_code = status
        self.text = text if payload is None else __import__("json").dumps(payload)
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def _run(monkeypatch, resp):
    import requests

    monkeypatch.setattr(requests, "get", lambda *a, **k: resp)
    return dp.search(["网盘资源"])


def test_响应缺_data_字段要抛错而不是返回空列表(monkeypatch):
    """★ 最重要的一条防假阴性。抖音风控时的典型形状就是**没有 `data` 键**;
    若这里返回 `[]`,上游会记成"本轮没线索",**风控就被读成了没生意**。"""
    with pytest.raises(dp.DouyinProtocolError) as e:
        _run(monkeypatch, _FakeResp(payload={"status_code": 0}))
    assert e.value.kind == "restricted" and e.value.needs_human


#: ★ **实跑抓回来的原文**(2026-10-07 匿名探测,`tools/douyin_probe.py` 的输出)
#: —— 用真响应当夹具,比手编一个"我以为它会这么回"的强得多。
REAL_ANON_2483 = {"log_pb": {"impr_id": "20261007231728EC856B57D52BF4ED43D4"},
                  "status_code": 2483, "status_msg": "请先登录，再继续搜索吧"}


def test_匿名被要求登录要归类为_need_login_而不是风控(monkeypatch):
    """★ **诊断口径**的守卫:2483「请先登录」的修法是**补登录态**;
    若把它归成 `restricted`(风控),接手的人就会去"等解封"或"改签名" —— **修错方向**。

    实测背景:这条接口**匿名搜不了**,而且 `a_bogus` 带不带响应**一字不差**
    (登录闸门在签名之前就拦下了)。
    """
    with pytest.raises(dp.DouyinProtocolError) as e:
        _run(monkeypatch, _FakeResp(payload=REAL_ANON_2483))
    assert e.value.kind == "need_login"
    assert e.value.needs_human
    assert "登录" in str(e.value)


def test_没有_data_又不是登录提示才算风控(monkeypatch):
    """**两条分支不能合并**:登录提示有明确修法,别的"没有 data"没有。"""
    with pytest.raises(dp.DouyinProtocolError) as e:
        _run(monkeypatch, _FakeResp(payload={"status_code": 0, "status_msg": "whatever"}))
    assert e.value.kind == "restricted"


def test_data_为空的列表是合法的空结果(monkeypatch):
    """`data: []` 与"没有 data 键"**必须分开**:前者是真没搜到,后者是被挡了。"""
    assert _run(monkeypatch, _FakeResp(payload=_resp([]))) == []


def test_403_argus_单独归类且需人工(monkeypatch):
    resp = _FakeResp(status=403, text="Blocked by ArgusSecurityPlugin Uifid Not Found")
    with pytest.raises(dp.DouyinProtocolError) as e:
        _run(monkeypatch, resp)
    assert e.value.kind == "argus" and e.value.needs_human


def test_403_没有_argus_字样就按普通_http_错(monkeypatch):
    with pytest.raises(dp.DouyinProtocolError) as e:
        _run(monkeypatch, _FakeResp(status=403, text="Forbidden"))
    assert e.value.kind == "api" and not e.value.needs_human


def test_响应_body_为_blocked_要抛错(monkeypatch):
    with pytest.raises(dp.DouyinProtocolError) as e:
        _run(monkeypatch, _FakeResp(text="blocked"))
    assert e.value.kind == "blocked" and e.value.needs_human


def test_非_JSON_要抛错(monkeypatch):
    with pytest.raises(dp.DouyinProtocolError) as e:
        _run(monkeypatch, _FakeResp(text="<html>"))
    assert e.value.kind == "api"


def test_status_code_非零要抛错(monkeypatch):
    with pytest.raises(dp.DouyinProtocolError) as e:
        _run(monkeypatch, _FakeResp(payload={"status_code": 8, "status_msg": "风控",
                                             "data": []}))
    assert e.value.kind == "api"


def test_网络异常归类为_network(monkeypatch):
    import requests

    def boom(*a, **k):
        raise OSError("connection reset")

    monkeypatch.setattr(requests, "get", boom)
    with pytest.raises(dp.DouyinProtocolError) as e:
        dp.search(["x"])
    assert e.value.kind == "network"


# ---------------------------------------------------------------------------
# 翻页契约
# ---------------------------------------------------------------------------


def test_翻页把上一屏的_logid_当_search_id(monkeypatch):
    """★ 与小红书**恰好相反**:小红书是"多页共用一个 search_id",
    抖音是"下一页用**上一屏响应的 logid**"。搞反了会每页拿到同一批(静默重复)。"""
    import requests

    seen: list[dict] = []

    def fake_get(url, headers=None, timeout=None):
        from urllib.parse import parse_qs, urlparse

        # ⚠️ `keep_blank_values=True` 不能省:`parse_qs` 默认**丢掉空值参数**,
        # 而 `search_id=` 恰好就是空串 —— 省了它,首屏那个断言会以 KeyError 的形式
        # 变成"测试自己写错了",而不是在**报我们想要的那件事**。
        q = parse_qs(urlparse(url).query, keep_blank_values=True)
        seen.append(q)
        page = len(seen)
        return _FakeResp(payload=_resp([_video(aweme_id=f"1000{page}")],
                                       logid=f"LOGID-{page}"))

    monkeypatch.setattr(requests, "get", fake_get)
    rows = dp.search(["网盘资源"], pages=3)
    assert [q["search_id"][0] for q in seen] == ["", "LOGID-1", "LOGID-2"]
    assert len(rows) == 3
    assert [r["aweme_id"] for r in rows] == ["10001", "10002", "10003"]


def test_某页空就停_不再继续翻(monkeypatch):
    """空页 = 到头了。继续翻只会白烧请求(而且多一次就多一次风控输入)。"""
    import requests

    calls: list[str] = []

    def fake_get(url, headers=None, timeout=None):
        calls.append(url)
        return _FakeResp(payload=_resp([] if len(calls) > 1 else [_video()]))

    monkeypatch.setattr(requests, "get", fake_get)
    dp.search(["x"], pages=5)
    assert len(calls) == 2


def test_没有关键词就不发请求(monkeypatch):
    import requests

    def boom(*a, **k):
        raise AssertionError("不该发请求")

    monkeypatch.setattr(requests, "get", boom)
    assert dp.search(["", "  ", None]) == []
