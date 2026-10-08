# -*- coding: utf-8 -*-
"""从**已登录的抖音浏览器档案**导出凭据,写进加密 cookie 库(2026-10-08)。

## 为什么要搬进服务层
这段逻辑原来只活在 `tools/douyin_export_cookie.py`(那是 **gitignored 的本地工具**)。
而现在**体检要在凭据失效时自动重导**(见 `chain_health._douyin_reheal`)——
**服务层不能依赖 `tools/`**(Docker 镜像根本不 COPY 它,这条纪律本仓写死过)。
所以核心搬到这里,工具与体检共用同一份。

## 为什么抖音能"自动接回"而微博/知乎不能
抖音的登录态活在 **MediaCrawler 的 Chrome 档案**(`cdp_dy_user_data_dir`)里,
那个档案**只要还登着就有效** —— 重导一次即可,不用人去点。
微博/知乎的 Cookie 是**人工粘进「Cookie 管理」页**的,没有档案可导 ⇒ 只能告警提示。

## ⚠️ 纪律(与 tools 里那份一致)
1. **不许静默回退到自带 Chromium** —— 它读不了 Edge 档案里的加密 cookie,会读成 0 个,
   还把人误导成"档案没登录"。Edge 起不来是偶发的 ⇒ 重试 3 次,仍不行就明确失败。
2. **只读、不导航**:开完直接读 cookie,不打开任何页面。
3. 凭据**只写加密库**,不打印、不落文件。
"""
from __future__ import annotations

import time
from pathlib import Path

from app.utils import get_logger

logger = get_logger(__name__)

PLATFORM = "douyin"
#: 默认档案:MediaCrawler 那条链每天在用的那个号(已在生产里跑,不是新增风险)
DEFAULT_PROFILE = Path(__file__).resolve().parents[2] / "tools" / "MediaCrawler" / "browser_data" / "cdp_dy_user_data_dir"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
#: 少了 `sessionid` 系列就必然回 2483「请先登录」(2026-10-07 实测口径)
LOGIN_CORE = ("sessionid", "sessionid_ss")
#: 一个域都别漏:抖音的登录态横跨 www / 主域
COOKIE_URLS = ("https://www.douyin.com", "https://douyin.com", "https://creator.douyin.com")


def read_profile_cookies(profile: Path | str | None = None) -> dict[str, str]:
    """从档案里读出 cookie 字典。**失败抛异常**(不返回空字典 —— 那会伪装成"档案没登录")。"""
    prof = Path(profile) if profile else DEFAULT_PROFILE
    if not prof.exists():
        raise FileNotFoundError(f"档案不存在:{prof}")

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        ctx = None
        last = ""
        for attempt in (1, 2, 3):
            try:
                ctx = p.chromium.launch_persistent_context(
                    channel="msedge", executable_path=EDGE,
                    user_data_dir=str(prof), headless=True, no_viewport=True)
                break
            except Exception as exc:  # noqa: BLE001 - Edge 起不来是偶发的,重试
                last = f"{type(exc).__name__}: {str(exc)[:90]}"
                logger.warning("导出抖音凭据:Edge 第 %d 次没起来,重试…", attempt)
                time.sleep(2)
        if ctx is None:
            # ⚠️ **不回退自带 Chromium**:它读不了 Edge 的加密 cookie,会读成 0 个,
            # 然后被报成"档案没登录" —— 明明是浏览器没起来(这一坑踩过)。
            raise RuntimeError(f"Edge 起了 3 次都没起来:{last}")
        try:
            jar: list[dict] = []
            for url in COOKIE_URLS:
                try:
                    jar.extend(ctx.cookies(url))
                except Exception:  # noqa: BLE001 - 单个域取不到不影响其余
                    pass
        finally:
            ctx.close()          # 正常退出:让 Chromium 自己收拾干净
    out: dict[str, str] = {}
    for c in jar:                # 同名取最后写入的
        out[c["name"]] = c["value"]
    return out


def export_from_profile(db, user_id: int = 1, profile: Path | str | None = None) -> str:
    """读档案 → 写加密库。返回一句**人话**(给体检/日志用);凭据值不返回。

    ⚠️ **缺 `sessionid` 系列时照样写下去但返回警告** —— 不静默成功:
    那正是"导了个空壳、还以为修好了"的形状。
    """
    from app.services.cookie_store import set_cookie

    ck = read_profile_cookies(profile)
    if not ck:
        raise RuntimeError("一个 cookie 都没读到(档案可能没登录)")
    blob = "; ".join(f"{k}={v}" for k, v in ck.items())
    set_cookie(db, user_id, PLATFORM, blob)
    missing = [k for k in LOGIN_CORE if k not in ck]
    if missing:
        return f"{len(ck)} 个 cookie,但**缺 {'/'.join(missing)}** —— 档案里多半没登录"
    return f"{len(ck)} 个 cookie(登录态核心齐全)"
