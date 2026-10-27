"""百度网盘**扫码登录**(纯协议,2026-10-27,用户口径「我给你扫俩百度你弹出来二维码」)。

## 它解决什么
百度盘配额已经爆了(实测 `used≈5.05 TiB / total≈120 GiB`,超额 43 倍),
96 条待转存全卡在「空间不足 -32」。用户的解法是**再加两个号**;
而交号最省事的方式不是手抄 cookie,是**扫一次码**。

## 协议形状(全部实测于 2026-10-27)
1. `GET passport.baidu.com/v2/api/getqrcode?lp=pc&qrloginfrom=pc&gid=<UUID>&apiver=v3&tt&tpl=netdisk`
   → `{"errno":0, "sign":"…", "imgurl":"passport.baidu.com/v2/api/qrcode?sign=…"}`
2. 把 `imgurl` 取成 PNG 给人扫(二维码**内容不返回**,只有图)。
3. **长轮询** `GET passport.baidu.com/channel/unicast?channel_id=<sign>&tpl=netdisk`
   · 没变化时**挂住 ≈30 秒**再回 `{"errno":1}`(不是错误,是"继续等");
   · 有变化回 `{"errno":0,"channel_v":"{\"status\":N}"}`,N: 0 等扫 / 1 已扫待确认 / 2 已确认。
4. 确认后取登录 cookie —— ⚠️ **这一步是整套里唯一没有公开文档的**:
   老实现(`BaiduPCS-Go` 3.x)打的是 `/v2/api/unicast`,但现行站点对**未确认的票**
   一律 302 到 `error.html`,所以**确认前探不出对错**。本模块按候选逐个试,
   **判据始终是"会话里出现了 BDUSS"**,而不是某个接口回了 200(那正是假成功)。

## 纪律
- **只有看到 BDUSS 才写库**;而且写库前还要用 `BaiduPanClient.keepalive()` 验一次
  网页会话(本仓 2026-10-04 踩过:只有 BDUSS 没有 STOKEN 的「半登录」,
  `loginStatus` 回 errno:0,转存却全失败)。
- 凭据**不打印、不落文件**;二维码 PNG 落在 `data/`(已 gitignore)。
- 二维码**每次一个唯一文件名**(sign 后缀)—— 固定名会被下一次覆盖,
  而看图软件可能还缓存着旧图,人就会白扫一张没人轮询的废码(小红书那次踩过)。
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import requests

from app.utils import get_logger

logger = get_logger(__name__)

PASSPORT = "https://passport.baidu.com"
PAN = "https://pan.baidu.com"
_TIMEOUT = 20
#: 长轮询**必须**给足超时:服务端会故意挂住 30 秒左右(那段不是卡死,是省请求)
_POLL_TIMEOUT = 45
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

#: 状态文案 —— `status` 是百度回的数字,不是 HTTP 码
STATUS_TEXT = {0: "等待扫码", 1: "已扫码,等手机上点「确认登录」",
               2: "已确认", 3: "二维码已过期"}

#: 登录态里**必须有**的两项:BDUSS 是身份,STOKEN 决定能不能转存/分享。
#: 只有 BDUSS 的「半登录」会被本模块**拒收**(见模块头「纪律」)。
_REQUIRED_COOKIES = ("BDUSS", "STOKEN")

#: 确认后**逐个试**的取码入口(`(method, path)`)。
#: ⚠️ 顺序 = 由新到旧:现行站点优先,老实现兜底。**谁回了什么都记日志** ——
#: 这一步失败时必须能说出"打到哪、回了什么",否则只能让人重扫。
_GRANT_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("GET", "/v2/api/unicast"),
    ("GET", "/v2/api/getcookie"),
    ("POST", "/v2/api/unicast"),
    ("GET", "/v2/api/qrcode/unicast"),
)


class BaiduQrError(Exception):
    """扫码登录失败(带 `kind`,方便脚本按类型给不同提示)。"""

    def __init__(self, message: str, *, kind: str = "error") -> None:
        super().__init__(message)
        self.kind = kind


@dataclass
class QrTicket:
    """一次扫码的**全部状态**。它必须被一路带着 —— 确认后取 cookie 靠的是同一会话。"""
    session: requests.Session
    sign: str
    img_url: str
    gid: str
    created_at: float = field(default_factory=time.time)
    #: 取码阶段实际命中的入口(排查用;失败时空)
    grant_hit: str = ""


def create_qr(timeout: float = _TIMEOUT) -> QrTicket:
    """申请一张二维码。返回的 `session` **必须**用于后续轮询与取码。"""
    session = requests.Session()
    session.headers.update({"User-Agent": _UA, "Referer": f"{PAN}/"})
    ts = int(time.time() * 1000)
    gid = str(uuid.uuid4()).upper()
    params = {"lp": "pc", "qrloginfrom": "pc", "gid": gid, "callback": "",
              "apiver": "v3", "tt": ts, "tpl": "netdisk", "_": ts}
    try:
        resp = session.get(f"{PASSPORT}/v2/api/getqrcode", params=params, timeout=timeout)
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise BaiduQrError(f"申请二维码失败:{type(exc).__name__}", kind="network") from exc
    sign = str(data.get("sign") or "")
    img = str(data.get("imgurl") or "")
    if not sign or not img:
        raise BaiduQrError(f"申请二维码失败:errno={data.get('errno')} "
                           f"msg={str(data.get('errmsg') or data.get('msg') or '')[:60]}",
                           kind="protocol")
    if img.startswith("//"):
        img = "https:" + img
    elif not img.startswith("http"):
        img = "https://" + img
    return QrTicket(session=session, sign=sign, img_url=img, gid=gid)


def download_qr(ticket: QrTicket, path: Path, timeout: float = _TIMEOUT) -> Path:
    """把二维码 PNG 落到 `path`。**二维码只有图、没有内容串** ⇒ 只能存图给人扫。"""
    resp = ticket.session.get(ticket.img_url, timeout=timeout)
    if resp.status_code != 200 or not resp.content:
        raise BaiduQrError(f"下载二维码失败:HTTP {resp.status_code}", kind="network")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(resp.content)
    return path


def parse_status(payload: object) -> int | None:
    """从轮询响应里读 `status`。**没有状态变化返回 None**(`errno:1` 就是"继续等")。

    纯函数,单测直接喂字典 —— 这里最容易把「还在等」误读成「出错」。
    """
    if not isinstance(payload, dict):
        return None
    try:
        errno = int(payload.get("errno"))
    except (TypeError, ValueError):
        return None
    if errno != 0:
        return None
    raw = payload.get("channel_v")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    if not isinstance(raw, dict):
        return None
    try:
        return int(raw.get("status"))
    except (TypeError, ValueError):
        return None


def poll(ticket: QrTicket, timeout: float = _POLL_TIMEOUT) -> tuple[int | None, str]:
    """长轮询一次。返回 `(status 或 None, 诊断串)` —— **网络抖动不抛**,调用方继续等。"""
    params = {"channel_id": ticket.sign, "tpl": "netdisk", "callback": "",
              "apiver": "v3", "tt": int(time.time() * 1000), "_": int(time.time() * 1000)}
    try:
        resp = ticket.session.get(f"{PASSPORT}/channel/unicast", params=params,
                                  timeout=timeout)
        return parse_status(resp.json()), ""
    except (requests.RequestException, ValueError) as exc:
        return None, f"{type(exc).__name__}"


def wait_for_login(ticket: QrTicket, *,
                   on_status: Callable[[int | None, str], None] | None = None,
                   deadline_s: float = 300) -> int:
    """等到 `status==2`。超时抛 `BaiduQrError(kind="timeout")`。"""
    deadline = time.monotonic() + deadline_s
    last: object = object()          # 哨兵:保证第一次一定回调
    while time.monotonic() < deadline:
        status, diag = poll(ticket)
        if on_status and (status, diag) != last:
            last = (status, diag)
            on_status(status, diag)
        if status == 2:
            return status
        if status == 3:
            raise BaiduQrError("二维码已过期,请重新生成", kind="expired")
    raise BaiduQrError(f"等待扫码超时({int(deadline_s)} 秒)", kind="timeout")


def cookie_map(ticket: QrTicket) -> dict[str, str]:
    """把会话里 **baidu.com 域**的 cookie 摊成 `{名: 值}`。"""
    out: dict[str, str] = {}
    for c in ticket.session.cookies:
        if str(c.domain or "").endswith("baidu.com"):
            out[str(c.name)] = str(c.value)
    return out


def complete(ticket: QrTicket) -> str:
    """确认之后**取登录态**,返回整条 cookie 串。拿不到 `BDUSS` 就抛错,绝不猜。

    ⚠️ 这一步是整套里唯一没有现行文档的(见模块头)。所以它**不赌单个接口**:
    逐个候选打一遍,判据只有一个 —— **会话里出现了 BDUSS**。
    """
    params = {"channel_id": ticket.sign, "tpl": "netdisk", "callback": "",
              "apiver": "v3", "tt": int(time.time() * 1000), "_": int(time.time() * 1000)}
    tried: list[str] = []
    for method, path in _GRANT_CANDIDATES:
        before = set(cookie_map(ticket))
        try:
            resp = ticket.session.request(
                method, f"{PASSPORT}{path}", params=params if method == "GET" else None,
                data=params if method == "POST" else None,
                timeout=_TIMEOUT, allow_redirects=False)
            note = f"{method} {path} → HTTP {resp.status_code}"
            body = resp.text[:120].replace("\n", " ")
        except requests.RequestException as exc:
            tried.append(f"{method} {path} → {type(exc).__name__}")
            continue
        got = cookie_map(ticket)
        if "BDUSS" in got:
            ticket.grant_hit = f"{method} {path}"
            logger.info("百度扫码:取码命中 %s(新增 cookie %s)", ticket.grant_hit,
                        sorted(set(got) - before))
            return _join(got)
        tried.append(f"{note} body={body!r}")
    raise BaiduQrError("已确认登录,但**没取到 BDUSS**。候选入口逐个试过:" +
                       " | ".join(tried), kind="grant")


def _join(cookies: dict[str, str]) -> str:
    #: 顺序无关,但**同名只留一个**;这里用 dict 天然去重
    return "; ".join(f"{k}={v}" for k, v in cookies.items() if v)


def missing_required(cookie: str) -> list[str]:
    """这条 cookie 串缺哪些必需项(`BDUSS` / `STOKEN`)。"""
    have = {seg.split("=", 1)[0].strip() for seg in (cookie or "").split(";") if "=" in seg}
    return [k for k in _REQUIRED_COOKIES if k not in have]


def verify(cookie: str) -> dict:
    """用**真实业务接口**验这枚 cookie:返回 `{"ok", "why", "ratio"}`。

    ⚠️ 判据不能是 `loginStatus`(见模块头),必须打 `/api/quota`。
    """
    if not cookie:
        return {"ok": False, "why": "cookie 是空的", "ratio": None}
    try:
        from app.services.baidupan_transfer import BaiduPanAuthError, BaiduPanClient

        client = BaiduPanClient(cookie)
        try:
            client.keepalive()
        except BaiduPanAuthError as exc:
            return {"ok": False, "why": str(exc)[:160], "ratio": None}
        usage = client.quota_usage()
    except Exception as exc:  # noqa: BLE001 - 验不过就是"不能用",别让异常冒到 CLI 外
        return {"ok": False, "why": f"{type(exc).__name__}: {str(exc)[:120]}", "ratio": None}
    if not usage:
        return {"ok": False, "why": "配额接口没给数(登录态可能不完整)", "ratio": None}
    return {"ok": True, "why": "", "ratio": usage["ratio"], "usage": usage}


# ---------------------------------------------------------------- 入库(两条登录路共用)

def next_pool_names(db, user_id: int) -> tuple[str, str]:
    """没指定名字时,挑一个**不会覆盖别人**的 `(label, cookie_key)`。

    ⚠️ 不能按"账号数 +1"硬算:池里已有 `百度2`(历史命名),再算成 `百度2`
    就会**按 label 覆盖**掉它的凭据(`pan_accounts.add_account` 的语义)。
    所以取**现有数字后缀的最大值 +1**。
    ⚠️ `cookie_key` 也不能走 `cookie_store.list_cookies` —— 它只遍历基础平台名,
    `baidupan#2` 这类多账号键**不在里面** ⇒ 第二次跑还会算成 `#2`,覆盖刚登的那个。
    """
    from sqlalchemy import select

    from app.db.models import UserCookie
    from app.services import pan_accounts

    labels = [r.label for r in pan_accounts.accounts(db, "baidupan", user_id=user_id)]
    nums = [int("".join(ch for ch in lb if ch.isdigit()))
            for lb in labels if any(ch.isdigit() for ch in lb)]
    label = f"百度{max(nums, default=1) + 1}"

    keys = {str(k) for k in db.scalars(
        select(UserCookie.platform).where(UserCookie.user_id == user_id)).all()}
    n = 2
    while f"baidupan#{n}" in keys:
        n += 1
    return label, f"baidupan#{n}"


def register_account(db, *, label: str, cookie: str, cookie_key: str,
                     user_id: int, ratio: float | None = None):
    """把验活的凭据写进 `cookie_store` + 账号池,返回池里的 `AccountRef`。

    ⚠️ **调用方必须先 `verify()` 通过** —— 这里不重复验活(免得两次网络请求),
    但也因此**绝不能拿它存未验过的串**。
    """
    from app.services import pan_accounts

    pan_accounts.add_account(db, "baidupan", label, cookie,
                             cookie_key=cookie_key, user_id=user_id)
    ref = next((r for r in pan_accounts.accounts(db, "baidupan", user_id=user_id)
                if r.cookie_key == cookie_key), None)
    if ref is not None and ratio is not None:
        pan_accounts.report(db, ref, ratio=float(ratio), full=False,
                            why=f"扫码登录后实测已用 {float(ratio) * 100:.1f}%")
    return ref


__all__ = ["BaiduQrError", "QrTicket", "STATUS_TEXT", "create_qr", "download_qr",
           "parse_status", "poll", "wait_for_login", "cookie_map", "complete",
           "missing_required", "verify", "next_pool_names", "register_account"]
