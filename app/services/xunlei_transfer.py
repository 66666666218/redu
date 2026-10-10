"""迅雷网盘转存 + 分享(2026-10-02)。

**移植自** [ctwj/urldb](https://github.com/ctwj/urldb) 的 `common/xunlei_pan.go` +
`common/xunlei_login.go`(Go)。它把迅雷那套签名完整复刻出来了 —— 关键结论是
**签名是纯函数**(MD5 迭代盐值),不是真机签名,所以 Python 能 1:1 复刻。

**本项目实际走的凭据路线**(2026-10-02 实测,比 urldb 更省事):
  1. **扫码登录一次**(`scripts/xunlei_login.py`)→ 从网页版 localStorage 抽
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


def _save_credentials(patch: dict) -> None:
    """把刷新出来的新凭据**写回加密存储**(与 `xunlei_captcha` 同一套)。

    ⚠️ **必须写回,尤其是 `refresh_token`**(2026-10-05 实测踩到):
    迅雷的 token 端点**会轮换 refresh_token** —— 兑过一次,旧的即失效。
    不写回的话**下一次刷新就 `invalid_grant (4126)`**,而症状是
    **"刷新完 20 分钟后整条迅雷链突然全挂"**(今晚 00:20 刷新、00:40 就开始失败)。

    ⚠️ 项目里 `xunlei_captcha` **早就知道这个坑**(它 docstring 写着
    "那次兑换会轮换 refresh_token,而我们不一定接得住,2026-10-02 踩过")——
    但**直接调 token 端点这条捷径没处理**,于是又踩了一次。
    """
    import json as _json

    from sqlalchemy import select

    from app.db import get_session_local
    from app.db.models import User
    from app.services import cookie_store

    db = get_session_local()()
    try:
        for (uid,) in db.execute(select(User.id).where(User.enabled.is_(True))).all():
            raw = cookie_store.get_cookie(db, uid, "xunlei")
            if not raw:
                continue
            try:
                cur = _json.loads(raw)
            except ValueError:
                continue
            if not cur.get("refresh_token"):
                continue
            cur.update({k: v for k, v in patch.items() if v})   # 只覆盖非空值
            cookie_store.set_cookie(db, uid, "xunlei", _json.dumps(cur, ensure_ascii=False))
            return
    finally:
        db.close()


def iso_expires_at(epoch: float | None = None, *, seconds_from_now: int | None = None) -> str:
    """迅雷**网页 localStorage** 里 `expires_at` 的格式:ISO-8601 + 毫秒 + `Z`。

    ⚠️ **必须是这个格式**(2026-10-05 定位到的"每天得重扫一次"根因):
    `xunlei_captcha` 借网页铸 captcha 时,要把一个"新鲜的" `expires_at` 注入页面,
    好让网页**认为 token 还没过期、不要去兑换** —— 因为那次兑换会**轮换 refresh_token**。
    它原来注入的是**整数 epoch**(`int(_jwt_exp(...))`),而页面用的是 ISO 串
    (实测库里读回的是 `2026-10-05T07:22:15.275Z`)。**格式对不上 ⇒ 页面照样去兑 ⇒
    refresh_token 被换掉而我们接不住 ⇒ 直连刷新 `invalid_grant` ⇒ 只能重扫。**
    实测时间线:15:20:41 captcha 续期 → **15:30 就 invalid_grant**;
    而 access_token 寿命实测 12 小时(JWT 看出来的)⇒ **一天一扫**。

    ⚠️ **注入点与写回点必须用同一个函数** —— 两处各写各的,迟早再飘一次
    (这正是这次踩的坑:同一个字段两种格式)。
    """
    from datetime import datetime, timezone

    if epoch is None:
        epoch = time.time() + int(seconds_from_now or 0)
    return (datetime.fromtimestamp(float(epoch), timezone.utc)
            .isoformat(timespec="milliseconds").replace("+00:00", "Z"))


def _refresh_access_token(refresh_token: str, client_id: str = "") -> str:
    """用 refresh_token 换 access_token(**client_id 必须是网页版那个**)。

    ⚠️ **顺手把轮换后的 `refresh_token` 写回**(2026-10-05 修):
    这个端点回的新 `refresh_token` 覆盖旧的,不存回去下次就 `invalid_grant`。
    """
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
    # 🔑 **轮换后的 refresh_token 必须落库** —— 否则下一次刷新就 invalid_grant。
    # 注意:新值可能与旧值**相同**(服务端未必每次都轮换),所以有就写、没有就算了。
    new_rt = data.get("refresh_token") or ""
    try:
        # ⚠️ `expires_at` 要写**真正的到期时刻(ISO)**,不是 `expires_in` 那个秒数。
        # 原写法 `data.get("expires_in")` 会把 `43200` 塞进 `expires_at` —— 字段名与内容不符,
        # 而且和 `xunlei_captcha` 写回的那种 ISO 串**格式打架**(同一个字段两种格式)。
        patch: dict = {"access_token": token}
        try:
            patch["expires_at"] = iso_expires_at(
                seconds_from_now=int(data.get("expires_in") or 43200))
        except (TypeError, ValueError):
            logger.debug("expires_in 解析失败,跳过 expires_at")
        if new_rt and new_rt != refresh_token:
            patch["refresh_token"] = new_rt
            logger.info("迅雷 refresh_token 已轮换,正在写回")
        _save_credentials(patch)
    except Exception:  # noqa: BLE001 - 写回失败不该把这次刷新作废(本次 token 仍可用)
        logger.exception("迅雷凭据写回失败(本次 token 仍可用)")
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


def move_files(file_ids: list[str], parent_id: str, cred: dict | None = None) -> dict:
    """把文件/文件夹**移到**指定目录(`POST /drive/v1/files/{file_id}/move`)。

    ⚠️ **形状踩坑记**(2026-10-02,试了 6 种才中):body 是**嵌套消息**
    `{"to": {"parent_id": "<目标目录 id>"}}` ——
      - 传字符串(`{"to": "xxx"}`)→ protobuf 解析器直接拒(`proto: syntax error`);
      - 传 `{"to": {"id": "xxx"}}` → 报 `file_move_or_copy_to_cur`(它把 id 当成了"当前目录")。
    返回 `{"status", "moved", "errors"}`;移动是**异步任务**(接口只回 task_id),
    所以调用方要稍后自查落点,别指望这一行就到位。
    """
    cred = cred or _credentials()
    if not cred or not file_ids or not parent_id:
        return {"status": "failed", "message": "缺凭据/文件 id/目标目录", "moved": 0, "errors": []}

    def _once() -> dict:
        headers = _drive_headers(_fresh_cred(cred))
        moved, errors = 0, []
        for fid in file_ids:
            r = requests.post(f"{_API}/drive/v1/files/{fid}/move", headers=headers,
                              timeout=_TIMEOUT, json={"to": {"parent_id": parent_id}})
            data = _json(r)                  # 先解析:captcha_invalid 是 400,别漏掉自愈
            if r.status_code == 200:
                moved += 1
            else:
                errors.append(f"{fid}: {str(data)[:100]}")
        if moved == 0 and errors:
            return {"status": "failed", "message": errors[0][:200], "moved": 0, "errors": errors}
        return {"status": "ok", "message": f"已提交移动 {moved} 个",
                "moved": moved, "errors": errors}

    try:
        return _with_captcha_retry(_once)
    except _CaptchaExpired:
        return {"status": "failed", "message": "captcha 失效且自动续期失败 —— 需要重新扫码登录",
                "moved": 0, "errors": []}
    except Exception as exc:  # noqa: BLE001
        logger.exception("迅雷移动失败")
        return {"status": "failed", "message": str(exc)[:200], "moved": 0, "errors": []}


# ⚠️ **配额探针必须限流**(2026-10-04):`/drive/v1/about` **要 captcha**、会走自愈重试,
# 打太密既费 captcha 额度也吃账号级限流。而配额是**粗判据**(只用来比 0.9 / 算剩余空间),
# 秒级陈旧完全无害。所以进程内共享一份、`_QUOTA_TTL` 秒内复用。
#
# 为什么需要这样做:`transfer_pending` 开头**已经**查了一次(注释写着"只查一次,整批共用,
# 别每条都打一次配额"),但"按剩余空间预判单个资源"这件事天然发生在**每一条**转存里 ——
# 若每次都各自探一次,一批 20 条就是 21 次探针(≈ 把那条纪律反过来做)。缓存让整批共享一次。
_QUOTA_TTL = 60.0
_quota_cache: dict = {"at": 0.0, "probed": False, "info": {}}


def reset_quota_cache() -> None:
    """丢掉缓存的配额。测试要"冷的探针"时显式调用(生产不需要)。"""
    _quota_cache.update({"at": 0.0, "probed": False, "info": {}})


def quota_info(cred: dict | None = None, fresh: bool = False) -> dict:
    """盘配额 `{"usage", "limit", "ratio"}`(字节;拿不到就是空 dict)。

    转存前的**盘级闸门**要用它:2026-10-02 实测翻过车 —— 群里的大包被自动搬进盘,
    直接把空间顶到 126%,之后所有转存都 `file_space_not_enough`。

    ⚠️ **探针失败必须降级成"不知道",不能冒泡** —— 这是闸门的判据,它自己抖一下就把
    整条转存链带崩,比不判还糟。所以**连取凭据都包在 try 里**。

    ⚠️ **有 `_QUOTA_TTL` 秒的进程内缓存**(含失败结果):失败也缓存,是为了别让一次
    captcha 失效在**同一条链里被反复撞**(那正是把 captcha 打爆的写法)。代价是恢复慢 ≤ TTL。

    `fresh=True` = **绕过缓存真打一次**(顺手刷新缓存)。给**人按的那个按钮**用 ——
    用户刚清完盘点"刷新盘况",给他 60 秒前的旧数会让人以为"清理没生效"。
    调度链里**别传它** —— 那里要的正是"整批共用一次"。
    """
    now = time.monotonic()
    if (not fresh and _quota_cache["probed"]
            and (now - _quota_cache["at"]) < _QUOTA_TTL):
        return _quota_cache["info"]
    info = _probe_quota(cred)
    _quota_cache.update({"at": now, "probed": True, "info": info})
    return info


def _probe_quota(cred: dict | None = None) -> dict:
    """真去打一次探针(不读缓存)。"""
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


def human_bytes(n: int | float | None, ref: int | float | None = None) -> str:
    """字节数 → 人话(`6.88 TiB`)。**拿不到返回 `?`**(不编数)。

    `ref` = **跟着它选单位**。缺口单独算时很有用:需要 6.88 TiB / 剩 6.04 TiB,
    缺口若自动落到 `861.88 GiB` 就**换了单位**,读的人要自己换算 ——
    传 `ref=需要的那个数` 就得到同一量纲的 `0.84 TiB`。
    """
    try:
        v = float(n)
    except (TypeError, ValueError):
        return "?"
    if v < 0:
        return "?"
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    if ref is not None:
        try:
            r, i = abs(float(ref)), 0
            while r >= 1024 and i < len(units) - 1:
                r /= 1024
                i += 1
            return f"{v / 1024 ** i:.2f} {units[i]}"
        except (TypeError, ValueError):
            pass
    for unit in units:
        if v < 1024:
            return f"{v:.2f} {unit}"
        v /= 1024
    return f"{v:.2f} PiB"


def free_bytes(cred: dict | None = None) -> int | None:
    """盘上还剩多少字节 = `limit - usage`。**拿不到返回 None**(按"不知道"处理)。"""
    info = quota_info(cred)
    try:
        return int(info["limit"]) - int(info["usage"])
    except (KeyError, TypeError, ValueError):
        return None


def required_size_from_error(resp: dict) -> int | None:
    """从转存失败的响应里取出迅雷**自己报**的所需空间(`DRIVE_SPACE_NOT_ENOUGH`)。

    实测报文:`{"details":[{"type":"DRIVE_SPACE_NOT_ENOUGH",
    "data":{"required_size":"7563939414460"}}]}` —— ⚠️ `required_size` 是**字符串**。

    它比"使用率闸门"准得多:能直接回答用户问的那个问题 ——
    **"是盘满了,还是这一个包比剩下的空间还大?"**(2026-10-04 实测:使用率才 **79.9%**,
    盘上还剩 **6.04 TiB**,而这个包要 **6.88 TiB**)。拿不到返回 None(**不知道 ≠ 满**)。
    """
    for d in (resp.get("details") or []):
        if not isinstance(d, dict) or d.get("type") != "DRIVE_SPACE_NOT_ENOUGH":
            continue
        try:
            return int((d.get("data") or {}).get("required_size"))
        except (TypeError, ValueError):
            return None
    return None


def share_required_bytes(detail: dict) -> int | None:
    """分享详情里各文件 `size` 之和 = **预计要占多少**;全为 0 或拿不到时返回 None。

    ⚠️ **只能当"下限"用**:迅雷对**文件夹**恒报 `size=0`(2026-10-04 实测),
    所以含文件夹的分享会被**严重低估**。因此这里**只允许用于"确定装不下"的提前放行**
    (低估 ⇒ 不会误挡),真实判据仍然是转存失败时迅雷自己给的 `required_size`。
    """
    total = 0
    for f in (detail.get("files") or []):
        if isinstance(f, dict):
            try:
                total += int(f.get("size") or 0)
            except (TypeError, ValueError):
                continue
    return total or None


def space_insufficient(out: dict) -> bool:
    """这条失败是不是"**单个资源比剩余空间大**"(而不是"盘的使用率到阈值了")。

    两者的运维动作完全不同:前者只要**不搬这一个**、让链路照推;后者才要清盘/扩容。
    2026-10-04 用户正是据此纠正了"盘满"的结论 —— 盘还有 6.04 TiB。
    """
    return (out or {}).get("code") == "space_insufficient"


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


class XunleiDriveError(RuntimeError):
    """迅雷网盘接口**硬失败**(HTTP 非 200 / 网络异常 / 续期失败)。

    ⚠️ **为什么必须与"目录是空的"分开**(2026-10-03 全项目审查):此前 `list_files` 把所有
    失败吞成 `[]`,于是 `xunlei_sync` 把"**凭据失效/网络断了**"当成"**盘里没有资源**",
    **每天记 `success(扫0 新0)`** —— 资源静默丢失且无人知。这是本项目已修 4 次的
    静默失败(闲鱼/知乎/MediaCrawler/这里)同型问题的第 5 例。
    **判据**:空目录是 `HTTP 200 + files: []`(返回空表);其余一律是硬失败(抛本异常)。
    """


def list_files(parent_id: str = "", cred: dict | None = None, limit: int = 200,
               include_trashed: bool = False) -> list[dict]:
    """列某个目录下的文件/文件夹(扫盘用)。**硬失败抛 `XunleiDriveError`**,不再返回空表。

    ⚠️ **必须过滤 `trashed`**(2026-10-02 实测踩过):接口**默认把回收站里的条目一起返回**
    —— 删掉一个文件夹后,它**整棵子树**(实测 2000+ 项)都会带着 `trashed=true` 混在列表里。
    不过滤的话,扫盘作业会把**已删除的文件当成新资源**登记、还去给它建分享链(必失败)。

    ⚠️ **没配凭据时仍返回 `[]`**(调用方 `xunlei_sync` 会先查 `_credentials()` 再决定;
    把它也变成抛错会让"没配迅雷"这种正常部署形态变成硬报错)。
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
            # 空目录是 200 + files:[](下面正常返回空表);走到这里就是**硬失败** ——
            # 401/403 凭据失效、5xx 上游故障,统统不能装作"目录是空的"。
            raise XunleiDriveError(f"HTTP {r.status_code}: {str(data)[:120]}")
        files = data.get("files") or []
        return files if include_trashed else [f for f in files if not f.get("trashed")]

    try:
        return _with_captcha_retry(_once)
    except XunleiDriveError:
        raise
    except Exception as exc:  # noqa: BLE001 - 包成自己的异常类型,好让调用方区分
        logger.exception("迅雷列目录失败")
        raise XunleiDriveError(f"列目录失败:{type(exc).__name__}: {str(exc)[:120]}") from exc


