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
        marker = field
        idx = lower.find(marker)
        while idx != -1:
            val_start = idx + len(marker) + 1
            val_end = text.find(" ", val_start)
            if val_end == -1:
                val_end = len(text)
            text = text[:val_start] + "****" + text[val_end:]
            lower = text.lower()
            idx = lower.find(marker, val_start + 4)
    return text


class _MaskFilter(logging.Filter):
    """把日志记录中的敏感片段脱敏后再输出。"""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = _mask(str(record.msg))
            if record.args:
                record.args = tuple(_mask(str(a)) if a is not None else a for a in record.args)
        except Exception:  # noqa: BLE001 - 脱敏失败不影响日志输出
            pass
        return True


def setup_logging(level: int = logging.INFO) -> None:
    """初始化根日志。重复调用是幂等的。"""
    root = logging.getLogger()
    if root.handlers:
        return
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
