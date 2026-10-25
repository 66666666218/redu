"""日志工具。

- `setup_logging()`:初始化全局 logger。
- `get_logger(name)`:获取带脱敏过滤的命名 logger。
- 脱敏:对 Cookie/密码字段做部分掩码,避免泄露敏感信息(见 doc/dev.md §10)。
"""
from __future__ import annotations

import logging
import sys

# 需要脱敏的日志片段:整体替换为掩码。
_SENSITIVE_FIELDS = ("cookie", "password", "pass", "authorization")


def _mask(text: str) -> str:
    """对可能包含敏感字段的字符串做简单脱敏处理。"""
    lower = text.lower()
    for field in _SENSITIVE_FIELDS:
        # 命中形如 "field=..." 或 "field:..." 的片段,仅保留前 4 位。
        # 分隔符校验:field 后必须紧跟 =/: 才算键值对——否则会把
        # "Cookie 定时续期完成:%d" 这类普通语句误判,吞掉 %d 占位符引发 Logging error
        marker = field
        idx = lower.find(marker)
        while idx != -1:
            sep = lower[idx + len(marker): idx + len(marker) + 1]
            if sep not in ("=", ":"):
                idx = lower.find(marker, idx + len(marker))
                continue
            val_start = idx + len(marker) + 1
            val_end = text.find(" ", val_start)
            if val_end == -1:
                val_end = len(text)
            text = text[:val_start] + "****" + text[val_end:]
            lower = text.lower()
            idx = lower.find(marker, val_start + 4)
    return text


class _MaskFilter(logging.Filter):
    """把日志记录中的敏感片段脱敏后再输出。

    只对**字符串**参数脱敏:原实现把所有参数 `str()` 化,会让
    `logger.info('... %d ...', 200)` 这类记录在格式化时抛
    `TypeError: %d format: a real number is required`(uvicorn/httpx 的访问日志
    大量使用 %d),日志被 logging 吞掉并往 stderr 刷内部错误。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = _mask(record.msg)
            if isinstance(record.args, tuple):
                record.args = tuple(_mask(a) if isinstance(a, str) else a for a in record.args)
            elif isinstance(record.args, dict):
                record.args = {k: (_mask(v) if isinstance(v, str) else v) for k, v in record.args.items()}
        except Exception:  # noqa: BLE001 - 脱敏失败不影响日志输出
            pass
        return True


def _utf8_stream(stream):
    """把输出流切成 **UTF-8**;切不动就原样返回(不抛)。

    ⚠️⚠️ **为什么必须做**(2026-10-26 实测):Windows 上服务由 `pythonw` 启动、
    stdout 重定向到 `data/app.log`,此时流的编码是**系统 locale = GBK**。
    任何带 emoji / `⇒` / `⚠️` 的日志消息都会在 `stream.write()` 处抛
    `UnicodeEncodeError`;logging 捕获后把「--- Logging error ---」+ **30 行调用栈**
    写进 stderr(也进同一个文件)⇒ **真正的那句日志没了**,换来一堆噪声。

    实测代价:`67MB` 的 `app.log` 里 **2056 个 traceback**,绝大多数就是它 ——
    也就是说,**我们最想看的那些带箭头/emoji 的诊断句,恰恰是最容易被丢掉的那批**。
    """
    try:
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
            return stream
    except Exception:  # noqa: BLE001 - 切不动就退到下面的包装
        pass
    try:
        import io

        return io.TextIOWrapper(stream.buffer, encoding="utf-8",
                                errors="replace", line_buffering=True)
    except Exception:  # noqa: BLE001 - 连 buffer 都没有:原样用,别让日志系统起不来
        return stream


def setup_logging(level: int = logging.INFO) -> None:
    """初始化根日志。重复调用是幂等的。"""
    root = logging.getLogger()
    if root.handlers:
        return
    #: 先切编码再建 handler —— 顺序反了那第一行就可能丢(见 `_utf8_stream`)
    sys.stdout = _utf8_stream(sys.stdout)
    sys.stderr = _utf8_stream(sys.stderr)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    handler.addFilter(_MaskFilter())
    root.addHandler(handler)
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    """返回命名 logger。日志输出经 `_MaskFilter` 自动脱敏。"""
    return logging.getLogger(name)