def list_all_files(parent_id: str = "", cred: dict | None = None, page_size: int = 200,
                   max_pages: int = 500, include_trashed: bool = False) -> list[dict]:
    """**翻页**列完一个目录 —— 跟着响应里的 `next_page_token` 一直走到没有为止。

    ⚠️⚠️ **为什么必须有它**(2026-10-07 实测):原来的 `list_files` **只发一页请求**,
    而响应里**明明带着 `next_page_token`** 却从来没人用 ⇒
    **任何条目数超过 `limit` 的目录,后面的内容我们永远看不到**。
    原始响应里 `limit=200` 就真给 200 条 + 一个 next_page_token —— 这是**实打实的截断**。

    ⚠️ **但我一度把它当成了"盘上 24 TiB、本地索引只有 8 行"的根因,那个说法不成立**:
    实测根目录**活条目只有 3~5 条**(原始列表里 197/200 是 `trashed` 的回收站条目),
    内容都在**深层子目录**里(「最全文件」37 项、「7️⃣9️⃣资源✨」29 项…)。
    ⇒ 那 8 行更可能是**扫描深度上限 + 只登记"资源包"**造成的,与分页无关。
    分页该修(它确实会截断),但**它不是那件事的解释** —— 别再照着这个去查。

    `max_pages` 是**防跑飞**的上限(默认 500 页 × 200 = 10 万条/目录);
    真撞上上限会记一条 warning —— **别让"没列完"和"列完了"长得一样**(本仓的母题)。
    """
    cred = cred or _credentials()
    if not cred:
        return []
    out: list[dict] = []
    token = ""
    for page in range(max_pages):
        def _once(_t=token) -> dict:
            p = {"limit": str(page_size), "parent_id": parent_id, "with_audit": "true"}
            if _t:
                p["page_token"] = _t
            r = requests.get(f"{_API}/drive/v1/files",
                             headers=_drive_headers(_fresh_cred(cred)), timeout=_TIMEOUT, params=p)
            data = _json(r)
            if r.status_code != 200:
                raise XunleiDriveError(f"HTTP {r.status_code}: {str(data)[:120]}")
            return data
        try:
            data = _with_captcha_retry(_once)
        except XunleiDriveError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("迅雷翻页列目录失败")
            raise XunleiDriveError(f"列目录失败:{type(exc).__name__}: {str(exc)[:120]}") from exc
        files = data.get("files") or []
        out.extend(files if include_trashed else [f for f in files if not f.get("trashed")])
        token = str(data.get("next_page_token") or "")
        if not token or not files:
            return out
    logger.warning("迅雷列目录达到翻页上限 %d 页(parent_id=%s)—— **可能没列完**",
                   max_pages, parent_id or "/")
    return out


