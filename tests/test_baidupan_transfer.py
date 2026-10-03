"""百度网盘转存的**错误可读性**单测(2026-10-03 补)。

背景:贴吧/知乎发现的影视盘链大批回 `errno=-6`,而光看数字**分不清是"分享已失效"(终态)
还是"转存被限制"(等一会就好)** —— 这两者对下游意义相反(前者的行该标 skipped、
后者该留 failed 下轮重来)。所以必须把百度的 `show_msg` 一起带进错误串。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

from app.services.baidupan_transfer import _transfer_error  # noqa: E402


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
