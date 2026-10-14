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


def read_ctx_cookies(ctx) -> dict[str, str]:
    """从一个**活着的浏览器 context** 读 cookie —— ★ **必须按小红书域名收口**。

    ## 为什么这是一个必须写在函数里的坑(2026-10-14 实测定位,两处代价)
    `ctx.cookies()`(**不传 URL**)返回的是**档案里所有域名**的 cookie。一个用过的
    Edge 档案实测带回 41 枚、5700 字符,其中 `elt`(1886)+ `lt`(1673)+
    `_EDGE_S`/`SRCHUID`/`ANON`/`WLS`… **整套是微软/Bing 的**,与小红书毫无关系:

    1. **把请求打坏**:Cookie 头 5.7 KB,加其余头约 **8 KB**,正撞 HTTP/2 的
       `MAX_HEADER_LIST_SIZE` ⇒ 服务端 GOAWAY/RST ⇒ `curl_cffi` 报 **`curl: (16)`**,
       于是整条链**静默退回裸 `requests`**(JA3 是 python-requests 自己的)——
       而按本仓规矩,那一层的结论**一句都不能采信**。
    2. **它本身是自曝**:真实浏览器**绝不会**把 `live.com` 的 cookie 发给
       `xiaohongshu.com`。带着它请求,等于又一次"登录设备 ≠ 调用设备"。

    ⇒ 凡是"从浏览器取小红书 cookie"的地方**都走这里**,别再手写 `ctx.cookies()`。
    """
    return {c["name"]: c["value"] for c in ctx.cookies(COOKIE_URL) if c.get("value")}


def read_profile(profile: Path | str | None = None) -> tuple[dict[str, str], str, dict[str, str]]:
    """开档案**一次**,读齐 `(cookie, ua, sec-ch-ua*)`。失败抛异常(绝不返回空字典)。

    ★ **为什么指纹要在这一步一起读**(2026-10-14 定位):这条路产出的就是
    **写进加密库的那份凭据**,而库里的凭据**只放得下 cookie** ⇒ 指纹没有落脚点
    ⇒ 协议路 `pool.ua_of()` 拿到空串、静默退回硬编码 `Chrome/138`,
    而会话是 **Edge** 签发的(证据:库里 cookie 带 `pglt-edgeChromium-*`)
    ⇒ 正是本项目已定性过的「登录设备 ≠ 调用设备」(`300011`)。
    同一枚凭据**在同一处读齐、同一处写齐**,才不会出现"cookie 换新了、指纹还是上一位"。

    ⚠️ 纪律 2 仍然成立:`navigator.*` 是**页内属性**,开个空白页读即可 ——
    **不导航、不发请求** ⇒ 零风控成本。
    """
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
            ck = read_ctx_cookies(ctx)      #: ★ 按域名收口 —— 见 `read_ctx_cookies`
            ua, hints = _read_fp(ctx)
        finally:
            ctx.close()          # 正常退出:让 Chromium 自己收拾干净
    return ck, ua, hints


def _read_fp(ctx) -> tuple[str, dict[str, str]]:
    """在**空白页**上读 `(ua, sec-ch-ua*)` —— 纯页内读取,不发任何请求。

    读不到就回 `("", {})`:调用方据此**什么都不存**,而不是存一份编的
    (编错的自报比不报更糟 —— 见 `xhs_fingerprint` 模块头)。
    """
    from app.services import xhs_fingerprint as F

    page = ctx.new_page()
    try:
        page.goto("about:blank")          #: 空白页就够 —— 这两个属性不依赖站点
        return F.read_from_page(page)
    except Exception as exc:  # noqa: BLE001 - 读指纹失败不该让"导出凭据"整体失败
        logger.warning("读档案指纹失败(不存指纹):%s", type(exc).__name__)
        return "", {}
    finally:
        try:
            page.close()
        except Exception:  # noqa: BLE001
            pass


def read_profile_cookies(profile: Path | str | None = None) -> dict[str, str]:
    """只要 cookie 的那半边(保留旧签名给老调用点用)。**失败抛异常**。"""
    return read_profile(profile)[0]


def export_from_profile(db, user_id: int = 1, profile: Path | str | None = None) -> str:
    """读档案 → 写加密库(cookie)+ **指纹**(`xhs_fp_<uid>`)。返回一句**人话**;值不返回。"""
    from app.services import xhs_accounts
    from app.services.cookie_store import set_cookie

    ck, ua, hints = read_profile(profile)
    if not ck:
        raise RuntimeError("一个 cookie 都没读到(档案可能没登录)")
    blob = "; ".join(f"{k}={v}" for k, v in ck.items())
    set_cookie(db, user_id, PLATFORM, blob)
    #: ★ **cookie 与指纹必须同一次写入** —— 见 `read_profile` 的说明。
    xhs_accounts.save_fp(db, user_id, ua, hints)
    missing = [k for k in KEY_ITEMS if k not in ck]
    if missing:
        # ⚠️ **不静默成功**:缺关键项正是"导了个空壳还以为修好了"的形状
        return f"{len(ck)} 个 cookie,但**缺 {'/'.join(missing)}** —— 档案里多半没登录"
    from app.services import xhs_fingerprint as F

    #: ⚠️ 指纹**读到了必须说出来**:上面那条"静默退回 Chrome/138"的根因就是
    #: 没人看得见"指纹到底存没存进去"。
    fp_note = f"指纹 {F.describe(ua)}" if ua else "⚠️ **没读到指纹**(协议路会退回硬编码常量)"
    return f"{len(ck)} 个 cookie(关键项齐全);{fp_note}"
