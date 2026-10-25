"""日志编码回归测试(2026-10-26)。

## 守的是什么

Windows 上服务由 `pythonw` 启动、stdout 重定向到文件时,流编码是 **GBK**。
任何带 emoji / `⇒` 的日志都会在 `stream.write()` 抛 `UnicodeEncodeError`,
logging 把「--- Logging error ---」+ 30 行调用栈写进 stderr ⇒ **那句日志消失**。

实测:67MB 的 `app.log` 里有 **2056 个 traceback**,大多是这么来的 ——
**我们写得越"人话"(带箭头/emoji),越容易被丢掉**。
"""
import io
import logging

import pytest

from app.utils.logger import _utf8_stream


def test_把_gbk_流切成_utf8(tmp_path):
    p = tmp_path / "log.txt"
    raw = open(p, "wb")
    s = io.TextIOWrapper(raw, encoding="gbk")
    with pytest.raises(UnicodeEncodeError):        # 切之前:写 emoji 会炸(复现线上现象)
        s.write("⇒🔥")
    out = _utf8_stream(s)
    out.write("⇒🔥\n")
    out.flush()
    assert p.read_text(encoding="utf-8").strip() == "⇒🔥"


def test_带emoji的日志真的落盘(tmp_path):
    """端到端:经 handler 写一行带 emoji 的日志,**读回来还在**(不是被吞掉)。"""
    p = tmp_path / "app.log"
    raw = open(p, "wb")
    stream = _utf8_stream(io.TextIOWrapper(raw, encoding="gbk"))
    handler = logging.StreamHandler(stream)
    lg = logging.getLogger("test_emoji_logger")
    lg.handlers = [handler]
    lg.propagate = False
    lg.setLevel(logging.INFO)
    lg.info("转存成功 ⇒ 🔥 已发")
    handler.flush()
    stream.flush()
    assert "🔥" in p.read_text(encoding="utf-8")


def test_切不动也不许抛():
    """没有 `reconfigure`、也没有 `buffer` 的怪流 ⇒ 原样返回,别让日志系统起不来。"""
    class _Weird:
        def write(self, s):
            pass

        def flush(self):
            pass

    w = _Weird()
    assert _utf8_stream(w) is w
