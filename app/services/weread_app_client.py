"""微信读书 **App 侧**客户端:`i.weread.qq.com`(2026-10-05 打通)。

**为什么要它 —— 这是唯一还能拿到精确阅读数的路**:

| 路 | 状态 |
|---|---|
| 网页版 `/web/mp/articles` | ❌ **账号级被拦** `-2041`(2026-10-05 控制变量实验:刚续期 1 分钟内、三种上下文、8 个号全挂) |
| **App 版 `/book/articles`** | ✅ **通,且带精确 `readNum`** |
| 微信 PC 端 `getappmsgext` | ❌ `ret=0` 但不返回 `appmsgstat`(5 种参数形态都一样) |

**怎么找到的**(留档,免得下次重新摸):用 mitmproxy 抓雷电模拟器里微信读书 App 的流量
(`tools/mitm_capture_weread.py` + `adb reverse` 隧道 + 系统区 CA)。**结论:鉴权不是
`Authorization`,而是两个自定义头** ——

    accessToken: <token>      ← 名字就叫 accessToken
    vid: <用户 vid>

外加一组设备标识头(`baseapi`/`appver`/`osver`/`channelId`/`basever`)。

⚠️ **App 与网页是两套完全独立的鉴权**:网页那枚 cookie 在 App 接口上回 `-2012 登录超时`
(实测),两者不能互相替代。

⚠️ **`accessToken` 会随 App 会话轮换**(实测三个值:`3RRAf8Il` → `slJzpb3p` → `ALRPC8tY`),
且**与设备上的那枚同步**。所以取 token 要走 `scripts/weread_app_login.py`
(从模拟器的 `WRAccount` 库里读),不能只存一次。

⚠️ **token 从哪来不影响调用**:拿到后**任何机器**都能直连 `i.weread.qq.com`
(本模块的请求是从本机 Windows 发出的,与模拟器无关)⇒ **模拟器只需偶尔开一次取 token**。
"""
from __future__ import annotations

from typing import Any

import requests

from app.utils import get_logger

logger = get_logger(__name__)

BASE = "https://i.weread.qq.com"
# 与抓包实测一致(App 版本 8.2.6 / Android 14);服务端未对版本做严格校验,
# 但**缺 `accessToken`/`vid` 必被拒**,所以这两个是硬要求。
_DEFAULT_HEADERS = {
    "Accept-Charset": "UTF-8",
    "Accept": "*/*",
    "baseapi": "34",
    "appver": "8.2.6.10163989",
    "basever": "8.2.6.10163989",
    "osver": "14",
    "channelId": "0",
    "User-Agent": ("WeRead/8.2.6 WRBrand/other Dalvik/2.1.0 "
                   "(Linux; U; Android 14; 25060RK16C)"),
}


class WereadAppError(RuntimeError):
    """App 接口调用失败(网络/业务码/缺凭据)。**硬失败一律抛**,不返回空。"""


class WereadAppAuthError(WereadAppError):
    """登录态问题(`-2010 用户不存在` / `-2012 登录超时`)⇒ **需要重新取 token**。

    单独成一类,方便上层把"该去模拟器重新取 token"与"网络抖动"分开处理 ——
    两者的处置完全不同。
    """


