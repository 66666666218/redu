"""迅雷网盘转存 + 分享(2026-10-02)。

**移植自** [ctwj/urldb](https://github.com/ctwj/urldb) 的 `common/xunlei_pan.go` +
`common/xunlei_login.go`(Go)。它把迅雷那套签名完整复刻出来了 —— 关键结论是
**签名是纯函数**(MD5 迭代盐值),不是真机签名,所以 Python 能 1:1 复刻。

**本项目实际走的凭据路线**(2026-10-02 实测,比 urldb 更省事):
  1. **扫码登录一次**(`tools/xl_qr_login.py`)→ 从网页版 localStorage 抽
     `access_token`(有效期 **12 小时**)、`refresh_token`、`captcha_token`、`device_id`;
  2. **access_token 过期** → 用 `refresh_token` 换新的:⚠️ **client_id 必须用网页版那个**
     (`Xqp0kJBXWhwaTpB6`,就是 localStorage 里 `credentials_<clientId>` 的那串),
     用 urldb 的 android `ClientID` 会 `invalid_grant`(实测);`client_secret` 传空即可;
  3. **captcha_token**:存的那份寿命**约 1.5 小时**,过期后盘写操作一律 `captcha_invalid`。
     `_captcha_token()` 会在没有存的那份时**自取兜底**,⚠️ 但自取的 token **过不了
     `/drive/v1/share`**(见该函数注释)—— 真正的解法见下面"待办"。

**流程**(同 urldb 的 `Transfer`):
  `GET  /drive/v1/share`          → 分享详情(拿 file_ids + pass_code_token)
  `POST /drive/v1/share/restore`  → 转存任务
  `GET  /drive/v1/tasks/{id}`     → 轮询到 progress==100
  `POST /drive/v1/share`          → 生成**我方**分享链

⚠️ **只做转存+分享**:「口令 → shareID」这一步只存在于迅雷客户端,本模块做不到。
调用方拿到的应该是 `pan.xunlei.com/s/<shareID>`(或带 `?pwd=` 的完整链)。
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
from urllib.parse import parse_qs, urlparse

import requests

from app.utils import get_logger

logger = get_logger(__name__)

_API = "https://api-pan.xunlei.com"
_AUTH = "https://xluser-ssl.xunlei.com"
_TIMEOUT = 25

# urldb 的 android 身份(签名盐值在这儿,用于**算 captcha_sign**;但刷新 token 要用网页版 client_id)
_CLIENT_ID = "Xp6vsxz_7IYVw2BB"
_CLIENT_VERSION = "8.31.0.9726"
_PACKAGE_NAME = "com.xunlei.downloadprovider"
_USER_AGENT = ("ANDROID-com.xunlei.downloadprovider/8.31.0.9726 netWorkType/5G appid/40 "
               "deviceName/Xiaomi_M2004j7ac deviceModel/M2004J7AC OSVersion/12 protocolVersion/301 "
               "platformVersion/10 sdkVersion/512000 Oauth2Client/0.9 "
               "(Linux 4_14_186-perf-gddfs8vbb238b) (JAVA 0)")
_APP_ID = "40"
_APP_KEY = "34a062aaa22f906fca4fefe9fb3a3021"
_ALGOS = ("9uJNVj/wLmdwKrJaVj/omlQ", "Oz64Lp0GigmChHMf/6TNfxx7O9PyopcczMsnf",
          "Eb+L7Ce+Ej48u", "jKY0", "ASr0zCl6v8W4aidjPK5KHd1Lq3t+vBFf41dqv5+fnOd",
          "wQlozdg6r1qxh0eRmt3QgNXOvSZO6q/GXK", "gmirk+ciAvIgA/cxUUCema47jr/YToixTT+Q6O",
          "5IiCoM9B1/788ntB", "P07JH0h6qoM6TSUAK2aL9T5s2QBVeY9JWvalf",
          "+oK0AN")
# 网页版 client_id(刷新 token 必须用它;扫码拿到的凭据里也会带一份,以凭据为准)
_WEB_CLIENT_ID = "Xqp0kJBXWhwaTpB6"


# ---------------------------------------------------------------- 签名(纯函数)

def _md5hex(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def _sign_with_timestamp(device_id: str, timestamp: str, client_id: str = _CLIENT_ID) -> str:
    """captcha 签名:对 `ClientID+ClientVersion+PackageName+DeviceID+时间戳` 依次加盐 MD5。

    ⚠️ `client_id` 参与计算,所以身份不同 → 签名不同(这是"自取 captcha 失败"的根因)。
    """
    s = client_id + _CLIENT_VERSION + _PACKAGE_NAME + device_id + timestamp
    for salt in _ALGOS:
        s = _md5hex(s + salt)
    return "1." + s


def _device_sign(device_id: str) -> str:
    """设备签名:`div101.` + DeviceID + MD5(SHA1(DeviceID+PackageName+AppID+AppKey))。"""
    sha1_hex = hashlib.sha1(
        (device_id + _PACKAGE_NAME + _APP_ID + _APP_KEY).encode("utf-8")).hexdigest()
    return "div101." + device_id + _md5hex(sha1_hex)


def _jwt_exp(access_token: str) -> float:
    """从 JWT 里读过期时间(秒)。读不到返回 0(当作已过期)。"""
    try:
        payload = access_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload)).get("exp") or 0)
    except Exception:  # noqa: BLE001
        return 0.0


def _jwt_sub(access_token: str) -> str:
    """从 JWT 里读用户 id。"""
    try:
        payload = access_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return str(json.loads(base64.urlsafe_b64decode(payload)).get("sub") or "")
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------- 凭据

def _credentials(settings=None) -> dict:
    """从 cookie_store 读迅雷凭据(加密存的 JSON)。取第一个启用用户的 —— 与 quark 一致。"""
    from sqlalchemy import select

    from app.db import get_session_local
    from app.db.models import User
    from app.services.cookie_store import get_cookie

    db = get_session_local()()
    try:
        for (uid,) in db.execute(select(User.id).where(User.enabled.is_(True))).all():
            raw = get_cookie(db, uid, "xunlei")
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            if data.get("refresh_token"):
                return data
    finally:
        db.close()
    return {}


def _refresh_access_token(refresh_token: str, client_id: str = "") -> str:
    """用 refresh_token 换 access_token(**client_id 必须是网页版那个**)。"""
    resp = requests.post(
        f"{_AUTH}/v1/auth/token",
        json={"grant_type": "refresh_token", "refresh_token": refresh_token,
              "client_id": client_id or _WEB_CLIENT_ID, "client_secret": ""},
        headers={"Content-Type": "application/json", "User-Agent": _USER_AGENT},
        timeout=_TIMEOUT)
    data = resp.json()
    token = data.get("access_token") or ""
    if not token:
        raise RuntimeError(f"迅雷刷新 token 失败:{str(data)[:180]}")
    return token


def _access_token(cred: dict) -> str:
    """取可用的 access_token:先用存着的(12h),快过期才刷。"""
    at = cred.get("access_token") or ""
    if at and _jwt_exp(at) > time.time() + 120:
        return at
    return _refresh_access_token(cred["refresh_token"], cred.get("client_id") or _WEB_CLIENT_ID)


def _headers(access_token: str, captcha: str, device_id: str,
             client_id: str = _CLIENT_ID) -> dict:
    return {"Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9",
            "Cache-Control": "no-cache", "Content-Type": "application/json",
            "Origin": "https://pan.xunlei.com", "Pragma": "no-cache",
            "Referer": "https://pan.xunlei.com/", "User-Agent": _USER_AGENT,
            "Authorization": "Bearer " + access_token, "x-captcha-token": captcha,
            "x-client-id": client_id, "x-device-id": device_id}


def _drive_headers(cred: dict) -> dict:
    """盘接口的请求头。

    ⚠️ **身份必须用凭据里那套**(扫码登录出来的是**网页版身份**:`client_id=Xqp0kJBXWhwaTpB6`),
    不能混用 android 的 `_CLIENT_ID` —— captcha 与 `client_id`/`device_id` **三者绑定**,
    混着用就是之前一直 `captcha_invalid: no client info found` 的原因。
    """
    return _headers(_access_token(cred), _captcha_token(cred),
                    cred.get("device_id") or "", cred.get("client_id") or _CLIENT_ID)


# ---------------------------------------------------------------- captcha 失效自愈

class _CaptchaExpired(RuntimeError):
    """盘接口回了 `captcha_invalid` —— 上层应**重铸一枚 captcha 再试**。"""


def _json(resp) -> dict:
    """取 JSON;若是 `captcha_invalid` 就抛 `_CaptchaExpired` 交给上层续期重试。"""
    try:
        data = resp.json()
    except ValueError:
        return {}
    if isinstance(data, dict) and data.get("error") == "captcha_invalid":
        raise _CaptchaExpired(str(data.get("error_description") or "captcha_invalid"))
    return data


def _fresh_cred(cred: dict) -> dict:
    """重跑前**重新读一遍凭据**。

    ⚠️ captcha 续期是写回库的,而 `_once()` 闭包里那份 `cred` 是**续期之前**读的快照 ——
    重试时若继续吃它,等于拿着旧 captcha 再打一次,续期就白做了(2026-10-02 实测踩过:
    verify 跑了 28 秒把浏览器都开起来了,重试仍然 captcha_invalid)。
    """
    return _credentials() or cred


def _renew_captcha() -> bool:
    """借网页版铸一枚新 captcha 并写回库(实现在 `app/services/xunlei_captcha.py`)。"""
    try:
        from app.services import xunlei_captcha

        return xunlei_captcha.refresh(force=True)
    except Exception:  # noqa: BLE001 - 续期失败不该盖住原始错误
        logger.exception("captcha 续期失败")
        return False


def _with_captcha_retry(fn):
    """跑 `fn()`;命中 `_CaptchaExpired` 就**重铸 captcha 再跑一次**。

    captcha 寿命很短(网页给它标的过期时间只有十几分钟),所以"过期 → 重铸 → 重试"必须
    是自动的,否则盘写操作动不动就要人工补。**只重试一次**:再失败就是续期本身有问题。
    """
    for attempt in (0, 1):
        try:
            return fn()
        except _CaptchaExpired:
            if attempt or not _renew_captcha():
                raise
    raise RuntimeError("unreachable")


# ---------------------------------------------------------------- captcha 自续
# ⚠️ **2026-10-02 修正一条旧结论**:此前记的是"captcha_token 自取不行",错的 —— 那次
# 是拿 **android 盐值**去算 **web client_id** 的签名。用 android 身份(我们本来就是拿
# `_CLIENT_ID` 发请求的)算签名,`/v1/shield/captcha/init` 直接 200。
# 存的那份寿命**约 1.5 小时**(标称 expires_at 还能再多撑一会儿),过期后所有盘写操作
# 报 `captcha_invalid` —— 所以这里自续,不必再让用户扫码。
_CAPTCHA_ACTION = "POST:/drive/v1/share"      # 实测 action 只校验格式,拿到的 token 全盘通用
_CAPTCHA_TTL = 25 * 60                        # 保守:25 分钟续一次(寿命的 ~1/3)
_captcha_cache: dict = {"token": "", "at": 0.0}


def _init_captcha(cred: dict, action: str = _CAPTCHA_ACTION) -> str:
    """自取一枚新的 captcha_token。失败返回空串(调用方降级用存的那份)。"""
    device_id = cred.get("device_id") or ""
    timestamp = str(int(time.time() * 1000))
    try:
        resp = requests.post(
            f"{_AUTH}/v1/shield/captcha/init",
            json={"client_id": _CLIENT_ID, "device_id": device_id, "action": action,
                  "captcha_token": "", "redirect_uri": "", "timestamp": timestamp,
                  "sign": _sign_with_timestamp(device_id, timestamp, _CLIENT_ID)},
            headers={"Content-Type": "application/json", "User-Agent": _USER_AGENT},
            timeout=_TIMEOUT)
        return (resp.json() or {}).get("captcha_token") or ""
    except Exception:  # noqa: BLE001 - 拿不到就退回过期那份,由调用方报错
        logger.exception("迅雷 captcha 自取失败")
        return ""


def _captcha_token(cred: dict) -> str:
    """给盘接口用的 captcha_token:**优先用扫码存下的那份**,没有才自取。

    ⚠️ **自取的 token 不是万能钥匙**(2026-10-02 实测):`captcha/init` 返回 200、也能
    通过 `about` / 列目录这类接口,但**`/drive/v1/share` 一律回 `captcha_invalid`
    (`detail: no client info found`)** —— 服务端要求这枚 token 绑在一个它认识的
    **客户端会话**上,而我们这套凭据是**网页版扫码**登录的(是 web 会话),自取时用的
    android 身份对不上;把签名塞进 `meta.captcha_sign` 更直接被拒
    (`invalid captcha_sign`,因为 `device_id` 不是 android 客户端注册过的那个)。
    所以**自取只当兜底**,真正可靠的是扫码那份(寿命约 1.5 小时)。
    """
    stored = cred.get("captcha_token") or ""
    if stored:
        return stored
    now = time.time()
    if _captcha_cache["token"] and now - _captcha_cache["at"] < _CAPTCHA_TTL:
        return _captcha_cache["token"]
    fresh = _init_captcha(cred)
    if fresh:
        _captcha_cache.update(token=fresh, at=now)
    return fresh


_parent_cache: dict = {"name": "", "id": "", "at": 0.0}


def resolve_parent_id(cred: dict | None = None, *, refresh: bool = False) -> str:
    """**转存落点目录 id**:按 `settings.xunlei_transfer_parent`(默认「最全文件」)在根目录里找。

    用户口径(2026-10-02):"以后都存进最全文件里面" —— 所有自动转存的资源统一落那个目录,
    不再散在根目录。

    - 按**名字**找而不是写死 id:目录改名/重建都能跟上;
    - **找不到返回 ""(根目录)并记 warning** —— 目录没了不该让整条链停摆;
    - 结果缓存 10 分钟,免得每次转存都去扫一遍目录。
    """
    from config.settings import get_settings

    st = get_settings()
    # **优先用配置的 id**(2026-10-02:名字查找不可靠 —— 「最全文件」存在却不被列表返回)
    fixed = (getattr(st, "xunlei_transfer_parent_id", "") or "").strip()
    if fixed:
        return fixed
    name = (getattr(st, "xunlei_transfer_parent", "") or "").strip()
    if not name:
        return ""
    now = time.time()
    if (not refresh and _parent_cache["name"] == name and _parent_cache["id"]
            and now - _parent_cache["at"] < 600):
        return str(_parent_cache["id"])
    for f in list_files("", cred=cred):
        if (f.get("name") or "").strip() == name and f.get("kind") == "drive#folder":
            _parent_cache.update(name=name, id=str(f.get("id") or ""), at=now)
            return str(f.get("id") or "")
    logger.warning("转存落点目录「%s」没找到,本轮落根目录", name)
    _parent_cache.update(name=name, id="", at=now)
    return ""


def verify(settings=None) -> dict:
    """探针:拿 token → 拉一次配额。返回 `{ok, message, quota}`。

    用于"凭据是否失效"的巡检,也是自测入口。
    """
    cred = _credentials(settings)
    if not cred:
        return {"ok": False, "message": "未配置迅雷凭据(需先扫码登录)"}

    def _once() -> dict:
        r = requests.get(f"{_API}/drive/v1/about", timeout=_TIMEOUT,
                         headers=_drive_headers(_fresh_cred(cred)))
        data = _json(r)
        if r.status_code != 200:
            return {"ok": False, "message": f"配额查询 {r.status_code}:{str(data)[:160]}"}
        return {"ok": True, "message": "ok", "quota": (data.get("quota") or {})}

    try:
        return _with_captcha_retry(_once)
    except _CaptchaExpired:
        return {"ok": False, "message": "captcha 失效且自动续期失败 —— 需要重新扫码登录"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": str(exc)[:200]}


# ---------------------------------------------------------------- 业务

def trash_files(file_ids: list[str], cred: dict | None = None) -> dict:
    """把文件/文件夹**移入回收站**(`DELETE /drive/v1/files/{file_id}`)。

    ⚠️ 实测踩过的坑(2026-10-02):
      - **`/drive/v1/files/trash` 不是删除接口** —— 对它 `POST` 回 501、`DELETE` 回
        `file_not_found`(那是"回收站"相关路径),白试了三轮;
      - **正解是 `DELETE /drive/v1/files/{file_id}`**(路径里带 id),成功返回 `{}`;
      - **没有批量**:得逐个删,所以这里循环。

    用途:清理"自动转存搬错的大件"。返回 `{"status", "message", "deleted", "errors"}`。
    """
    cred = cred or _credentials()
    if not cred or not file_ids:
        return {"status": "failed", "message": "没有凭据或没有文件 id", "deleted": 0, "errors": []}

    def _once() -> dict:
        headers = _drive_headers(_fresh_cred(cred))
        deleted, errors = 0, []
        for fid in file_ids:
            r = requests.delete(f"{_API}/drive/v1/files/{fid}", headers=headers, timeout=_TIMEOUT)
            data = _json(r)                  # 先解析:captcha_invalid 是 400,别漏掉自愈
            if r.status_code == 200:
                deleted += 1
            else:
                errors.append(f"{fid}: {str(data)[:100]}")
        if deleted == 0 and errors:
            return {"status": "failed", "message": errors[0][:200],
                    "deleted": 0, "errors": errors}
        return {"status": "ok", "message": f"已移入回收站 {deleted} 个",
                "deleted": deleted, "errors": errors}

    try:
        return _with_captcha_retry(_once)
    except _CaptchaExpired:
        return {"status": "failed", "message": "captcha 失效且自动续期失败 —— 需要重新扫码登录",
                "deleted": 0, "errors": []}
    except Exception as exc:  # noqa: BLE001
        logger.exception("迅雷删除失败")
        return {"status": "failed", "message": str(exc)[:200], "deleted": 0, "errors": []}


def quota_info(cred: dict | None = None) -> dict:
    """盘配额 `{"usage", "limit", "ratio"}`(字节;拿不到就是空 dict)。

    转存前的**盘级闸门**要用它:2026-10-02 实测翻过车 —— 群里的大包被自动搬进盘,
    直接把空间顶到 126%,之后所有转存都 `file_space_not_enough`。

    ⚠️ **探针失败必须降级成"不知道",不能冒泡** —— 这是闸门的判据,它自己抖一下就把
    整条转存链带崩,比不判还糟。所以**连取凭据都包在 try 里**。
    """
    try:
        cred = cred or _credentials()
        if not cred:
            return {}

        def _once() -> dict:
            r = requests.get(f"{_API}/drive/v1/about", timeout=_TIMEOUT,
                             headers=_drive_headers(_fresh_cred(cred)))
            # ⚠️ **先解析再判状态码**:`captcha_invalid` 是 400 返回的,若写成
            # "200 才解析",异常就永远不会抛、自愈也就永远不会触发(2026-10-02 实测踩过)
            data = _json(r)
            return data if r.status_code == 200 else {}

        # ⚠️ 配额接口**也要 captcha** —— 必须走自愈重试,否则 captcha 一过期,闸门就
        # "什么都不知道"而放行(2026-10-02 实测:闸门刚上线就因为这个漏了一次,
        # 3 条被无条件搬、撞 `file_space_not_enough`)
        quota = (_with_captcha_retry(_once).get("quota") or {})
        limit, usage = int(quota.get("limit") or 0), int(quota.get("usage") or 0)
        if not limit:
            return {}
        return {"usage": usage, "limit": limit, "ratio": usage / limit}
    except Exception:  # noqa: BLE001 - 探针:拿不到就说不知道
        logger.exception("迅雷配额查询失败")
        return {}


def quota_ratio(cred: dict | None = None) -> float | None:
    """盘的使用率(`usage/limit`)。**拿不到返回 None**(调用方按"不知道"处理,别误判成满)。"""
    return quota_info(cred).get("ratio")


_SPACE_ERROR_HINTS = ("file_space_not_enough", "空间不足", "file_space")
_OWN_SHARE_HINTS = ("file_restore_own", "转存自己的文件")


def is_space_error(message: str) -> bool:
    """这条失败是不是"**盘没空间**"。

    盘满不是"这条资源的问题",是**盘的问题** —— 它必须**可重试**(清出空间后接着搬),
    不能像"分享已失效"那样标终态。闸门已经会挡,但闸门靠配额探针,而探针本身也要
    captcha、会失效(2026-10-02 实测漏过一次:3 条被无条件搬、撞空间不足还标了终态),
    所以这里做**第二层**兜底。
    """
    text = message or ""
    return any(h in text for h in _SPACE_ERROR_HINTS)


def is_own_share_error(message: str) -> bool:
    """这条失败是不是"**这是我们自己发的分享**"(`file_restore_own`)。

    群里会转我们自己发出去的分享(2026-10-02 实测:「最全文件」就是),这种**永远不可能成功**
    —— 重试也是白试,**该直接标终态**,不该当普通失败留着重试。
    """
    text = message or ""
    return any(h in text for h in _OWN_SHARE_HINTS)


_DEAD_SHARE_HINTS = ("get_share_user_banned", "share_overdue", "share_cancelled",
                     "sensitive_resource", "分享已失效", "分享已取消", "分享已过期")


def is_dead_share_error(message: str) -> bool:
    """这条失败是不是"**分享本身已经死了**"(分享者被封 / 过期 / 被取消 / 敏感资源)。

    实测(2026-10-02):群里大量条目是**被封号的人发的分享**(`get_share_user_banned`)
    —— 它们**永远转不了**,反复当 `failed` 留痕只是噪音,该直接标终态。
    """
    text = message or ""
    return any(h in text for h in _DEAD_SHARE_HINTS)



def _extract_share_id(url: str) -> tuple[str, str]:
    """`pan.xunlei.com/s/<id>?pwd=xxxx` → (share_id, pass_code)。"""
    u = urlparse(url.strip())
    parts = [p for p in u.path.split("/") if p]
    share_id = ""
    if len(parts) >= 2 and parts[0] == "s":
        share_id = parts[1]
    elif parts:
        share_id = parts[-1]
    pwd = (parse_qs(u.query).get("pwd") or [""])[0]
    return share_id, pwd


def share_files(file_ids: list[str], expiration_days: str = "7",
                cred: dict | None = None) -> dict:
    """把盘里已有的文件**生成我方分享链**。

    与 `transfer_and_share` 的区别:那个是"从别人的分享转存进来",这个是"盘里已经有了,
    给它开个分享" —— 扫盘登记(`xunlei_sync`)走这条。

    返回 `{"status", "message", "share_url", "code"}`。
    """
    cred = cred or _credentials()
    if not cred:
        return {"status": "failed", "message": "未配置迅雷凭据(需先扫码登录)"}
    if not file_ids:
        return {"status": "failed", "message": "没有文件 id"}
    try:
        def _once() -> dict:
            h = _drive_headers(_fresh_cred(cred))
            share = _json(requests.post(f"{_API}/drive/v1/share", headers=h, timeout=_TIMEOUT,
                                        json={"file_ids": file_ids, "share_to": "copy",
                                              "params": {"subscribe_push": "false",
                                                         "WithPassCodeInLink": "true"},
                                              "title": "云盘资源分享", "restore_limit": "-1",
                                              "expiration_days": expiration_days}))
            if not share.get("share_url"):
                return {"status": "failed", "message": f"生成分享失败:{str(share)[:160]}"}
            return {"status": "ok", "message": "ok",
                    "share_url": share["share_url"] + "?pwd=" + (share.get("pass_code") or ""),
                    "code": share.get("pass_code") or "",
                    "share_id": share.get("share_id") or ""}

        return _with_captcha_retry(_once)
    except _CaptchaExpired:
        return {"status": "failed", "message": "captcha 失效且自动续期失败 —— 需要重新扫码登录"}
    except Exception as exc:  # noqa: BLE001
        logger.exception("迅雷生成分享失败")
        return {"status": "failed", "message": str(exc)[:200]}


def list_files(parent_id: str = "", cred: dict | None = None, limit: int = 200,
               include_trashed: bool = False) -> list[dict]:
    """列某个目录下的文件/文件夹(扫盘用)。失败返回空表。

    ⚠️ **必须过滤 `trashed`**(2026-10-02 实测踩过):接口**默认把回收站里的条目一起返回**
    —— 删掉一个文件夹后,它**整棵子树**(实测 2000+ 项)都会带着 `trashed=true` 混在列表里。
    不过滤的话,扫盘作业会把**已删除的文件当成新资源**登记、还去给它建分享链(必失败)。
    """
    cred = cred or _credentials()
    if not cred:
        return []

    def _once() -> list[dict]:
        r = requests.get(f"{_API}/drive/v1/files", headers=_drive_headers(_fresh_cred(cred)),
                         timeout=_TIMEOUT,
                         params={"limit": str(limit), "parent_id": parent_id,
                                 "with_audit": "true"})
        data = _json(r)                       # 先解析:captcha_invalid 是 400,别漏掉自愈
        if r.status_code != 200:
            return []
        files = data.get("files") or []
        return files if include_trashed else [f for f in files if not f.get("trashed")]

    try:
        return _with_captcha_retry(_once)
    except Exception:  # noqa: BLE001 - 探针类调用,失败即空(含续期失败)
        logger.exception("迅雷列目录失败")
        return []


def transfer_and_share(share_url: str, parent_id: str = "", settings=None) -> dict:
    """`pan.xunlei.com/s/xxx` → 转存到我方盘 → 生成我方分享链。

    返回结构与 `quark_transfer.transfer_and_share` 对齐:
    `{"status": "ok"|"failed", "message", "share_url", "code", "fid"}`。
    """
    cred = _credentials(settings)
    if not cred:
        return {"status": "failed", "message": "未配置迅雷凭据(需先扫码登录)"}
    share_id, pass_code = _extract_share_id(share_url)
    if not share_id:
        return {"status": "failed", "message": f"解析不出 share_id:{share_url[:80]}"}
    # 用户口径:转存统一落到「最全文件」下(可在 settings.xunlei_transfer_parent 改);
    # 找不到那个目录就落根目录,不影响转存本身
    parent_id = parent_id or resolve_parent_id(cred)
    def _once() -> dict:
        h = _drive_headers(_fresh_cred(cred))

        detail = _json(requests.get(f"{_API}/drive/v1/share", headers=h, timeout=_TIMEOUT,
                                    params={"share_id": share_id, "pass_code": pass_code,
                                            "limit": "100", "pass_code_token": "",
                                            "page_token": "",
                                            "thumbnail_size": "SIZE_SMALL"}))
        if detail.get("share_status") != "OK":
            return {"status": "failed", "message": f"分享状态异常:{str(detail)[:160]}"}
        files = [f.get("id") for f in (detail.get("files") or []) if f.get("id")]
        if not files:
            return {"status": "failed", "message": "分享里没有文件"}

        restore = _json(requests.post(f"{_API}/drive/v1/share/restore", headers=h,
                                      timeout=_TIMEOUT,
                                      json={"parent_id": parent_id, "share_id": share_id,
                                            "pass_code_token": detail.get("pass_code_token") or "",
                                            "ancestor_ids": [], "specify_parent_id": True,
                                            "file_ids": files}))
        task_id = restore.get("restore_task_id") or ""
        if not task_id:
            return {"status": "failed", "message": f"转存失败:{str(restore)[:160]}"}

        task: dict = {}
        for _ in range(50):                       # 最多 50 次 × 2 秒
            task = _json(requests.get(f"{_API}/drive/v1/tasks/{task_id}", headers=h,
                                      timeout=_TIMEOUT))
            if int(task.get("progress") or -1) == 100:
                break
            time.sleep(2)
        file_ids = _trace_file_ids(task)
        if not file_ids:
            # ⚠️ 这里**不能**兜底用源分享的 file_ids:那些 id 不在我方盘里,
            # 建分享必 `file_not_found`(2026-10-02 实测踩过)。宁可明确失败。
            return {"status": "failed",
                    "message": f"转存任务未返回文件 id(progress={task.get('progress')})"}

        share = _json(requests.post(f"{_API}/drive/v1/share", headers=h, timeout=_TIMEOUT,
                                    json={"file_ids": file_ids, "share_to": "copy",
                                          "params": {"subscribe_push": "false",
                                                     "WithPassCodeInLink": "true"},
                                          "title": "云盘资源分享", "restore_limit": "-1",
                                          "expiration_days": "7"}))
        if not share.get("share_url"):
            return {"status": "failed", "message": f"生成分享失败:{str(share)[:160]}"}
        return {"status": "ok", "message": "转存并分享成功",
                "share_url": share["share_url"] + "?pwd=" + (share.get("pass_code") or ""),
                "code": share.get("pass_code") or "", "fid": ",".join(file_ids)}

    try:
        return _with_captcha_retry(_once)
    except _CaptchaExpired:
        return {"status": "failed", "message": "captcha 失效且自动续期失败 —— 需要重新扫码登录"}
    except Exception as exc:  # noqa: BLE001 - 对外只返回结构化失败,不抛
        logger.exception("迅雷转存失败")
        return {"status": "failed", "message": str(exc)[:200]}


def _trace_file_ids(task: dict) -> list[str]:
    """从转存任务里取**我方盘里**新文件 id(urldb 兼容多种返回格式,这里同样兜住)。

    ⚠️ **实测(2026-10-02,修复一个把整条链卡死的 bug)**:真实字段在
    `task["params"]["trace_file_ids"]`(**不在顶层**),而且值是
    **JSON 字符串包着的 dict**:`{"<源文件id>": "<我方新文件id>"}` —— 要取 **values**。
    原实现只读顶层、只认 list/dict-as-list → 恒取空 → 调用方兜底用**源分享的 id**
    去建分享 → `file_not_found`(转存明明成功,却卡在最后一步)。
    """
    raw = (task.get("params") or {}).get("trace_file_ids") or task.get("trace_file_ids")
    if isinstance(raw, str) and raw:
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = [raw]
    if isinstance(raw, dict):                      # 源 id → 我方 id,取 value
        return [str(v) for v in raw.values() if v]
    if isinstance(raw, list):
        return [str(v) for v in raw if v]
    for key in ("file_ids",):                      # 老格式兜底
        val = task.get(key)
        if isinstance(val, list):
            return [str(v) for v in val if v]
    return [str(task["file_id"])] if task.get("file_id") else []
