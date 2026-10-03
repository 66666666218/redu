"""迅雷扫码登录 —— 抽全量凭据并**直接写进 cookie_store**。

**从 `tools/xl_qr_login.py` 迁来**(2026-10-04)。`tools/` 整个被 `.gitignore` 忽略,
所以那份**只存在于本机** —— 机器一丢、或换台机部署,这个能力就没了。
同类先例:`scripts/xianyu_login.py`、`scripts/mc_login.py` 都已经迁进来了。

跑法(主环境有 playwright 即可,**不需要** MediaCrawler 的 venv):
    python scripts/xunlei_login.py

**抽哪些**(后面调盘接口都要用,少一个都会失败):
  `access_token`(12h)、`refresh_token`、`captcha_token`、`device_id`、`client_id`

⚠️ **登录后必须再走一步:打开一个分享页。** 两个原因(**都是踩过的坑**):
  ① 网页**只有在需要时**才铸 captcha;开分享页正好触发它,我们才拿得到 `captcha_token`;
  ② **网页自己刷新 token 时会轮换 `refresh_token`**(旧的用一次即废),所以要
     **等页面折腾完再读最终态** —— 否则存下来的 refresh_token 下一秒就是废的
     (2026-10-02 踩过:沿用了旧 expires_at,refresh_token 被网页用掉,只能重扫)。

⚠️ **`refresh_token` 失效是本项目最常见的一次性故障**(2026-10-04 又遇到一次):
盘**读**操作(列文件/群消息)用缓存凭据就行,所以**采集看着一切正常**;
只有**转存/分享这类写操作**才需要新 token,于是失效**只在写操作上暴露**。
判断方法:`python -c "from app.services import xunlei_transfer as x; print(x.verify())"`。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# Windows 控制台默认 GBK,直接 print 非 ASCII(如 ✓)会 UnicodeEncodeError
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from playwright.sync_api import sync_playwright  # noqa: E402

from app.services import xunlei_transfer as xt  # noqa: E402

HOME = "https://pan.xunlei.com/"
# 开这个分享页是为了**触发网页铸 captcha**(它自己会去 captcha/init)
SHARE = "https://pan.xunlei.com/s/VOtw0rXU99xNQ-XD-0vtBexoA1?pwd=gcsk"
WAIT_SECONDS = 300
CLIENT_ID = xt._WEB_CLIENT_ID


def _read_local_storage(pg) -> dict:
    try:
        return pg.evaluate("() => Object.fromEntries(Object.entries(localStorage))")
    except Exception:  # noqa: BLE001
        return {}


def _collect(pg) -> dict:
    """把 localStorage 里的凭据拼成我们存库用的那份。"""
    ls = _read_local_storage(pg)
    blob: dict = {}
    # 1) SDK 的 credentials_<clientId>(网页登录后写的那份,字段最全)
    try:
        sdk = json.loads(ls.get(f"credentials_{CLIENT_ID}") or "{}")
        blob.update({k: v for k, v in sdk.items() if v})
    except Exception:  # noqa: BLE001
        pass
    # 2) 顶层扁平 key(网页刷 token 后会写这里,以最新为准)
    for key, field in (("access_token", "access_token"),
                       ("refresh_token", "refresh_token"),
                       ("expires_at", "expires_at"),
                       ("sub", "sub")):
        if ls.get(key):
            blob[field] = ls[key]
    # 3) 设备号:网页自己生成的那个 —— captcha 与它绑定,必须一起存
    if ls.get("deviceid"):
        blob["device_id"] = str(ls["deviceid"])
    # 4) captcha:存的是 **JSON**(`{"token": "ck0...", "expires_at": "..."}`),要取 token
    raw = ls.get(f"captcha_{CLIENT_ID}") or ""
    if raw:
        try:
            blob["captcha_token"] = json.loads(raw).get("token") or ""
        except ValueError:
            blob["captcha_token"] = raw
    blob["client_id"] = CLIENT_ID
    return blob


def main() -> int:
    captured: dict = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)        # 有头:用户要扫码
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"))
        pg = ctx.new_page()

        def on_req(req) -> None:
            """兜底:captcha 没落 localStorage 时,从请求头里捞。"""
            if "/drive/v1/" in req.url and req.headers.get("x-captcha-token"):
                captured.setdefault("captcha_token", req.headers["x-captcha-token"])
                captured.setdefault("device_id", req.headers.get("x-device-id") or "")
                captured.setdefault("client_id", req.headers.get("x-client-id") or "")

        pg.on("request", on_req)
        pg.goto(HOME, wait_until="domcontentloaded", timeout=60000)
        print("=== 浏览器已打开 pan.xunlei.com ===", flush=True)
        print("=== 请点「扫码登录」,用迅雷 App 扫码确认(最多等 5 分钟)===", flush=True)

        deadline = time.time() + WAIT_SECONDS
        while time.time() < deadline:
            time.sleep(3)
            ls = _read_local_storage(pg)
            if ls.get("refresh_token") or ls.get(f"credentials_{CLIENT_ID}"):
                break
        else:
            print("=== 超时,没检测到登录态 ===", flush=True)
            browser.close()
            return 1

        print("=== 登录成功,开一个分享页让网页把 captcha 铸出来(约 12 秒)===", flush=True)
        try:
            pg.goto(SHARE, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:  # noqa: BLE001
            print("(分享页导航异常,继续):", exc, flush=True)
        time.sleep(12)                      # 等它刷 token + 铸 captcha(别缩短:refresh_token 会被轮换)

        blob = _collect(pg)
        # 请求头里捞到的 (device_id, client_id) 是 captcha 实际绑定的那组,**优先用它**
        # (localStorage 的 `deviceid` 可能是别的形态,对不上就 captcha_invalid)
        for k in ("device_id", "client_id"):
            if captured.get(k):
                blob[k] = captured[k]
        if captured.get("captcha_token"):
            blob["captcha_token"] = captured["captcha_token"]
        browser.close()

    missing = [k for k in ("access_token", "refresh_token", "captcha_token", "device_id")
               if not blob.get(k)]
    print(f"=== 抽到:{sorted(blob.keys())} ===", flush=True)
    if missing:
        print(f"⚠️ 缺 {missing} —— 盘写操作可能失败,建议重跑一次", flush=True)

    from sqlalchemy import select

    from app.db import get_session_local, init_db
    from app.db.models import User
    from app.services.cookie_store import set_cookie

    init_db()
    db = get_session_local()()
    try:
        uid = db.scalar(select(User.id).where(User.enabled.is_(True)).order_by(User.id))
        if not uid:
            print("没有启用的用户", flush=True)
            return 1
        set_cookie(db, uid, "xunlei", json.dumps(blob, ensure_ascii=False))
        print(f"=== 已加密写入 cookie_store(user={uid}, platform=xunlei) ===", flush=True)
        print("=== 验证 ===", flush=True)
        print(json.dumps(xt.verify(), ensure_ascii=False)[:200], flush=True)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
