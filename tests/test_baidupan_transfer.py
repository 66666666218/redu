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