def _kinds_of(h: dict, ids: list[str], parent_id: str) -> dict[str, bool]:
    """id → **是不是目录**(列一次父目录建表即可,不必逐个问)。"""
    try:
        items = _json(requests.get(f"{_API}/drive/v1/files", headers=h, timeout=_TIMEOUT,
                                   params={"parent_id": parent_id or "", "limit": "200"}))
    except Exception:                                # noqa: BLE001 - 建不出表就当作"都不是目录"
        return {}
    return {str(x["id"]): str(x.get("kind")) == "drive#folder"
            for x in (items.get("files") or []) if x.get("id")}


def _batch_copy(h: dict, ids: list[str], to_parent: str) -> None:
    """把 `ids` 复制进 `to_parent`(**盘内复制**)。

    ★ 形状是**从迅雷自己的网页 JS 里挖出来的**(2026-10-10):
    `{"ids": […], "to": {"parent_id": …}}` —— 我先试的 `/{id}/copy` 是**错路由**。
    """
    r = requests.post(f"{_API}/drive/v1/files:batchCopy", headers=h,
                      json={"ids": ids, "to": {"parent_id": to_parent, "space": ""},
                            "space": ""}, timeout=_TIMEOUT)
    d = _json(r)
    if not d.get("task_id") and d.get("error"):
        raise RuntimeError(str(d.get("error_description") or d)[:120])


