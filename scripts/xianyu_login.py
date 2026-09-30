# -*- coding: utf-8 -*-
"""闲鱼扫码登录 CLI(2026-10-01)。

**为什么**:闲鱼 Cookie 手工 F12 导出痛点——字段易漏(缺 `_m_h5_tk`/`unb`/`cookie2`
即 TOKEN_ILLEGAL)、浏览器与采集器会话互顶。本工具把"换 Cookie"缩短成**扫一次码**,
Cookie 自动入库(加密),不再需要人工复制粘贴。

协议按公开实现调研后**独立实现**(passport.goofish.com 二维码登录流程);
用 curl_cffi(TLS 指纹模拟,项目既有依赖)而非普通 httpx——与采集侧同款指纹更抗风控。

用法:
    python scripts/xianyu_login.py            # 终端显示二维码,手机闲鱼 App 扫一扫
    python scripts/xianyu_login.py --user 1   # 指定入库用户(默认 1)
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASSPORT = "https://passport.goofish.com"
_SITE = {"appName": "xianyu", "fromSite": "77", "appEntrance": "web"}
_BIZ = "taobaoBizLoginFrom=web&renderRefer=https://www.goofish.com/"
_REQUIRED = ("_m_h5_tk", "unb", "cookie2")  # 官方登录态三件套(缺一必 TOKEN_ILLEGAL)


def _log(msg: str) -> None:
    print(msg, flush=True)


def _passport_headers(ua: str) -> dict:
    return {
        "User-Agent": ua,
        "Referer": f"{PASSPORT}/mini_login.htm?lang=zh_cn&appName=xianyu&appEntrance=web",
        "Origin": PASSPORT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }


def _data_of(body: dict) -> dict:
    return ((body.get("content") or {}).get("data")) or body.get("data") or {}


def _cookie_names(session) -> set[str]:
    return {c.name for c in session.cookies.jar}


def login(user_id: int, poll_timeout: int = 180) -> int:
    from curl_cffi import requests as creq

    s = creq.Session(impersonate="chrome")
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " \
         "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    h = _passport_headers(ua)

    # ① 初始化会话(拿 XSRF-TOKEN / cookie2 / cna 等基础 Cookie)
    try:
        s.get(f"{PASSPORT}/mini_login.htm",
              params={"lang": "zh_cn", "appName": "xianyu", "appEntrance": "web",
                      "styleType": "vertical", "notLoadSsoView": "false",
                      "notKeepLogin": "false", "isMobile": "false", "qrCodeFirst": "true"},
              headers=h, timeout=20)
    except Exception as exc:  # noqa: BLE001
        _log(f"[错误] 初始化 passport 失败(网络?):{exc}")
        return 2
    csrf = (s.cookies.get("XSRF-TOKEN") or s.cookies.get("_csrf_token") or "")
    cookie2 = s.cookies.get("cookie2") or ""

    # ② 生成二维码
    try:
        r = s.get(f"{PASSPORT}/newlogin/qrcode/generate.do",
                  params={**_SITE, "_csrf_token": csrf, "umidToken": "", "hsiz": cookie2,
                          "bizParams": _BIZ, "mainPage": "false", "isMobile": "false",
                          "lang": "zh_CN", "returnUrl": "", "umidTag": "SERVER",
                          "_bx-v": "2.5.31"},
                  headers=h, timeout=20)
        data = _data_of(r.json())
    except Exception as exc:  # noqa: BLE001
        _log(f"[错误] 生成二维码失败:{exc}")
        return 2
    code_content = str(data.get("codeContent") or data.get("qrCode") or data.get("url") or "")
    t, ck = str(data.get("t") or ""), str(data.get("ck") or data.get("lgToken") or "")
    if not code_content or not t:
        _log(f"[错误] 二维码响应异常:{str(data)[:200]}")
        return 2

    # ③ 终端显示二维码
    try:
        import qrcode

        qr = qrcode.QRCode(border=1)
        qr.add_data(code_content)
        qr.make()
        _log("\n请用 **手机闲鱼 App** 扫码登录(打开 App → 我的 → 右上角扫一扫):\n")
        qr.print_ascii(invert=True)
    except ImportError:
        _log(f"未装 qrcode 库,请手动打开链接完成扫码:{code_content}")
    _log(f"\n(二维码 {poll_timeout} 秒内有效;等待扫码...)")

    # ④ 轮询
    deadline = time.time() + poll_timeout
    status = "NEW"
    last = ""
    while time.time() < deadline:
        time.sleep(2.5)
        try:
            r2 = s.post(f"{PASSPORT}/newlogin/qrcode/query.do",
                        params={**_SITE, "_bx-v": "2.5.31"},
                        data={"t": t, "ck": ck, **_SITE, "_csrf_token": csrf,
                              "umidToken": "", "hsiz": cookie2, "bizParams": _BIZ,
                              "mainPage": "false", "isMobile": "false", "lang": "zh_CN",
                              "returnUrl": "", "umidTag": "SERVER", "navlanguage": "zh-CN",
                              "navUserAgent": ua, "navPlatform": "Win32", "isIframe": "true",
                              "documentReferer": "https://www.goofish.com/",
                              "defaultView": "qrcode",
                              "deviceId": s.cookies.get("cna") or ""},
                        headers={**h, "Content-Type": "application/x-www-form-urlencoded"},
                        timeout=20)
            d2 = _data_of(r2.json())
        except Exception as exc:  # noqa: BLE001 - 单次轮询失败不终止
            _log(f"(轮询异常,继续:{str(exc)[:60]})")
            continue
        raw = str(d2.get("qrCodeStatus") or d2.get("status") or "NEW").upper()
        if raw in ("NEW", "SCANED"):
            if raw != last:
                _log({"NEW": "等待扫码...", "SCANED": "已扫码,请在手机上确认登录 ✓"}[raw])
                last = raw
            continue
        if raw in ("CONFIRMED", "SUCCESS"):
            status = "CONFIRMED"
            break
        if raw in ("EXPIRED", "CANCELED", "CANCELLED"):
            _log(f"[失败] 二维码已{('过期' if raw == 'EXPIRED' else '取消')},请重新运行本脚本")
            return 3
        _log(f"(未知状态 {raw},继续等待)")

    if status != "CONFIRMED":
        _log("[失败] 超时未确认,请重新运行本脚本")
        return 3

    # ⑤ 收集 Cookie → 校验三件套 → 入库
    names = _cookie_names(s)
    missing = [n for n in _REQUIRED if n not in names]
    if missing:
        _log(f"[失败] 登录已确认但 Cookie 不完整(缺 {'、'.join(missing)})。")
        _log("  可能需要在手机 App 上完成人脸/安全验证后重试;或换用浏览器导出。")
        return 4
    cookie_str = "; ".join(f"{c.name}={c.value}" for c in s.cookies.jar
                           if c.name and c.value)

    from app.db import get_session_local
    from app.services.cookie_store import set_cookie

    db = get_session_local()()
    try:
        set_cookie(db, user_id, "goofish", cookie_str)
    finally:
        db.close()
    _log(f"\n✅ 登录成功!Cookie({len(names)} 个字段)已加密写入 用户#{user_id} 的 goofish 平台。")
    _log("  下一次采集轮(30 分钟内)自动生效;本工具不侵入运行中的服务。")
    _log("  ⚠️ 扫码后尽量别再用浏览器打开闲鱼,避免会话互顶。")
    return 0


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description="闲鱼扫码登录,自动入库 Cookie")
    ap.add_argument("--user", type=int, default=1, help="入库用户 ID(默认 1)")
    args = ap.parse_args()
    return login(args.user)


if __name__ == "__main__":
    raise SystemExit(main())
