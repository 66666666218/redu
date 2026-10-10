"""微信读书 **App 侧凭据**的获取与自愈(2026-10-05)。

**为什么单独成模块**:取 token 原来写在 `scripts/weread_app_login.py` 里,而
**服务层不能依赖 `scripts/`**(分层要求,而且 Docker 镜像根本不 COPY `scripts/`)。
现在核心逻辑在这里,脚本只是薄 CLI 包装,`WereadAppClient` 也能调它**自愈**。

**它解决什么**:App 的 `accessToken` **会随 App 会话轮换**(实测
`3RRAf8Il` → `slJzpb3p` → `ALRPC8tY`)。轮换后接口回 `-2010/-2012`,
阅读数就又断了。所以"能自动重取"是这条链**长期不断**的前提 ——
否则每次失效都要人记得去跑脚本,那种"靠人记得"的机制迟早会忘(本仓已经吃过一次)。

⚠️ **重取需要雷电模拟器开着**(token 只存在于 App 的账号库里)。
所以自愈**会失败**,这不是异常而是常态 —— 调用方必须按"可能拿不到"处理,
**绝不能因为它拿不到就把整轮监听搞挂**。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.utils import get_logger

logger = get_logger(__name__)

PKG = "com.tencent.weread"
DB_REL = f"/data/data/{PKG}/databases/WRAccount"
#: `deviceid` 在这里(XML,不是数据库)。**只需读一次**,读完就能纯 HTTP 续期。
DEVICE_XML_REL = f"/data/data/{PKG}/shared_prefs/device.xml"
DEFAULT_ADB = r"D:\leidian\LDPlayer14\adb.exe"

#: ★★ **纯 HTTP 续期**(2026-10-10 实测打通)**——这是"模拟器可以彻底退休"的那一步**。
#:
#: 算法与身份都来自公开实现 **`teng-lin/weread-omni`**(`src/profile.ts` / `src/device-ua.ts`):
#:
#:     signature = sha256(f"{timestamp}{deviceId}{random}")      ← **无密钥**,64 位 hex
#:
#: ⚠️⚠️ **一条本仓记错了很久的结论**:先前写的是「卡在 **64 位 signature(带密钥)**,要逆微读 APK」——
#: **错**。当时试过 4 种拼法(排序 JSON / 原序 k=v / 去掉 sig 字段再哈希,md5+sha256),
#: 唯独没试过**最朴素的纯拼接**。2026-10-10 按上面的写法一次就通(HTTP 200 + 有效 accessToken)。
#:
#: ⚠️ **第二个坑(实测踩到)**:**token 必须与铸它的客户端身份配套用**。
#: 用 eink 身份铸、却拿安卓 App 的头去调 `/book/articles`,回 `-2012 登录超时`;
#: 换成**同一套 eink 头**再调 → 200,`review.mpInfo.readNum` 正常拿到(实测 31)。
LOGIN_URL = "https://i.weread.qq.com/login"
#: eink(BOOX)客户端身份 —— 与 `teng-lin/weread-omni` 的 `einkDevice()` 一致
EINK_HEADERS = {
    "Accept-Charset": "UTF-8",
    "Accept": "*/*",
    "baseapi": "30",
    "appver": "2.1.2.10245900",
    "basever": "2.1.2.10245900",
    "osver": "11",
    "channelId": "900",
    "wrbrand": "Onyx",
    "User-Agent": ("WeRead/2.1.2 WRBrand/Onyx wr_eink Dalvik/2.1.0 "
                   "(Linux; U; Android 11; BOOX Build/onyx)"),
}
_EINK_DEVICE_NAME = "BOOX"
_EINK_DEVICE_TYPE = 3


class WereadAppTokenError(RuntimeError):
    """**纯 HTTP 铸 token 失败**(HTTP 码或业务码)。

    单独成一个类型,是为了让调用方能把「这条路本身不通(刷新令牌废了/被风控)」
    与「网络抖动」分开 —— 前者该去唤醒 App 兜底,后者重试即可(与 `WereadAppAuthError` 同一考虑)。
    """
PLATFORM = "weread_app"


def _find_adb(explicit: str = "") -> str:
    for cand in (explicit, os.getenv("WEREAD_ADB_PATH", ""), DEFAULT_ADB,
                 shutil.which("adb") or ""):
        if cand and Path(cand).exists():
            return cand
    return ""


def _run(adb: str, *args: str, timeout: int = 120) -> str:
    p = subprocess.run([adb, *args], capture_output=True, timeout=timeout)
    return (p.stdout + p.stderr).decode("utf-8", "replace").strip()


def pull_from_emulator(adb: str = "", tmpdir: str | None = None) -> dict[str, Any]:
    """从雷电模拟器里把 `accessToken` / `vid` 读出来。**失败抛异常**,别返回空。

    ⚠️ **必须连 `-wal` 一起拉**:token 的最新值在 WAL 里,只拉主库会读到**上一轮**的旧值
    (实测踩过:主库给 `slJzpb3p`、带上 WAL 才是当前值)。
    """
    exe = _find_adb(adb)
    if not exe:
        raise RuntimeError(f"找不到 adb(试过 {DEFAULT_ADB}、$WEREAD_ADB_PATH、PATH)")
    dev = _run(exe, "devices")
    if "emulator" not in dev and "127.0.0.1" not in dev:
        raise RuntimeError("没有已连接的模拟器 —— 需要开雷电(并打开设置里的 ADB 开关)")
    _run(exe, "root", timeout=60)
    _run(exe, "shell",
         f"cp {DB_REL} /sdcard/_wa.db; cp {DB_REL}-wal /sdcard/_wa.db-wal 2>/dev/null; "
         f"cp {DB_REL}-shm /sdcard/_wa.db-shm 2>/dev/null; echo ok")

    own = tmpdir is None
    td = tmpdir or tempfile.mkdtemp(prefix="wracc_")
    try:
        local = os.path.join(td, "WRAccount")
        for src, dst in (("/sdcard/_wa.db", local),
                         ("/sdcard/_wa.db-wal", local + "-wal"),
                         ("/sdcard/_wa.db-shm", local + "-shm")):
            subprocess.run([exe, "pull", src, dst], capture_output=True, timeout=180)
        if not Path(local).exists():
            raise RuntimeError("拉取账号库失败(微信读书可能没登录)")
        con = sqlite3.connect(local)
        cols = [c[1] for c in con.execute("PRAGMA table_info(Account)")]
        rows = [dict(zip(cols, r)) for r in con.execute("SELECT * FROM Account")]
        con.close()
    finally:
        if own:
            shutil.rmtree(td, ignore_errors=True)

    rows = [r for r in rows if r.get("accessToken") and r.get("vid")]
    if not rows:
        raise RuntimeError("账号库里没有有效的 accessToken/vid —— App 里是不是没登录?")
    row = rows[0]
    # ★ **`refreshToken` 也一起读**:它是**长效**凭据,而 `POST https://i.weread.qq.com/login`
    # 的请求体里 `refreshToken` 就是那个「长效→短效」的兑换凭据(查历史抓包
    # `data/_mitm_weread.txt` 得出的)。
    #
    # ⚠️⚠️ **2026-10-10 订正:这只是"读出来了",兑换那一步从来没实现。**
    # 全仓库**没有任何 `/login` 的调用**(`weread_app_client` 只有 `articles()`)——
    # 所以**续期实际仍然靠 `refresh_with_wake()` 用 adb 唤醒 App 重读账号库**,
    # 模拟器照样每次都得开。原文写的是"日常续期不再需要它",读起来像**已经做完了**
    # (本仓最忌的"看着像做了")。真要做,卡点是 `/login` 的**64 位签名(带密钥)**,
    # 得逆微读 APK —— 与夸克那次同一形状,但**难度不在算法而在签名**。
    return {"accessToken": str(row["accessToken"]), "vid": str(row["vid"]),
            "refreshToken": str(row.get("refreshToken") or ""),
            "refreshTokenExpired": row.get("refreshTokenExpired"),
            "userName": row.get("userName") or "",
            #: 这枚 token 是**安卓 App** 签发的 ⇒ 调用时要用安卓 App 的身份头
            #: (与 HTTP 铸出来的 eink token 不能混用,见 `EINK_HEADERS` 上面的实测)
            "profile": "app",
            "pulled_at": datetime.now().isoformat(" ", "seconds")}


def read_device_id_from_emulator(adb: str = "") -> str:
    """从模拟器 `shared_prefs/device.xml` 读 `deviceid`(实测 **38 位数字**)。

    ⚠️ **只需读一次**:拿到之后纯 HTTP 续期就不再需要模拟器了。
    ⚠️ 记忆里曾把它记成「38 位 hex」——**不准**,它是纯数字(实测
    `35165432967079520699752786780101783056`)。
    """
    import re

    exe = _find_adb(adb)
    if not exe:
        raise RuntimeError(f"找不到 adb(试过 {DEFAULT_ADB}、$WEREAD_ADB_PATH、PATH)")
    _run(exe, "root", timeout=60)
    txt = _run(exe, "shell", f"cat {DEVICE_XML_REL}", timeout=60)
    m = re.search(r'name="deviceid"[^>]*>\s*([0-9A-Za-z]+)\s*<', txt or "")
    if not m:
        raise RuntimeError(f"device.xml 里没有 deviceid(拿到 {len(txt or '')} 字节)")
    return m.group(1)


def mint_access_token_http(device_id: str, refresh_token: str, *,
                           timeout: float = 25.0) -> dict[str, Any]:
    """**纯 HTTP** 换新 `accessToken`(不需要模拟器、不需要 App)。失败抛异常。

    签名 = `sha256(f"{timestamp}{deviceId}{random}")`,**无密钥**;身份用 eink(见 `EINK_HEADERS`)。
    ⚠️ 调用方拿到的 token **必须配 `EINK_HEADERS` 去用**(`WereadAppClient(profile="eink")`),
    混用安卓 App 的头会 `-2012`。
    ⚠️ 响应里**可能带新的 `refreshToken`**(会轮换)⇒ 有就**必须写回**,否则用一次就废。
    """
    import hashlib
    import random as _random

    import requests

    if not device_id or not refresh_token:
        raise RuntimeError("缺 deviceId 或 refreshToken(先跑一次 `pull_from_emulator` 取)")
    ts = int(time.time() * 1000)
    rnd = _random.randint(1, 1000)
    body = {
        "deviceId": device_id, "deviceName": _EINK_DEVICE_NAME, "inBackground": 0,
        "kickType": 1, "random": rnd, "refCgi": "", "refreshToken": refresh_token,
        "signature": hashlib.sha256(f"{ts}{device_id}{rnd}".encode()).hexdigest(),
        "timestamp": ts, "trackId": "",
        "deviceType": _EINK_DEVICE_TYPE,
    }
    r = requests.post(LOGIN_URL, headers={**EINK_HEADERS,
                                          "content-type": "application/json; charset=UTF-8"},
                      data=json.dumps(body), timeout=timeout)
    j = r.json()
    tok = str(j.get("accessToken") or "")
    if r.status_code != 200 or not tok:
        raise WereadAppTokenError(
            f"HTTP 铸 token 失败:{r.status_code} {str(j.get('errCode') or j.get('errMsg') or '')[:120]}")
    return {"accessToken": tok, "vid": str(j.get("vid") or ""),
            "refreshToken": str(j.get("refreshToken") or refresh_token),
            "userName": str((j.get("user") or {}).get("name") or ""),
            "profile": "eink"}


def load(session: Session, user_id: int) -> dict[str, Any] | None:
    """读已存的 App 凭据(JSON);没有/坏了都返回 `None` 并**留一条 warning**。"""
    from app.services.cookie_store import get_cookie

    try:
        raw = get_cookie(session, user_id, PLATFORM)
        if not raw:
            return None
        return json.loads(raw)
    except Exception as exc:  # noqa: BLE001 - 坏凭据 = 当没配,但要说出来
        logger.warning("App 侧凭据不可用(%s):%s", PLATFORM, str(exc)[:120])
        return None


#: 微信读书 App 的启动组件 —— 唤醒它用。
#: ⚠️ 实测 `am start -a MAIN -c LAUNCHER <pkg>` 在这个镜像上**会被拒**
#: ("unable to resolve Intent"),**必须用显式组件名**。
#: 取自 `adb shell cmd package resolve-activity --brief com.tencent.weread`。
WEREAD_ACT = "com.tencent.weread/.LauncherActivity"


def wake_app(adb: str = "", wait: float = 8.0) -> bool:
    """用 adb 把微信读书 App **拉到前台** —— 目的是**让它刷新会话**。

    ## 为什么这一步是必须的(2026-10-07 实测)
    一直以为"阅读数断了 = 登录态失效,要人扫一次"。查下来**大半不是**:
      · App 长时间不动时,它数据库里的 `accessToken` 会停在**旧值**,服务端已经不认;
      · 而**只要 App 活动一次,它就会写一个新的、有效的**。
    实测(同一分钟内):
        拉到的 `3sg_FVG7` → **-2012 登录超时**
        拉到的 `HbVEHrVJ` → **✓ 88 篇,82 篇带阅读数**(之后连拉 4 次都稳)
    ⇒ "登录态失效"里有一大块其实是"**App 没活动**",**不用人手动去点**。
    """
    exe = _find_adb(adb)
    if not exe:
        return False
    try:
        subprocess.run([exe, "shell", "am", "start", "-n", WEREAD_ACT],
                       capture_output=True, timeout=60)
        time.sleep(wait)
        return True
    except Exception:  # noqa: BLE001 - 唤醒失败就照旧试一次,别把整轮带崩
        logger.debug("唤醒微信读书 App 失败", exc_info=True)
        return False


def verify_by_api(session: Session, user_id: int, *, profile: str = ""):
    """造一个 `(token, vid) -> bool` 的验活函数:**真拿一个号问一次 App 接口**。

    ⚠️ 关键在于**真的问了**。调用方要知道的是"这个 token 现在能不能用",
    不是"我有没有试着取"—— 后者就是本仓反复出现的**假绿灯**。
    库里没有可试的号时返回 True(**不阻断**,与探针同口径)。

    ⚠️⚠️ **必须带对身份档**(2026-10-10,这里踩过一次):拿 **eink 身份铸的 token**
    配**安卓 App 的头**去问,一定回 `-2012` —— 那是"身份不配套",**不是 token 坏了**。
    我第一版就是从**库里旧凭据**读 profile,而此刻库里还是旧档,于是**刚铸出来的好 token
    被验活自己否掉**。⇒ 现在由**每条路各自声明**它要验的是哪个档:
    `refresh_via_http` 声明 `eink`、`refresh`(唤醒 App 那条)声明 `app`。
    """
    def _verify(token, vid) -> bool:
        from sqlalchemy import select

        from app.db.models import WechatBenchmark
        from app.services.weread_app_client import WereadAppClient

        try:
            bm = session.scalar(select(WechatBenchmark).where(
                WechatBenchmark.user_id == user_id,
                WechatBenchmark.weread_book_id != "").limit(1))
        except Exception:  # noqa: BLE001
            return True
        if bm is None:
            return True
        use = profile or str((load(session, user_id) or {}).get("profile") or "")
        WereadAppClient(str(token), str(vid), profile=use).articles(bm.weread_book_id)
        return True
    return _verify


def refresh_via_http(session: Session, user_id: int, *, verify=None) -> dict[str, Any]:
    """**纯 HTTP** 重取并写回(不需要模拟器)。返回 `{ok, reason?, ...}` —— **不抛异常**。

    与 `refresh()`(唤醒 App 重读账号库)是**两条并列的路**,这里**不碰 adb**。
    前提:凭据里已经存了 `deviceId`(一次性从模拟器读,见 `read_device_id_from_emulator`)。
    """
    from app.services.cookie_store import set_cookie

    cur = load(session, user_id)
    if not cur:
        return {"ok": False, "reason": "没配过 App 凭据"}
    device_id = str(cur.get("deviceId") or "")
    if not device_id:
        return {"ok": False, "reason": "凭据里没有 deviceId(需一次性从模拟器读:read_device_id_from_emulator)"}
    try:
        blob = mint_access_token_http(device_id, str(cur.get("refreshToken") or ""))
    except Exception as exc:  # noqa: BLE001 - 拿不到只是"这次没兜底",别把整轮带崩
        return {"ok": False, "reason": f"{type(exc).__name__}: {str(exc)[:140]}"}

    if verify is None:
        verify = verify_by_api(session, user_id, profile="eink")   # ★ 我铸的就是 eink 档
    try:
        if not verify(blob["accessToken"], blob["vid"] or cur.get("vid")):
            return {"ok": False, "reason": "铸出来的 token 验活失败(接口不认)"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"验活异常:{type(exc).__name__}: {str(exc)[:120]}"}

    payload = {**cur, **blob, "deviceId": device_id,
               "pulled_at": datetime.now().isoformat(" ", "seconds")}
    # ⚠️ **`refreshToken` 可能轮换** ⇒ `blob` 里带回来的那枚要**写回**(否则用一次就废,
    #    与迅雷那条「网页兑换会轮换 refresh_token 必须读回」是同一个教训)。
    try:
        set_cookie(session, user_id, PLATFORM, json.dumps(payload, ensure_ascii=False))
        session.commit()
    except Exception as exc:  # noqa: BLE001 - 写回失败要**说出来**,别当作成功
        return {"ok": False, "reason": f"写回凭据失败:{type(exc).__name__}: {str(exc)[:120]}"}
    logger.info("微信读书 App token 已**纯 HTTP** 续期(用户 %s,无需模拟器)", user_id)
    return {"ok": True, "via": "http", **blob}


def refresh_with_wake(session: Session, user_id: int, *, adb: str = "",
                      attempts: int = 3) -> dict[str, Any]:
    """**唤醒 App → 重取 → 立刻验活**,不行就重试。返回 `{ok, ...}`。

    ⚠️ 这是"阅读数自愈"的入口。原来的 `_reget` 只是"重取一次、拿到**同一个值**、再失败"
    —— 那是**空转**(见 quark_kouling 里同一条教训):它让"这条路全断了"在日志里
    长得像"自愈机制在正常工作"。**有验活的才叫自愈。**
    ⚠️ 全部尝试都失败才返回 `ok=False` —— 那时才该惊动人工。
    """
    last: dict[str, Any] = {"ok": False, "reason": "未尝试"}
    # ★★ ① **先试纯 HTTP**(2026-10-10 打通)—— **不需要模拟器**。
    #    这条路一通,模拟器对微信读书就**彻底退休**了;下面那段唤醒 App 只在
    #    (a) 凭据里还没有 deviceId(没做过一次性读取)或 (b) HTTP 这条路被拒时才走。
    http_out = refresh_via_http(session, user_id)   # 它自己按 eink 档验活
    if http_out.get("ok"):
        return http_out
    logger.info("纯 HTTP 续期未成(%s)—— 回落唤醒 App(需要模拟器)",
                str(http_out.get("reason"))[:90])
    # ② 回落:唤醒 App 重读账号库(**要模拟器开着**;没开就一定失败,这是常态不是故障)
    for i in range(max(1, attempts)):
        wake_app(adb)
        last = refresh(session, user_id, adb=adb)   # 它自己按 app 档验活
        if last.get("ok"):
            if i:
                logger.info("微信读书 App token 第 %d 次唤醒后取到有效值", i + 1)
            return last
        logger.info("第 %d 次唤醒+重取仍无效:%s", i + 1, str(last.get("reason"))[:80])
        time.sleep(3)
    return {"ok": False,
            "reason": f"两条路都没成 —— 纯 HTTP:{str(http_out.get('reason'))[:80]};"
                      f"唤醒 App:{str(last.get('reason'))[:80]}"}


def refresh(session: Session, user_id: int, *, adb: str = "", verify=None) -> dict[str, Any]:
    """**重取并写回** App 凭据。返回 `{ok, reason?, ...}` —— **不抛异常**。

    为什么不抛:它的调用点在监听轮里,**取不到 token 只是"这次没有兜底",
    不该把整轮监听带崩**。调用方看 `ok` 决定要不要继续用 App 路。

    `verify` 可注入一个 `(token, vid) -> bool` 的校验函数(默认走真实接口,
    测试注入桩避免联网)。
    """
    from app.services.cookie_store import set_cookie

    try:
        blob = pull_from_emulator(adb)
    except Exception as exc:  # noqa: BLE001 - 模拟器没开是常态,不是故障
        return {"ok": False, "reason": f"{type(exc).__name__}: {str(exc)[:120]}"}

    if verify is None:
        verify = verify_by_api(session, user_id, profile="app")   # ★ 这条路读的是 App 签发的
    try:
        if not verify(blob["accessToken"], blob["vid"]):
            return {"ok": False, "reason": "取到的新 token 验活失败", **blob}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"验活异常 {type(exc).__name__}", **blob}

    set_cookie(session, user_id, PLATFORM, json.dumps(blob, ensure_ascii=False))
    logger.info("App 侧凭据已重取并写回(用户 %s,vid=%s)", user_id, blob.get("vid"))
    return {"ok": True, **blob}
