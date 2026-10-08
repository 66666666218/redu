# -*- coding: utf-8 -*-
"""从**已登录的小红书浏览器档案**导出凭据,写进加密 cookie 库(2026-10-08)。

## 为什么要搬进服务层
和抖音那份同源:这段逻辑原来只活在 `tools/xhs_export_cookie.py`(**gitignored 的本地工具**),
而现在**体检要在凭据失效时自动重导**(见 `chain_health._xhs_reheal`)——
**服务层不能依赖 `tools/`**(Docker 镜像根本不 COPY 它)。

## ⚠️ 三条纪律(都是小红书那次交的学费)
1. **不许静默回退到自带 Chromium** —— 它读不了 Edge 档案里的加密 cookie,会读成 0 个,
   还把人误导成"档案没登录"。Edge 起不来是偶发的 ⇒ 重试 3 次,仍不行就明确失败。
2. **只读、不导航** —— 开完直接读 cookie,不打开任何页面。
3. 凭据**只写加密库**,不打印、不落文件。

## ★ 什么时候**不该**重导(2026-10-08 实测的教训)
重导只能修"**凭据掉了/被踢**",**修不了"账号被限制"**。
实测:压测把号打进 `-104 您当前登录的账号没有权限访问` 之后,
**重导多少次都没用**(凭据能认证,是账号没权限),而且**反复重试/重登只会延长封锁**。
⇒ 调用方必须**按失败类型决定**:只有 `need_login` 才重导,`restricted` 要**停手等**。
"""
from __future__ import annotations

import time
from pathlib import Path

from app.utils import get_logger

logger = get_logger(__name__)

PLATFORM = "xiaohongshu"
#: 协议那条路用的那份凭据来自这个档案(与页面渲染的多档位轮换**不是一回事**)
DEFAULT_PROFILE = Path(__file__).resolve().parents[2] / "data" / "xhs_2"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
#: 少了这几个基本就是"不完整"的凭据(手抄那份正是缺 `id_token`)
KEY_ITEMS = ("web_session", "a1", "webId", "id_token")
COOKIE_URL = "https://www.xiaohongshu.com"


def read_profile_cookies(profile: Path | str | None = None) -> dict[str, str]:
    """从档案读出 cookie 字典。**失败抛异常**(绝不返回空字典 —— 那会伪装成"档案没登录")。"""
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
                logger.warning("导出小红书凭据:Edge 第 %d 次没起来,重试…", attempt)
                time.sleep(2)
        if ctx is None:
            raise RuntimeError(f"Edge 起了 3 次都没起来:{last}")
        try:
            jar = ctx.cookies(COOKIE_URL)
        finally:
            ctx.close()          # 正常退出:让 Chromium 自己收拾干净
    return {c["name"]: c["value"] for c in jar}


def export_from_profile(db, user_id: int = 1, profile: Path | str | None = None) -> str:
    """读档案 → 写加密库。返回一句**人话**;凭据值不返回。"""
    from app.services.cookie_store import set_cookie

    ck = read_profile_cookies(profile)
    if not ck:
        raise RuntimeError("一个 cookie 都没读到(档案可能没登录)")
    blob = "; ".join(f"{k}={v}" for k, v in ck.items())
    set_cookie(db, user_id, PLATFORM, blob)
    missing = [k for k in KEY_ITEMS if k not in ck]
    if missing:
        # ⚠️ **不静默成功**:缺关键项正是"导了个空壳还以为修好了"的形状
        return f"{len(ck)} 个 cookie,但**缺 {'/'.join(missing)}** —— 档案里多半没登录"
    return f"{len(ck)} 个 cookie(关键项齐全)"