def _intro_file_ids(h: dict, intro_dir: str, *fallbacks: str) -> list[str]:
    """找 `intro_dir` 这个目录、列出里面的**文件** id(目录不列)。

    先在 `fallbacks` 给的父目录里找,再在**根目录**找 —— 简介目录通常就在根上。
    ⚠️ 找不到就返回空,**不抛**(简介拿不到不该毁掉整次转存)。
    """
    want = intro_dir.strip().strip("/")
    if not want:
        return []
    for pid in (*fallbacks, ""):
        try:
            top = _json(requests.get(f"{_API}/drive/v1/files", headers=h, timeout=_TIMEOUT,
                                     params={"parent_id": pid or "", "limit": "200"}))
        except Exception:                            # noqa: BLE001 - 某个父目录列不动就换下一个
            continue
        for x in (top.get("files") or []):
            if str(x.get("kind")) != "drive#folder":
                continue
            if str(x.get("name") or "").strip() != want:
                continue
            try:
                inner = _json(requests.get(f"{_API}/drive/v1/files", headers=h, timeout=_TIMEOUT,
                                           params={"parent_id": str(x.get("id")), "limit": "200"}))
            except Exception:                        # noqa: BLE001
                return []
            return [str(y["id"]) for y in (inner.get("files") or [])
                    if str(y.get("kind")) != "drive#folder" and y.get("id")]
    return []


