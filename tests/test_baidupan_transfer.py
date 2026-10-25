"""百度网盘转存的**错误可读性**单测(2026-10-03 补)。

背景:贴吧/知乎发现的影视盘链大批回 `errno=-6`,而光看数字**分不清是"分享已失效"(终态)
还是"转存被限制"(等一会就好)** —— 这两者对下游意义相反(前者的行该标 skipped、
后者该留 failed 下轮重来)。所以必须把百度的 `show_msg` 一起带进错误串。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest  # noqa: E402

from app.services.baidupan_transfer import (  # noqa: E402
    BaiduPanAuthError,
    BaiduPanClient,
    _looks_like_auth_issue,
    _transfer_error,
)


class _FakeSession:
    """假会话:记下请求过的 URL,固定回一份 JSON。"""

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.urls: list[str] = []

    def get(self, url, **kw):
        self.urls.append(url)
        payload = self.payload
        return type("R", (), {"json": lambda self: payload, "status_code": 200})()


def _client_with(payload: dict) -> tuple[BaiduPanClient, _FakeSession]:
    c = BaiduPanClient("BDUSS=x")
    sess = _FakeSession(payload)
    c._browser = lambda: sess          # type: ignore[method-assign]
    return c, sess


class _FakeText:
    """假会话:回一段 HTML(`_share_meta` 只读 `.text`)。"""

    def __init__(self, text: str) -> None:
        self.text = text

    def get(self, url, **kw):
        return self


def test_share_meta_parses_nested_file_list_without_truncating() -> None:
    """★ 2026-10-21:旧正则 `\\[[^\\]]*\\]` 遇到嵌套数组/字符串里的 `]` 就提前截断,
    `json.loads` 抛 `Unterminated string starting at: …` —— 积压里 12 条贴吧链
    **每轮都失败**,却是我们自己的解析切歪了,不是链坏了。这里钉住"嵌套也能整段解析"。
    """
    html = (
        '<script>var yunData = {"share_uk":"123","shareid":"456","bdstoken":"tok",'
        '"file_list":[{"fs_id":1,"server_filename":"a]b.mkv","size":10,'
        '"thumbs":[{"url":"https://x/1"}]}]};</script>'
    )
    c = BaiduPanClient("BDUSS=x")
    c._browser = lambda: _FakeText(html)      # type: ignore[method-assign]
    uk, shareid, tok, files = c._share_meta("surl")
    assert (uk, shareid, tok) == ("123", "456", "tok")
    assert len(files) == 1 and files[0]["server_filename"] == "a]b.mkv"


def test_keepalive_probes_the_web_session_not_login_status() -> None:
    """⚠️ **必须探网页会话**(`/api/quota`),不能探 `/api/loginStatus`(2026-10-04 实测)。

    一份**只有 `BDUSS`、没有 `STOKEN`** 的"半登录" Cookie,`loginStatus` 照样回 `errno:0` ——
    体检说"正常",而转存和分享页解析全挂。**这就是假成功,还让 Cookie 失效的告警永远等不到。**
    所以这条测试钉的是"**打的是哪个端点**":谁把它改回 `loginStatus`,这里就红。
    """
    c, sess = _client_with({"errno": 0})
    assert c.keepalive() is True
    assert sess.urls and "/api/quota" in sess.urls[0], f"探错端点了:{sess.urls}"
    assert not any("loginStatus" in u for u in sess.urls)


def test_keepalive_raises_actionable_message_on_half_login() -> None:
    """半登录(只要 BDUSS 没有 STOKEN)必须抛,且文案要**说清该去哪、缺什么**。"""
    c, _ = _client_with({"errno": -6, "errmsg": "用户未登录"})
    with pytest.raises(BaiduPanAuthError) as ei:
        c.keepalive()
    msg = str(ei.value)
    assert "STOKEN" in msg and "pan.baidu.com" in msg, f"文案不可操作:{msg}"


def test_auth_expiry_is_recognized_from_show_msg() -> None:
    """⚠️ **百度把"登录态过期"也塞在 `errno=-6` 里,只有 `show_msg` 认得出**(2026-10-04 实测)。

    实测原文就长这样:`转存失败(errno=-6 账户已过期，重新登陆)`。
    如果只按 errno 归成"普通失败",下游就只记一条泛泛的 `failed` ——
    **一批本来能搬的资源全卡着,却没人知道要去重粘 Cookie**(那天一轮 15 条里 10 条是这个)。
    """
    for msg in ("账户已过期，重新登陆", "请先登录", "登录失效", "未登录"):
        assert _looks_like_auth_issue(msg) is True, msg


def test_dead_link_wording_is_not_mistaken_for_auth() -> None:
    """反向:死链的措辞不能被当成登录态问题(否则会去推一条错误的告警)。"""
    for msg in ("分享文件已被删除", "分享不存在", "", "转存过于频繁"):
        assert _looks_like_auth_issue(msg) is False, msg


def test_show_msg_is_included() -> None:
    """**核心**:不带 show_msg 时人看不出到底怎么了。"""
    msg = _transfer_error(-6, "分享文件已被删除")
    assert "分享文件已被删除" in msg
    assert "-6" in msg


def test_known_rate_limit_errno_gets_its_own_wording() -> None:
    """105 / -70 是已知的"转存被限制" —— 要能一眼看出"该等",而不是"该死"。"""
    for code in (105, -70):
        msg = _transfer_error(code, "转存过于频繁")
        assert "被限制" in msg and "稍后重试" in msg
        assert "转存过于频繁" in msg


def test_unknown_errno_still_carries_show_msg() -> None:
    msg = _transfer_error(-9, "文件不存在")
    assert "转存失败" in msg and "文件不存在" in msg


def test_missing_show_msg_does_not_crash_or_leave_dangling_space() -> None:
    """百度偶尔不回 show_msg —— 不能因此报错,也不该留个悬空空格(`(errno=-6 )`)。"""
    for sm in ("", "   ", None):
        msg = _transfer_error(-6, sm or "")
        assert msg == "转存失败(errno=-6)", msg


def test_show_msg_is_truncated() -> None:
    """百度偶尔回一大段 HTML/堆栈 —— 别让它污染运行记录。"""
    assert len(_transfer_error(-6, "x" * 500)) < 120


# ---------------------------------------------------------------------------
# ★★★ 2026-10-13:`/share/verify` 的 `errno=105` 到底是"分享已死"还是"等一会"?
#
# 实测证据:库里 9 条 `failed` 全是 `提取码验证失败(errno=105)`,逐条去 GET 它们的
# 分享页 ⇒ **恒回 HTTP 404** ⇒ 链接已不存在,重试一万次也不会好。
# 而旧措辞下游 `_is_dead_link` 认不出 ⇒ 一律记 `failed` ⇒ **每天重试同一批死链**
# (日志里 294 条「盘链转存失败」、积压恒卡 17 条不退)。
# ---------------------------------------------------------------------------


class _VerifySession:
    """`/share/verify` 回指定 errno;分享页回指定 HTTP 码/正文。"""

    def __init__(self, errno: int, page_status: int = 200, page_body: str = "",
                 page_raises: bool = False) -> None:
        self.errno = errno
        self.page_status = page_status
        self.page_body = page_body
        self.page_raises = page_raises
        self.posts: list[str] = []
        self.gets: list[str] = []

    def post(self, url, **kw):
        self.posts.append(url)
        # ⚠️ 先算成局部变量再塞进替身:写成 `lambda self: self.errno` 会被**替身自己**
        #    的 `self` 遮蔽(`r.json()` 把实例当第一个参数传进来)⇒ 报 "R 没有 errno"。
        payload = {"errno": self.errno, "err_msg": ""}
        return type("R", (), {"json": lambda self: payload})()

    def get(self, url, **kw):
        self.gets.append(url)
        if self.page_raises:
            raise RuntimeError("连接超时")
        return type("R", (), {"status_code": self.page_status, "text": self.page_body})()


def _vc(**kw) -> tuple[BaiduPanClient, _VerifySession]:
    c = BaiduPanClient("BDUSS=x")
    sess = _VerifySession(**kw)
    c._browser = lambda: sess          # type: ignore[method-assign]
    return c, sess


def test_verify_105_加上分享页404_记终态() -> None:
    """★ 死链必须判**终态** —— 否则每天重试一遍,`failed` 永远不退。"""
    import app.services.pan_discovery as pd

    c, sess = _vc(errno=105, page_status=404)
    with pytest.raises(Exception) as ei:
        c._verify("BjXDjLdj3WB1EZdAdByJXA", "abcd")

    msg = str(ei.value)
    assert "分享地址已失效" in msg, f"措辞必须让下游 `_is_dead_link` 认得出死链:{msg}"
    assert pd._is_dead_link(msg) and pd._fail(msg)["status"] == "skipped", (
        f"认出来才是终态;否则又回到「每天重试同一批死链」:{msg}")
    assert sess.gets, "判终态前**必须真去看一眼分享页** —— 不能凭 errno 猜"
    assert "/s/1BjXDjLdj3WB1EZdAdByJXA" in sess.gets[0], sess.gets


def test_verify_105_但分享页还活着_继续重试() -> None:
    """⚠️ 反向守卫:**没证据就不许判终态**。

    把一条其实能搬的链永久钉死,是本仓 2026-10-02 踩过的坑 ——
    `errno=105` 单看数字分不清"链接死了"和"这一下没验过",所以必须由分享页拍板。
    """
    import app.services.pan_discovery as pd

    c, _ = _vc(errno=105, page_status=200, page_body="<html>正常分享页</html>")
    with pytest.raises(Exception) as ei:
        c._verify("Babc", "abcd")

    msg = str(ei.value)
    assert pd._fail(msg)["status"] == "failed", f"页面还在 ⇒ 可重试:{msg}"


def test_verify_探分享页失败_也不许判终态() -> None:
    """探测本身失败(超时/网络抖动)⇒ 没有证据 ⇒ 照旧留可重试。"""
    import app.services.pan_discovery as pd

    c, _ = _vc(errno=105, page_raises=True)
    with pytest.raises(Exception) as ei:
        c._verify("Babc", "abcd")
    assert pd._fail(str(ei.value))["status"] == "failed"


def test_verify_页面明写已失效_也算死链() -> None:
    """有些死链页面回 200 但正文写着"分享的文件已经被取消" —— 同样算证据。"""
    import app.services.pan_discovery as pd

    c, _ = _vc(errno=105, page_status=200, page_body="<p>分享的文件已经被取消</p>")
    with pytest.raises(Exception) as ei:
        c._verify("Babc", "abcd")
    assert pd._fail(str(ei.value))["status"] == "skipped"


def test_无密码分享不验提取码也不探页面() -> None:
    """没有提取码 ⇒ 本就没有"验码"这一步,`_verify` 直接返回,别白发请求。"""
    c, sess = _vc(errno=105, page_status=404)
    c._verify("Babc", "")
    assert sess.posts == [] and sess.gets == []


def test_其它errno保持原措辞不误判死链() -> None:
    """`errno=-9/-6` 等不是 105 的,**不许**被这次改动顺手判成死链。"""
    import app.services.pan_discovery as pd

    c, _ = _vc(errno=-9, page_status=404)
    with pytest.raises(Exception) as ei:
        c._verify("Babc", "abcd")
    msg = str(ei.value)
    assert "提取码验证失败(errno=-9)" == msg, msg
    assert pd._fail(msg)["status"] == "failed"


class _PostSession:
    """只做 `post` 的假会话:按队列依次回载荷(只剩一个时一直回它)。"""

    def __init__(self, payloads: list[dict]) -> None:
        self.payloads = list(payloads)
        self.posts: list[tuple[str, dict]] = []

    def post(self, url, **kw):
        self.posts.append((url, kw.get("data") or {}))
        p = self.payloads.pop(0) if len(self.payloads) > 1 else self.payloads[0]
        return type("R", (), {"json": lambda self: p, "status_code": 200})()


class Test自动建目录:
    """★★ 2026-10-17:目标目录被删 ⇒ 58 条候选每轮 `errno=2 转存路径不存在`,**断了 5 天**
    才被体检顺出来(积压从 67 涨到 139)。根因是**代码没有建目录的能力** ⇒ 只能等人。

    现在:**撞到「路径不存在」就建一次再重试一回**(happy path 一次额外请求都不多发)。
    接口是**实测**出来的(2026-10-17 可逆探测:建 → `list_dir` 立刻可见 → `delete_paths` 删掉)。
    """

    def _c(self, payloads, monkeypatch):
        c = BaiduPanClient("BDUSS=x")
        sess = _PostSession(payloads)
        c._browser = lambda: sess                       # type: ignore[method-assign]
        monkeypatch.setattr(c, "get_bdstoken", lambda: "TOK")
        return c, sess

    def test_建成功(self, monkeypatch) -> None:
        c, sess = self._c([{"errno": 0, "fs_id": 1}], monkeypatch)
        assert c.ensure_dir("/redian百度转存") is None, "不返回值:建没建百度不告诉我们"
        url, data = sess.posts[0]
        assert "/api/create" in url and data.get("path") == "/redian百度转存"
        assert data.get("isdir") == "1", "必须显式说这是目录"

    def test_已存在算成功不许抛(self, monkeypatch) -> None:
        """并发/两台实例同时建是正常情况 ⇒ 撞名不是错误。"""
        c, _ = self._c([{"errno": -8, "show_msg": "文件已存在"}], monkeypatch)
        assert c.ensure_dir("/redian百度转存") is None

    def test_登录态失效要单独抛(self, monkeypatch) -> None:
        """⚠️ 混进"建目录失败"就会把人指向错的修法(该做的是重粘 Cookie)。"""
        c, _ = self._c([{"errno": -6, "show_msg": "用户未登录"}], monkeypatch)
        with pytest.raises(BaiduPanAuthError):
            c.ensure_dir("/redian百度转存")

    def test_别的错误要说清_errno(self, monkeypatch) -> None:
        c, _ = self._c([{"errno": -7, "show_msg": "空间不足"}], monkeypatch)
        with pytest.raises(Exception) as ei:
            c.ensure_dir("/redian百度转存")
        assert "errno=-7" in str(ei.value) and "空间不足" in str(ei.value)

    def _xfer(self, c, monkeypatch, ensure_raises: bool = False):
        monkeypatch.setattr(c, "_verify", lambda *a, **k: None)
        monkeypatch.setattr(c, "_share_meta",
                            lambda *a, **k: ("uk", "sid", "tok",
                                             [{"fs_id": 1, "server_filename": "a.txt"}]))
        seen: list[str] = []

        def _ens(path, **k):
            seen.append(path)
            if ensure_raises:
                raise RuntimeError("建目录被拒(权限)")

        monkeypatch.setattr(c, "ensure_dir", _ens)
        return seen

    def test_路径不存在会建一次再重试(self, monkeypatch) -> None:
        c, sess = self._c([{"errno": 2, "show_msg": "转存路径不存在"}], monkeypatch)
        seen = self._xfer(c, monkeypatch)
        with pytest.raises(Exception) as ei:
            c.transfer_and_share("https://pan.baidu.com/s/1abc")
        assert seen == ["/redian百度转存"], "没去建目录"
        assert len(sess.posts) == 2, "没重试那一回"
        assert "转存路径不存在" in str(ei.value), "两次都失败就该如实报原错误"

    def test_建不出来要如实报_不许伪装成链坏了(self, monkeypatch) -> None:
        """★ 建不出来(权限/只读)时,错误串必须**指向目录**,不能变成"这条链转存失败" ——
        否则这 58 条本来能搬的链会被记成失败/死链,而人还在查分享本身。"""
        c, sess = self._c([{"errno": 2, "show_msg": "转存路径不存在"}], monkeypatch)
        seen = self._xfer(c, monkeypatch, ensure_raises=True)
        with pytest.raises(Exception) as ei:
            c.transfer_and_share("https://pan.baidu.com/s/1abc")
        assert seen == ["/redian百度转存"]
        assert "建不出来" in str(ei.value) and "/redian百度转存" in str(ei.value), ei.value
        assert len(sess.posts) == 1, "建都建不出来,别再打一次(那是白打)"

    def test_对已存在的目录百度也回_errno_0(self, monkeypatch) -> None:
        """★★ **真机行为(2026-10-17 实测)**:对**已存在**的目录,`/api/create` 回 `errno:0`,
        既不是 `-8` 也不带"已存在" ⇒ **调用方不许拿返回值判断"原来在不在"**
        (我第一版就返回布尔值,还被自己写的替身"验证"通过 —— 那是个**假判据**,
        真机一调就露:它回 True,而目录根本没被新建)。"""
        c, _ = self._c([{"errno": 0, "fs_id": 653594014991471,
                         "path": "/redian百度转存", "isdir": 1}], monkeypatch)
        assert c.ensure_dir("/redian百度转存") is None

    def test_别的_errno_2_不许触发自愈(self, monkeypatch) -> None:
        """⚠️ 本文件头注有实测:`opera=copy` 恒回 `errno=2` —— 那是**这条路由不可用**,
        不是目录缺失。盲目重试只会多打一次。"""
        c, sess = self._c([{"errno": 2, "show_msg": "文件已存在"}], monkeypatch)
        seen = self._xfer(c, monkeypatch)
        with pytest.raises(Exception):
            c.transfer_and_share("https://pan.baidu.com/s/1abc")
        assert seen == [], "show_msg 不是「路径不存在」就不许去建目录"
        assert len(sess.posts) == 1


# ---------------------------------------------------------------------------
# ★★★ 2026-10-25:`surl` 必须**只取路径段** —— 链接自带 `?pwd=` 时不许把查询串一起塞进去。
#
# 受控 A/B(同一条链、同一个 Cookie、相隔 1 秒,真机):
#     surl=`Ftzp5KvW7tYegpsm48Qu_w?pwd=0000` → `/share/verify` 回 **errno=105**
#     surl=`Ftzp5KvW7tYegpsm48Qu_w`            → 回 **errno=0**(验码通过、能列出文件)
# ⇒ 生产积压里那 35 条 `提取码验证失败(errno=105)` **全是这个切法造成的**:
#   `share_url.split("/s/1")[1]` 把 `?pwd=0000` 一起带进去,每轮都死在验码;
#   而分享页本身是活的 ⇒ `_share_page_gone` 正确地判 False ⇒ **永远判不了终态**,
#   于是每天重试、每天同样失败。
# ---------------------------------------------------------------------------


class TestSurlShape:
    def test_只取路径段(self) -> None:
        from app.services.baidupan_transfer import surl_of

        assert surl_of("https://pan.baidu.com/s/1Ftzp5KvW7tYegpsm48Qu_w?pwd=0000") == (
            "Ftzp5KvW7tYegpsm48Qu_w")
        assert surl_of("https://pan.baidu.com/s/1abc?pwd=1234&x=1") == "abc"
        assert surl_of("https://pan.baidu.com/s/1abc#pwd=1234") == "abc"
        assert surl_of("https://pan.baidu.com/s/1abc") == "abc"

    def test_转存时验码与解析都用裸_surl(self, monkeypatch) -> None:
        c = BaiduPanClient("BDUSS=x")
        seen: dict[str, str] = {}
        monkeypatch.setattr(c, "_verify", lambda surl, pwd: seen.update(verify=surl))

        def _stop(surl):
            seen["meta"] = surl
            raise RuntimeError("停在这里,后面不需要")

        monkeypatch.setattr(c, "_share_meta", _stop)
        with pytest.raises(RuntimeError):
            c.transfer_and_share(
                "https://pan.baidu.com/s/1Ftzp5KvW7tYegpsm48Qu_w?pwd=0000",
                password="0000")
        #: 两个步骤**都**得是裸 surl:`/share/init?surl=` 与 `/s/1{surl}` 同样不吃查询串
        assert seen == {"verify": "Ftzp5KvW7tYegpsm48Qu_w",
                        "meta": "Ftzp5KvW7tYegpsm48Qu_w"}, seen

    def test_认不出的形状要明确报错而不是拿整串去打接口(self, monkeypatch) -> None:
        c = BaiduPanClient("BDUSS=x")
        monkeypatch.setattr(c, "_verify", lambda *a, **k: pytest.fail("不该走到验码"))
        with pytest.raises(Exception) as ei:
            c.transfer_and_share("https://example.com/not-baidu")
        assert "不是有效的百度分享链接" in str(ei.value), ei.value

