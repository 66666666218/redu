"""迅雷 captcha 续期(2026-10-02):**借网页版铸一枚**,写回 cookie_store。

**为什么是"借网页版"**:盘写操作要 `captcha_token`,而它由**网页版身份**铸出来的才被
服务端认(captcha 与 `client_id`/`device_id` 三者绑定)。我们自取的那条路走不通 ——
`captcha/init` 能返回 200,但 `/drive/v1/share` 一律 `no client info found`(详见 CHANGELOG
2026-10-02 13:06/14:05 两条)。

**做法**:Playwright 打开 `pan.xunlei.com`,注入当前凭据(⚠️ **`expires_at` 必须写新鲜**,
否则网页会去兑 refresh_token 并把它轮换掉),再打开**任意分享页**触发网页自己铸 captcha,
然后把 `x-captcha-token` 与同一次请求的 `device_id`/`client_id` 一起拿回来。

**回写什么**:captcha 三件套 + 网页刷新出来的 `access_token`/`refresh_token`
(页面若轮换过 token,不回写就等于丢;实测 `expires_at` 新鲜时它不会轮换)。

跑一次约 15 秒,所以有**冷却时间**(`_MIN_INTERVAL`),避免一次失败把浏览器开成串。
"""
from __future__ import annotations
from app.utils.ua import CHROME_WINDOWS  # 统一 UA

import json
import time

from app.utils import get_logger

logger = get_logger(__name__)

_MIN_INTERVAL = 60          # 两次铸造之间的最小间隔(秒),防止连环失败时把浏览器开成串
_CREDS_KEY = "xunlei"
_PAN_HOME = "https://pan.xunlei.com/"
# 兜底用的分享页(DB 里没有群分享时用它触发网页铸 captcha)
_FALLBACK_SHARE = "https://pan.xunlei.com/s/VOtw0rXU99xNQ-XD-0vtBexoA1?pwd=gcsk"
_last_minted: float = 0.0
_last_error: str = ""


def _pick_share_url() -> str:
    """挑一个分享页来触发铸 captcha:优先用库里已有的群分享链,失败回落到常量。"""
    try:
        from sqlalchemy import select

        from app.db import get_session_local
        from app.db.models import XunleiGroupShare

        db = get_session_local()()
        try:
            url = db.scalar(select(XunleiGroupShare.origin_url)
                            .where(XunleiGroupShare.origin_url != "")
                            .order_by(XunleiGroupShare.id.desc()).limit(1))
            if url:
                return str(url)
        finally:
            db.close()
    except Exception:  # noqa: BLE001 - 挑不到就用常量
        logger.debug("挑分享页失败,用兜底链", exc_info=True)
    return _FALLBACK_SHARE


def _mint_via_web(share_url: str) -> dict:
    """开浏览器铸一枚 captcha。返回 `{token, device_id, client_id}` + 可能更新过的 token。

    失败返回 `{}`(调用方自己决定怎么办),不抛 —— 它跑在转存重试路径上。
    """
    from app.services import xunlei_transfer as xt

    cred = xt._credentials()
    if not cred:
        return {}
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:  # pragma: no cover - 环境没装 playwright 时不该炸
        logger.warning("未装 playwright,无法借网页铸 captcha")
        return {}

    client_id = cred.get("client_id") or xt._WEB_CLIENT_ID
    access = xt._access_token(cred)
    # ⚠️ expires_at 写新鲜:否则网页判断"该刷新了",会拿 refresh_token 去兑 —— 那次兑换
    # **会轮换 refresh_token**,而我们不一定接得住(2026-10-02 踩过)
    injected = {"access_token": access,
                "refresh_token": cred.get("refresh_token") or "",
                "expires_at": int(xt._jwt_exp(access) or (time.time() + 3600)),
                "expires_in": 43200,
                "sub": xt._jwt_sub(access),
                "user_id": xt._jwt_sub(access),
                "token_type": "Bearer"}
    minted: dict = {}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(user_agent=CHROME_WINDOWS)
            pg = ctx.new_page()

            def on_req(req) -> None:
                if "/drive/v1/" in req.url and req.headers.get("x-captcha-token"):
                    minted.setdefault("token", req.headers["x-captcha-token"])
                    minted["device_id"] = req.headers.get("x-device-id") or ""
                    minted["client_id"] = req.headers.get("x-client-id") or ""

            pg.on("request", on_req)
            pg.goto(_PAN_HOME, wait_until="domcontentloaded", timeout=60000)
            for k, v in ((f"credentials_{client_id}", json.dumps(injected)),
                         ("access_token", injected["access_token"]),
                         ("refresh_token", injected["refresh_token"])):
                pg.evaluate("([k, v]) => localStorage.setItem(k, v)", [k, v])
            pg.goto(share_url, wait_until="domcontentloaded", timeout=60000)
            for _ in range(25):
                if minted.get("token"):
                    break
                time.sleep(1)
            try:                       # 网页若自己换过 token,一并读回(不然等于丢)
                ls = pg.evaluate("() => Object.fromEntries(Object.entries(localStorage))")
                for k in ("access_token", "refresh_token", "expires_at"):
                    if ls.get(k) and str(ls[k]) != str(injected.get(k, "")):
                        minted[k] = ls[k]
            except Exception:  # noqa: BLE001
                logger.debug("读回 localStorage 失败", exc_info=True)
            browser.close()
    except Exception:  # noqa: BLE001
        logger.exception("借网页铸 captcha 失败")
        return {}
    return minted


def refresh(force: bool = False) -> bool:
    """铸一枚新 captcha 并写回 cookie_store。成功 True;冷却中/失败 False。

    `force=True` 跳过冷却(给"已经确定失效、要立刻补"的场景)。
    """
    global _last_minted, _last_error
    if not force and time.time() - _last_minted < _MIN_INTERVAL:
        logger.info("captcha 续期在冷却中(%.0fs 前刚铸过)", time.time() - _last_minted)
        return False
    minted = _mint_via_web(_pick_share_url())
    _last_minted = time.time()
    if not minted.get("token"):
        _last_error = "没铸到 captcha(页面可能被要求登录)"
        logger.warning("captcha 续期失败:%s", _last_error)
        return False

    from sqlalchemy import select

    from app.db import get_session_local
    from app.db.models import User
    from app.services import xunlei_transfer as xt
    from app.services.cookie_store import set_cookie

    db = get_session_local()()
    try:
        uid = db.scalar(select(User.id).where(User.enabled.is_(True)).order_by(User.id))
        if not uid:
            return False
        cur = dict(xt._credentials())
        for k in ("access_token", "refresh_token", "expires_at"):    # 网页轮换过的优先
            if minted.get(k):
                cur[k] = minted[k]
        cur.update(captcha_token=minted["token"], device_id=minted["device_id"],
                   client_id=minted["client_id"])
        set_cookie(db, uid, _CREDS_KEY, json.dumps(cur, ensure_ascii=False))
    finally:
        db.close()
    _last_error = ""
    logger.info("captcha 已续期(device=%s…)", (minted.get("device_id") or "")[:10])
    return True


def status() -> dict:
    """给巡检/接口看的:上次铸造时间与最近一次错误。"""
    return {"last_minted_at": _last_minted, "last_error": _last_error,
            "cooldown_seconds": _MIN_INTERVAL}
