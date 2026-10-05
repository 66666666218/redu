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
    # ⚠️ `expires_at` 写新鲜:否则网页判断"该刷新了",会拿 refresh_token 去兑 ——
    # 那次兑换**会轮换 refresh_token**,而我们不一定接得住(2026-10-02 踩过)。
    #
    # ⚠️⚠️ **2026-10-05:`expires_at` 必须按**页面自己的格式**(ISO 串)注入** ——
    # 这里原来塞的是 `int(...)` 的**整数 epoch**,而页面 localStorage 里用的明明是
    # `2026-10-05T07:22:15.275Z` 这种 ISO 串(从库里读回的那份就是页面写的)。
    # **格式对不上 ⇒ 页面判定"这值没法用/已过期" ⇒ 照样去兑换 ⇒ refresh_token 被轮换**,
    # 于是"每天得重扫一次"(access_token 寿命实测 12 小时,到期后直连刷新必 `invalid_grant`)。
    # 实测时间线:15:20:41 captcha 续期 → **15:30 刷新就 invalid_grant**。
    _exp = xt._jwt_exp(access) or (time.time() + 3600)
    injected = {"access_token": access,
                "refresh_token": cred.get("refresh_token") or "",
                "expires_at": xt.iso_expires_at(_exp),      # ⚠️ 格式见该函数:必须是 ISO 串
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
            # ⚠️⚠️ **顶层 `expires_at` 必须一起注入**(2026-10-05 找到的"每天要重扫"真根因)。
            # 页面的凭据**存在两处**:SDK 的 `credentials_<clientId>` blob **和** 一组顶层键。
            # 见 `scripts/xunlei_login.py` 读页面时**两处都读**(`localStorage["access_token"]`
            # 与 `JSON.parse(localStorage["credentials_<cid>"])`)。
            # 这里原来只注入了 blob + 顶层 access/refresh,**漏了顶层 `expires_at`** ⇒
            # 页面读顶层读不到新鲜值 ⇒ **仍判定"该刷新了"** ⇒ 去兑换 ⇒
            # **refresh_token 被轮换掉**,而页面把新值落盘与否我们接不住 ⇒
            # 12 小时后(access 寿命实测 12h)直连刷新 `invalid_grant` ⇒ **只能重扫**。
            # 实测时间线:15:20:41 captcha 续期 → **15:30 就 invalid_grant**。
            for k, v in ((f"credentials_{client_id}", json.dumps(injected)),
                         ("access_token", injected["access_token"]),
                         ("refresh_token", injected["refresh_token"]),
                         ("expires_at", injected["expires_at"]),
                         ("expires_in", injected["expires_in"])):
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

        # ⚠️⚠️ **续期后立刻验一次刷新**(2026-10-05):网页兑换**可能已经把 refresh_token
        # 换掉**,而"**服务端轮换了、页面却没落到 localStorage**"这种情况**我们读不回来** ——
        # 于是库里那枚就是废的,但要等 access_token 到期(12 小时)才会暴露,那时**只能重扫**。
        # 与其等 12 小时,不如现在就用一次刷新把状态**钉死**:
        #   · 成功 ⇒ `_refresh_access_token` 会把**全新的一对**写回(顺带盖掉网页换走的那枚);
        #   · 失败 ⇒ **refresh_token 已死** ⇒ **当场告警"请重新扫码"**,别等它半夜自己烂掉。
        # 代价是每次续期多一次刷新调用(续期本身有 `_MIN_INTERVAL` 冷却,很便宜)。
        try:
            xt._refresh_access_token(cur.get("refresh_token") or "",
                                     cur.get("client_id") or "")
            logger.info("captcha 续期后刷新验证通过(凭据已钉死为最新)")
        except Exception as exc:  # noqa: BLE001 - 验活失败**必须说出来**,不能只写日志
            logger.error("captcha 续期后刷新验证失败:%s", str(exc)[:160])
            try:
                from app.services.alert_service import notify_incident

                notify_incident(db, uid, "xunlei", "🔴 迅雷 refresh_token 已失效,需重新扫码",
                                f"captcha 续期后立刻验证刷新就失败了:{str(exc)[:120]}。"
                                "说明这枚 refresh_token 已经被作废(多半是网页兑换轮换掉了、"
                                "而新值没落盘)。**转存与群分享两条链会全停**,"
                                "请跑 `python scripts/xunlei_login.py` 重新扫码。",
                                push_feishu=True)
                db.commit()
            except Exception:  # noqa: BLE001 - 告警失败不影响续期结果
                logger.debug("迅雷失效告警推送失败", exc_info=True)
    finally:
        db.close()
    _last_error = ""
    logger.info("captcha 已续期(device=%s…)", (minted.get("device_id") or "")[:10])
    return True


def status() -> dict:
    """给巡检/接口看的:上次铸造时间与最近一次错误。"""
    return {"last_minted_at": _last_minted, "last_error": _last_error,
            "cooldown_seconds": _MIN_INTERVAL}
