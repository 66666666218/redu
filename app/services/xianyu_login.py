# -*- coding: utf-8 -*-
"""闲鱼扫码登录服务(2026-10-01 可视化版):前端一键扫码,替代命令行工具。

协议与 scripts/xianyu_login.py 同源(passport.goofish.com 二维码流程),
本模块把交互式脚本改造成**无状态 HTTP 服务可用**的会话机:

- `start_qr_login(user_id)`  → 生成二维码(PNG base64) + session_id(进程内存表);
- `poll_qr_login(session_id)` → 轮询:等待/已扫/确认/成功(Cookie 已加密入库)/过期;
- 会话 15 分钟过期自动清理;单进程内存表(本机单实例部署,够用且无持久化负担)。

登录成功入库前做**登录态校验**(unb+cookie2;`_m_h5_tk` 是 mtop 短效令牌,
采集器首次请求自动获取,不在此校验——2026-10-01 踩过)。
"""
from __future__ import annotations

import base64
import io
import secrets
import threading
import time

from curl_cffi import requests as creq

from app.utils import get_logger

logger = get_logger(__name__)

PASSPORT = "https://passport.goofish.com"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
_SITE = {"appName": "xianyu", "fromSite": "77", "appEntrance": "web"}
_BIZ = "taobaoBizLoginFrom=web&renderRefer=https://www.goofish.com/"
_TTL = 900          # 会话有效期(秒)
_REQUIRED = ("unb", "cookie2")   # 登录态核心;token 动态获取不校验

_sessions: dict[str, dict] = {}
_lock = threading.Lock()


def _h() -> dict:
    return {"User-Agent": _UA, "Referer": f"{PASSPORT}/mini_login.htm?lang=zh_cn&appName=xianyu&appEntrance=web",
            "Origin": PASSPORT, "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9"}


def _data_of(body: dict) -> dict:
    return ((body.get("content") or {}).get("data")) or body.get("data") or {}


def _gc() -> None:
    now = time.time()
    with _lock:
        for sid in [s for s, v in _sessions.items() if now - v["created"] > _TTL]:
            _sessions.pop(sid, None)


def start_qr_login(user_id: int = 1) -> dict:
    """生成登录二维码。返回 {session_id, qr_png(base64), expires_in};失败抛异常。"""
    _gc()
    s = creq.Session(impersonate="chrome")
    s.get(f"{PASSPORT}/mini_login.htm",
          params={"lang": "zh_cn", "appName": "xianyu", "appEntrance": "web",
                  "styleType": "vertical", "notLoadSsoView": "false",
                  "notKeepLogin": "false", "isMobile": "false", "qrCodeFirst": "true"},
          headers=_h(), timeout=20)
    csrf = s.cookies.get("XSRF-TOKEN") or s.cookies.get("_csrf_token") or ""
    cookie2 = s.cookies.get("cookie2") or ""
    r = s.get(f"{PASSPORT}/newlogin/qrcode/generate.do",
              params={**_SITE, "_csrf_token": csrf, "umidToken": "", "hsiz": cookie2,
                      "bizParams": _BIZ, "mainPage": "false", "isMobile": "false",
                      "lang": "zh_CN", "returnUrl": "", "umidTag": "SERVER", "_bx-v": "2.5.31"},
              headers=_h(), timeout=20)
    data = _data_of(r.json())
    code_content = str(data.get("codeContent") or data.get("qrCode") or data.get("url") or "")
    t, ck = str(data.get("t") or ""), str(data.get("ck") or data.get("lgToken") or "")
    if not code_content or not t:
        raise RuntimeError(f"二维码生成失败:{str(data)[:160]}")
    import qrcode

    img = qrcode.make(code_content)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    sid = secrets.token_urlsafe(16)
    with _lock:
        _sessions[sid] = {"session": s, "t": t, "ck": ck, "csrf": csrf, "cookie2": cookie2,
                          "status": "waiting", "created": time.time(), "user_id": user_id}
    return {"session_id": sid, "qr_png": base64.b64encode(buf.getvalue()).decode(),
            "expires_in": _TTL}