def transfer_and_share(share_url: str, parent_id: str = "", settings=None,
                       intro_dir: str | None = None) -> dict:
    """`pan.xunlei.com/s/xxx` → 转存到我方盘 → 生成我方分享链。

    返回结构与 `quark_transfer.transfer_and_share` 对齐:
    `{"status": "ok"|"failed", "message", "share_url", "code", "fid"}`。

    `intro_dir` = **宣传简介所在目录名**(如 `监控简介`)。
    ⚠️ **留 `None` = 自己从 `settings.pan_intro_xunlei_dir` 读**(没配就不做)—— 这样
    三个调用点**一个都不用改**,免得每处都抄一遍 `getattr(settings, …)`。
    传空串 = 明确不做。
    ⚠️ 简介是**并进分享清单**(`file_ids` 里加一个 id),**不做复制** ——
    与夸克/百度同一条纪律:**必须在建链这一步就带上,建完再补是补不进去的**。
    """
    if intro_dir is None:
        try:
            from config.settings import get_settings

            intro_dir = (getattr(settings or get_settings(), "pan_intro_xunlei_dir", "") or "")
        except Exception:                            # noqa: BLE001 - 读不到配置就当作不做
            intro_dir = ""
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

        # 🔎 **超出剩余空间的预判**(2026-10-04):别等迅雷挡回来才知道搬不下 ——
        # 分享详情的 `size` 之和若**已经**大于剩余空间,直接不试。
        # ⚠️ 文件夹恒报 size=0 ⇒ 这个和是**下限**,只会漏报不会误报(见 `share_required_bytes`)。
        need = share_required_bytes(detail)
        free = free_bytes(cred) if need else None
        if need and free is not None and need > free:
            return {"status": "failed", "code": "space_insufficient",
                    "required_size": need, "free_size": free,
                    "message": (f"空间不足:这个包至少 {human_bytes(need)},"
                                f"盘上只剩 {human_bytes(free)},还差 {human_bytes(need - free, ref=need)}"
                                f"(单个资源比剩余空间大 —— 与「盘满」不是一回事)")}

        restore = _json(requests.post(f"{_API}/drive/v1/share/restore", headers=h,
                                      timeout=_TIMEOUT,
                                      json={"parent_id": parent_id, "share_id": share_id,
                                            "pass_code_token": detail.get("pass_code_token") or "",
                                            "ancestor_ids": [], "specify_parent_id": True,
                                            "file_ids": files}))
        task_id = restore.get("restore_task_id") or ""
        if not task_id:
            # 🔎 迅雷**自己报**的所需空间 —— 比"使用率闸门"准得多,用来把"盘满"拆成
            #    「按设计挡下」还是「单个资源就是比剩余空间大」(2026-10-04 用户口径)。
            need = required_size_from_error(restore)
            if need is not None:
                free = free_bytes(cred)
                short = (f",还差 {human_bytes(need - free, ref=need)}"
                         if free is not None and need > free else "")
                return {"status": "failed", "code": "space_insufficient",
                        "required_size": need, "free_size": free,
                        "message": (f"空间不足:这个包需要 {human_bytes(need)},"
                                    f"盘上只剩 {human_bytes(free) if free is not None else '未知'}"
                                    f"{short}(单个资源比剩余空间大 —— 与「盘满」不是一回事)")}
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

        # ★★★ 宣传简介(2026-10-10)。**优先"塞进资源目录里面"**(与夸克同规则):
        #   资源是**目录** ⇒ `batchCopy` 把简介复制进去,然后**只分享那些目录** ——
        #   对方整包保存时自动带走,而且不会在他盘里平铺出一个 `简介.doc`。
        #   ⚠️ 代价:**每个资源目录会真的多存一份**(实测约 2.9 MB)。
        #   资源是**散文件** ⇒ 无处可塞 ⇒ 退回"并进分享清单"(并排顶层,不占空间)。
        if intro_dir:
            try:
                intro_ids = _intro_file_ids(h, intro_dir, parent_id)
                if not intro_ids:
                    logger.warning("宣传简介目录没找到或是空的,本轮跳过:%s", intro_dir)
                else:
                    kinds = _kinds_of(h, file_ids, parent_id)
                    dirs = [i for i in file_ids if kinds.get(i)]
                    if dirs and len(dirs) == len(file_ids):
                        copied = 0
                        for d in dirs:                  # 每个目录塞一份
                            try:
                                _batch_copy(h, intro_ids, d)
                                copied += 1
                            except Exception as exc:    # noqa: BLE001 - 单个目录失败不中断
                                logger.warning("往目录 %s 塞简介失败:%s", d[:12], str(exc)[:80])
                        if copied:
                            logger.info("宣传简介已塞进 %d/%d 个资源目录(%s)",
                                        copied, len(dirs), intro_dir)
                        else:                            # 一个都没塞进去 ⇒ 退回并排,别让分享缺简介
                            file_ids = list(dict.fromkeys([*file_ids, *intro_ids]))
                    else:
                        file_ids = list(dict.fromkeys([*file_ids, *intro_ids]))
                        logger.info("资源含散文件 ⇒ 简介并排进分享清单:%d 个", len(intro_ids))
            except Exception as exc:                # noqa: BLE001 - 拿不到简介不该毁掉整次转存
                logger.warning("处理宣传简介失败(不挡转存/分享):%s: %s",
                               type(exc).__name__, str(exc)[:120])

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
