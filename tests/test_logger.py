"""日志脱敏过滤器单测。

重点:脱敏不能破坏日志格式化——uvicorn/httpx 的访问日志大量使用 `%d`,
若把参数一律 `str()` 化会抛 TypeError,日志被吞且往 stderr 刷内部错误。
"""
import logging

from app.utils.logger import _MaskFilter, _mask


def _record(msg, args):
    return logging.LogRecord("t", logging.INFO, __file__, 1, msg, args, None)


def test_masks_sensitive_fields() -> None:
    assert "SECRET" not in _mask("cookie=SECRETVALUE123")
    assert "hunter2" not in _mask("password=hunter2 x")


def test_keeps_non_string_args_for_formatting() -> None:
    """%d 参数必须保持数字类型,否则格式化会抛 TypeError。"""
    rec = _record('HTTP %s "%s %d %s"', ("POST", "HTTP/1.1", 200, "OK"))
    _MaskFilter().filter(rec)
    assert rec.args[2] == 200 and isinstance(rec.args[2], int)
    assert rec.getMessage() == 'HTTP POST "HTTP/1.1 200 OK"'  # 不抛异常


def test_masks_string_args_only() -> None:
    rec = _record("采集 %s 用量 %d", ("cookie=ABCDEFGH", 3))
    _MaskFilter().filter(rec)
    assert "ABCDEFGH" not in rec.getMessage() and rec.args[1] == 3


def test_dict_args_supported() -> None:
    rec = _record("%(n)d 条 %(who)s", {"n": 5, "who": "cookie=XYZ12345"})
    _MaskFilter().filter(rec)
    assert rec.getMessage().startswith("5 条") and "XYZ12345" not in rec.getMessage()