def poll_qr_login(session_id: str) -> dict:
    """轮询扫码状态;确认后自动校验+加密入库。返回 {status, message?}。"""
    with _lock:
        st = _sessions.get(session_id)
    if st is None:
        return {"status": "not_found", "message": "会话不存在或已过期,请重新生成二维码"}
    if time.time() - st["created"] > _TTL:
        return {"status": "expired", "message": "二维码已过期,请重新生成"}
    if st["status"] in ("success", "expired"):
        return {"status": st["status"], "message": st.get("message", "")}

    s = st["session"]
    try:
        r = s.post(f"{PASSPORT}/newlogin/qrcode/query.do",
                   params={**_SITE, "_bx-v": "2.5.31"},
                   data={"t": st["t"], "ck": st["ck"], **_SITE, "_csrf_token": st["csrf"],
                         "umidToken": "", "hsiz": st["cookie2"], "bizParams": _BIZ,
                         "mainPage": "false", "isMobile": "false", "lang": "zh_CN",
                         "returnUrl": "", "umidTag": "SERVER", "navlanguage": "zh-CN",
                         "navUserAgent": _UA, "navPlatform": "Win32", "isIframe": "true",
                         "documentReferer": "https://www.goofish.com/",
                         "defaultView": "qrcode", "deviceId": s.cookies.get("cna") or ""},
                   headers={**_h(), "Content-Type": "application/x-www-form-urlencoded"},
                   timeout=20)
        d = _data_of(r.json())
    except Exception as exc:  # noqa: BLE001 - 单次轮询失败不终止会话
        logger.warning("闲鱼扫码轮询异常:%s", str(exc)[:100])
        return {"status": "waiting", "message": "轮询中"}

    raw = str(d.get("qrCodeStatus") or d.get("status") or "NEW").upper()
    if raw == "NEW":
        return {"status": "waiting"}
    if raw == "SCANED":
        st["status"] = "scanned"
        return {"status": "scanned", "message": "已扫码,请在手机上确认登录"}
    if raw in ("EXPIRED", "CANCELED", "CANCELLED"):
        st["status"] = "expired"
        return {"status": "expired", "message": "二维码已过期,请重新生成"}
    if raw not in ("CONFIRMED", "SUCCESS"):
        return {"status": "waiting", "message": f"未知状态 {raw}"}

    # 已确认:预热拿 mtop 令牌(网关 Set-Cookie 自动进 jar) → 校验登录态 → 入库
    st["status"] = "confirmed"
    uid = st.get("user_id") or 1
    try:
        s.get("https://h5api.m.goofish.com/h5/mtop.common.getTimestamp/1.0/",
              headers={"User-Agent": _UA, "Referer": "https://www.goofish.com/"}, timeout=20)
    except Exception:  # noqa: BLE001 - 预热失败不挡入库(采集器会自动补)
        pass
    names = {c.name for c in s.cookies.jar}
    missing = [n for n in _REQUIRED if n not in names]
    if missing:
        st["status"] = "failed"
        return {"status": "failed",
                "message": f"登录已确认但缺 {'、'.join(missing)}(可能需在 App 完成人脸验证),请重试"}
    cookie_str = "; ".join(f"{c.name}={c.value}" for c in s.cookies.jar if c.name and c.value)
    from app.db import get_session_local
    from app.services.cookie_store import set_cookie

    db = get_session_local()()
    try:
        set_cookie(db, uid, "goofish", cookie_str)
    finally:
        db.close()
    st["status"] = "success"
    st["message"] = f"登录成功,Cookie({len(names)} 字段)已加密入库"
    logger.info("闲鱼扫码登录成功 user=%s 字段=%s", uid, len(names))
    return {"status": "success", "message": st["message"]}
