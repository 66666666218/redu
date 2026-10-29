# -*- coding: utf-8 -*-
"""推送新鲜度闸门单测(2026-10-29)。

用户口径:「全部都需要新鲜度检查,所有板块不要都跟知乎一样占比这么少」。
规则:按**发布时间**裁 `PUSH_FRESH_DAYS` 天;**判不了时间的放行并单列计数**
(把"不知道"当"老"会把整条板堵死)。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import time  # noqa: E402

from app.services import freshness_gate as fg  # noqa: E402


class _S:
    push_fresh_days = 7
    push_fresh_allow_unknown = True


def test_zero_and_missing_mean_unknown_not_1970() -> None:
    """⚠️ 各源用 `publish_at=0` 表示"没给时间";照 fromtimestamp(0) 会变成 1970 年
    ⇒ 每一条都被判成"太旧"。"""
    assert fg._as_dt(0) is None and fg._as_dt(None) is None and fg._as_dt("0") is None
    assert fg.is_stale(0, _S()) is False


def test_is_stale_respects_window() -> None:
    now = time.time()
    assert fg.is_stale(now - 10 * 86400, _S()) is True
    assert fg.is_stale(now - 2 * 86400, _S()) is False
    assert fg.is_stale("2026-01-01", _S()) is True


def test_split_fresh_counts_stale_and_unknown() -> None:
    now = int(time.time())
    items = [{"publish_at": now - 2 * 86400}, {"publish_at": now - 10 * 86400},
             {"publish_at": 0}, {}]
    kept, stale, unknown = fg.split_fresh(items, _S())
    assert len(kept) == 3 and stale == 1 and unknown == 2
    assert fg.summary(len(kept), stale, unknown).startswith("新鲜 3/4")


def test_unknown_can_be_dropped_by_config() -> None:
    class _Strict(_S):
        push_fresh_allow_unknown = False

    kept, stale, unknown = fg.split_fresh([{"publish_at": 0}], _Strict())
    assert kept == [] and unknown == 1


def test_zero_days_disables_the_gate() -> None:
    class _Off(_S):
        push_fresh_days = 0

    items = [{"publish_at": 1}]
    assert fg.split_fresh(items, _Off()) == (items, 0, 0)