class WereadAppClient:
    """App 侧只读客户端。构造需要 `access_token` 与 `vid`(见 `scripts/weread_app_login.py`)。

    **自愈**:`accessToken` 会随 App 会话轮换(实测三个值),轮换后接口回
    `-2010/-2012`、阅读数又断。所以构造时可以传 `on_auth_error` ——
    一个"重取凭据"的回调,**遇到登录态错误时自动重取并重试一次**。
    没有它,就得靠人记得去跑脚本;那种机制迟早会忘(本仓已经吃过一次)。

    ⚠️ **只重试一次**:重取还要开模拟器,取不到就别死循环 —— 取不到就照常抛,
    由上层按"这次没有兜底"处理。
    """

    def __init__(self, access_token: str, vid: str | int, *, timeout: int = 20,
                 on_auth_error=None) -> None:
        if not access_token or not vid:
            raise WereadAppAuthError("缺 accessToken 或 vid(跑 scripts/weread_app_login.py 取)")
        self._token, self._vid, self._timeout = access_token, str(vid), timeout
        self._on_auth_error = on_auth_error
        self.refreshed = False        # 供调用方/测试断言"确实自愈过"

    def _headers(self) -> dict[str, str]:
        return {**_DEFAULT_HEADERS, "accessToken": self._token, "vid": self._vid}

    def articles(self, book_id: str, *, count: int = 20, offset: int = 0) -> list[dict[str, Any]]:
        """取某公众号的文章列表(含**精确** `read_num` / `like_num`)。见 `_fetch`。"""
        try:
            return self._fetch(book_id, count, offset)
        except WereadAppAuthError:
            if self._on_auth_error is None:
                raise
            got = self._on_auth_error()          # 重取凭据(可能是 None)
            if not got:
                raise                            # 取不到就照实抛,别假装重试过
            self._token, self._vid = str(got[0]), str(got[1])
            self.refreshed = True
            logger.info("App 凭据已自愈(vid=%s),重试这一次请求", self._vid)
            return self._fetch(book_id, count, offset)

    def _fetch(self, book_id: str, count: int, offset: int) -> list[dict[str, Any]]:
        """真正发请求并解析。

        ⚠️ **业务码要当错误看**:HTTP 200 + `errcode` 是最典型的假成功(与 B站 `-352`、
        网页版 `-2041` 同一条纪律)。这里**失败抛异常,绝不返回空列表** ——
        返回空会被上层读成"这个号今天没发文章"。

        ⚠️ `requests` 是**模块级**导入(不是在函数里 import):测试要能
        `monkeypatch.setattr(模块, "requests", 替身)`;函数内 import 是局部名,**patch 不到**。
        """
        url = f"{BASE}/book/articles"
        params = {"count": str(count), "offset": str(offset),
                  "bookId": book_id, "synckey": "0"}
        try:
            r = requests.get(url, headers=self._headers(), params=params, timeout=self._timeout)
        except Exception as exc:  # noqa: BLE001 - 包成自己的异常类型
            raise WereadAppError(f"App 请求异常:{type(exc).__name__}") from exc
        try:
            payload = r.json()
        except ValueError as exc:
            raise WereadAppError(f"App 返回非 JSON(HTTP {r.status_code})") from exc
        code = payload.get("errcode")
        if code not in (None, 0):
            msg = payload.get("errmsg") or ""
            if code in (-2010, -2012):
                raise WereadAppAuthError(f"App 登录态问题:{code} {msg} ⇒ 重新取 token")
            raise WereadAppError(f"App 接口报错:{code} {msg}")
        out = []
        for it in payload.get("reviews") or []:
            rev = (it or {}).get("review") or {}
            mp = rev.get("mpInfo") or {}
            title = str(mp.get("title") or "").strip()
            if not title:
                continue
            # ⚠️ **字段名与网页端 `flatten_mp_articles` 保持逐字一致**(2026-10-05 收敛):
            # 两条路产出同一形状,调用方才不用再写一层翻译 —— 那种翻译正是"两处同构、
            # 迟早飘一个"的来源(本仓在 UA/时间解析上都吃过这个亏)。
            # 差异只有两个:**多余字段**(`mp_name` 网页端没有,带着无害)。
            out.append({
                "title": title,
                "original_id": str(mp.get("originalId") or ""),
                "review_id": str((it or {}).get("reviewId") or ""),
                "read_num": _int(mp.get("readNum")),
                "like_num": _int(mp.get("likeNum")),
                "create_time": _int(mp.get("time")),
                "mp_name": str(mp.get("mp_name") or ""),
            })
        return out


def _int(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0
