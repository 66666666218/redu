# -*- coding: utf-8 -*-
"""小红书:**签名 oracle** —— 让**站点自己的 JS** 算签名(2026-10-12)。

## 要解决什么
现状是"**Python 复刻算法 + 手工维护版本号**":`x0` 从 4.3.5 追到 4.4.3、环境标志
19 位要从真签名里解密才拿得到……每改一次版我们追一次,**追慢一次就被判成异常客户端**
(`461` / `300011`),而它的表现和"账号被风控"**一模一样**,极难分辨。

`window._webmsxyw` 是**站点自己打包进页面的**签名函数。让它算,出来的必然是**线上版本**,
于是下面这些维护负担全部消失:

| 现状要维护 | oracle 之后 |
|---|---|
| `SIGN_TEMPLATE` 的 `x0` 版本号 | 不需要(页面自带) |
| `SIGN_ENV_FLAGS` 19 位环境标志 | 不需要(页面自带) |
| `HEX_KEY` / 字母表 / 各种前缀 | 不需要 |
| 改版后"比对常量"的排查流程 | 不存在了 |

## 实测(2026-10-12,`tools/xhs_oracle_probe.py`)
匿名页面 → `window._webmsxyw` 签搜索请求 → **HTTP 200**(不是 461、不是 406)。
⇒ 签名这一关**通了**;剩下的 `-100/-104` 是**凭据问题**,不是签名问题。

## ⚠️ 三条纪律
1. **浏览器全程匿名**。绝不往页面里注入 `web_session`。链路文档 §7 坑③ 实测:
   带登录 session 的无头页面一旦导航,服务端会下发删除指令把 session 作废。
   这里页面**匿名加载**,只借它的 JS 与 `a1` —— 登录态只出现在**我们发的 HTTP 头**里。
2. **只借 `a1`**:把我们自己那枚 `a1`(与 `web_session` 配对的那枚)注入页面,
   保证签名里的设备标识与请求 cookie 一致。
3. **它是增强,不是替代**:`search()` 仍保留 `xhshow` 那条路作回落 ——
   oracle 起不来(没装 playwright / 没浏览器 / 页面被改)时自动退回,整条链不会断。
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Any

from app.utils import get_logger

logger = get_logger(__name__)

HOME = "https://www.xiaohongshu.com/explore"

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36")

#: 页面复用上限。**主动重建**是有意的:常驻太久的页面本身就是异常特征,
#: 而且站点推送的 JS 变了之后旧页面算的还是老版本。
MAX_CALLS = 200
MAX_AGE_S = 1800.0
#: 页面加载后等站点把 `a1`/`gid` 写出来、bundle 初始化完
SETTLE_MS = 3000

_lock = threading.Lock()
_page: Any = None
_ctx: Any = None
_browser: Any = None
_pw: Any = None
_a1: str = ""
_calls = 0
_born = 0.0
_xs_common = ""      #: 从页面自己发的请求里截获的真实 `x-s-common`


class OracleUnavailable(Exception):
    """oracle 起不来(缺依赖/页面结构变了)。调用方应回落到 `xhshow`。"""


def _fresh() -> Any:
    """**匿名**开一个页面(不注入任何登录态),并截获页面自己发的真实头。"""
    global _page, _ctx, _browser, _pw, _calls, _born, _a1, _xs_common

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise OracleUnavailable("没装 playwright") from exc

    if _pw is not None:
        _close()

    _pw = sync_playwright().start()
    try:
        _browser = _pw.chromium.launch(headless=True)
        _ctx = _browser.new_context(user_agent=_UA, locale="zh-CN",
                                    timezone_id="Asia/Shanghai")
        page = _ctx.new_page()

        caught: list[str] = []

        def _grab(req) -> None:
            """截获页面**自己**发的真实 `x-s-common` —— 最权威的"真浏览器"样本。"""
            if caught or "xiaohongshu.com" not in req.url:
                return
            try:
                v = (req.headers or {}).get("x-s-common") or ""
            except Exception:  # noqa: BLE001
                return
            if v:
                caught.append(v)

        page.on("request", _grab)
        page.goto(HOME, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(SETTLE_MS)
    except Exception as exc:  # noqa: BLE001
        _close()
        raise OracleUnavailable(f"打开页面失败:{type(exc).__name__}: {str(exc)[:90]}") from exc

    _page = page
    _xs_common = caught[0] if caught else ""
    _calls = 0
    _born = time.time()
    _a1 = ""
    logger.info("小红书签名 oracle:匿名页面就绪(真实 x-s-common %s)",
                f"{len(_xs_common)} 字节" if _xs_common else "**没截到**")
    return page


def _close() -> None:
    global _page, _ctx, _browser, _pw, _calls, _born, _a1, _xs_common
    for obj, name in ((_browser, "browser"), (_pw, "playwright")):
        if obj is not None:
            try:
                obj.close() if name == "browser" else obj.stop()
            except Exception:  # noqa: BLE001
                pass
    _page = _ctx = _browser = _pw = None
    _calls = 0
    _born = 0.0
    _a1 = ""
    _xs_common = ""


def _ensure(a1: str) -> Any:
    """拿到一个**可用且 a1 一致**的页面;不新鲜就重建。"""
    global _a1
    if _page is None:
        _fresh()
    elif _calls >= MAX_CALLS or (time.time() - _born) > MAX_AGE_S:
        logger.info("小红书签名 oracle:页面到期(调用 %d 次 / %.0fs),重建", _calls,
                    time.time() - _born)
        _fresh()
    if a1 and a1 != _a1:
        #: ★ 只注入 `a1`(设备标识),**绝不注入 `web_session`** —— 见模块 docstring 纪律 1
        try:
            _ctx.add_cookies([{"name": "a1", "value": a1,
                               "domain": ".xiaohongshu.com", "path": "/"}])
            _a1 = a1
        except Exception:  # noqa: BLE001
            logger.debug("注入 a1 失败(签名仍可用,只是设备标识不同源)", exc_info=True)
    return _page


def sign(uri: str, payload: dict | None = None, *, a1: str = "",
         params: dict | None = None) -> dict[str, str]:
    """**让站点自己的 JS 签**这个请求,返回 `x-s` / `x-t` / `x-s-common`。

    ⚠️ 只返回**签名头**;`Cookie` 头要调用方自己加(链路文档 §4 坑①:
    `xhshow` 与 oracle 都不替你带 `Cookie`,少了它接口回 `-101`)。

    `payload` 会用**紧凑 JSON** 参与签名 —— 调用方发出去的 body 必须**逐字节**一致。
    """
    with _lock:
        page = _ensure(a1)
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False) \
            if payload is not None else ""
        content = uri + body
        try:
            out = page.evaluate(
                "([c,u,p]) => window._webmsxyw(c,u,p)",
                [content, hashlib.md5(content.encode()).hexdigest(),
                 hashlib.md5(uri.encode()).hexdigest()])
        except Exception as exc:  # noqa: BLE001
            raise OracleUnavailable(
                f"页面签名失败:{type(exc).__name__}: {str(exc)[:90]}") from exc
        global _calls
        _calls += 1

    if not isinstance(out, dict) or not out.get("X-s"):
        raise OracleUnavailable(f"_webmsxyw 没返回签名:{str(out)[:100]}")
    return {"x-s": str(out["X-s"]), "x-t": str(out.get("X-t") or ""),
            "x-s-common": _xs_common or ""}


def headers(uri: str, cookies: dict[str, str], *, payload: dict | None = None,
            extra: dict | None = None) -> dict[str, str]:
    """造一份**可直接发**的请求头(含 `Cookie`)。回落 `xhshow` 时也能用。"""
    a1 = str(cookies.get("a1") or "")
    try:
        signed = sign(uri, payload, a1=a1)
    except OracleUnavailable as exc:
        logger.warning("小红书签名 oracle 不可用,回落 xhshow:%s", exc)
        from app.services.xhs_protocol_source import _signer

        signed = _signer().sign_headers_post(uri, cookies, payload=payload,
                                             sign_format="xyw", x_rap=True)
    out = {k: v for k, v in signed.items() if v}
    out["x-mns"] = "unload"
    out["content-type"] = "application/json;charset=UTF-8"
    out["user-agent"] = _UA
    out["referer"] = "https://www.xiaohongshu.com/"
    out["origin"] = "https://www.xiaohongshu.com"
    #: ⚠️ **必须自己带 Cookie**(签名器不替你加)
    out["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items() if v)
    if extra:
        out.update(extra)
    return out


def shutdown() -> None:
    """关掉常驻页面(进程退出/调度器停摆时调)。不调也不会泄漏:进程结束就没了。"""
    with _lock:
        _close()
        logger.info("小红书签名 oracle:已关闭")
