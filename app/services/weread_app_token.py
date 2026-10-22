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
DEFAULT_LDCONSOLE = r"D:\leidian\LDPlayer14\ldconsole.exe"

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


def _run(adb: str, *args: str, timeout: int = 120, serial: str = "") -> str:
    """跑一次 adb。`serial` 非空时插 `-s <serial>`(**多开时必须指定**,见 `pull_from_emulator`)。"""
    prefix = ("-s", serial) if serial else ()
    p = subprocess.run([adb, *prefix, *args], capture_output=True, timeout=timeout)
    return (p.stdout + p.stderr).decode("utf-8", "replace").strip()


def platform_for(account_key: str) -> str:
    """账号 key(`weread` / `weread#2`)→ 它的 **App 凭据** key(`weread_app` / `weread_app#2`)。

    ⚠️ 网页 cookie(`weread#N`)与 App token(`weread_app#N`)是**两套独立鉴权**
    (见 `cookie_store.PLATFORMS`),但**同属一个账号** —— 这个映射就是它俩的对应关系。
    """
    key = str(account_key or "").strip() or "weread"
    if "#" not in key:
        return PLATFORM
    return f"{PLATFORM}#{key.split('#', 1)[1]}"


def emulator_ready(adb: str = "", serial: str = "") -> bool:
    """模拟器现在能不能读到账号库(只看 `adb devices`)。**不抛异常**。

    为什么会需要这个判断:2026-10-18 实测 —— **只有 App 自己铸的 token** 能打通
    `i.weread.qq.com/book/articles`;纯 HTTP 铸出来的那枚(身份档 `eink`)打同一个端点
    仍然 `-2041`。而 `refresh_with_wake` 原来**先试 HTTP**,于是"HTTP 成功"就把
    唯一真能用的那条路挡在后面 ⇒ 生产里每轮 `app_blocked` 十几发、阅读数全 0。

    `serial` 非空时**只认这一台**(多开:一个号一个实例,别拿到别人的库)。
    """
    exe = _find_adb(adb)
    if not exe:
        return False
    try:
        if serial:
            out = _run(exe, "devices", timeout=15)
            return serial in out and "device" in out
        dev = _run(exe, "devices", timeout=15)
    except Exception:  # noqa: BLE001 - adb 卡住/没装都算"不在"
        return False
    return "emulator" in dev or "127.0.0.1" in dev


def _ldconsole(explicit: str = "") -> str:
    """找雷电的 `ldconsole.exe`(启停实例用)。找不到返回空串。"""
    for cand in (explicit, os.getenv("WEREAD_LDCONSOLE_PATH", ""), DEFAULT_LDCONSOLE):
        if cand and Path(cand).exists():
            return cand
    return ""


def emulator_index(serial: str = "") -> int | None:
    """`emulator-5554` → 0、`emulator-5556` → 1 ……(雷电/安卓模拟器的端口规律)。

    只用于"按需启停"时把 `adb` 的 serial 翻成 `ldconsole --index`。
    不是这个形状 → 返回 None(调用方按"不知道该启哪台"处理,不猜)。
    """
    import re

    m = re.search(r"emulator-(\d+)", serial or "")
    if not m:
        return None
    port = int(m.group(1))
    return (port - 5554) // 2 if port >= 5554 else None


def ensure_emulator(serial: str = "", *, adb: str = "", timeout: float = 120.0) -> bool:
    """模拟器不在时**按需拉起来**(仅当 `weread_emulator_autostart` 打开)。

    ⚠️ 为什么可以"不用一直开着"(2026-10-22):能用的 token 只有 App 签发的,
    但**监听轮本身不碰模拟器** —— 它只读库里存好的 token。模拟器只需要在**续期那一刻**
    开一会儿:唤醒 App 刷新会话 → 读出 token → 就可以关。
    ⇒ 本函数在续期时把实例拉起来,`quit_emulator` 在取到 token 后关掉;
    一天只开 4 次(03:52 / 07:52 / 13:52 / 19:52),每次约 1 分钟。

    返回是否就绪;**关闭开关/找不到 ldconsole/启不来一律返回 False**,
    调用方回落纯 HTTP(与"模拟器本来就没开"同一条路)。
    """
    if emulator_ready(adb, serial=serial):
        return True
    try:
        from config.settings import get_settings

        st = get_settings()
        if not getattr(st, "weread_emulator_autostart", False):
            return False
        exe = _ldconsole(str(getattr(st, "weread_ldconsole_path", "") or ""))
        idx = emulator_index(serial)
        if not exe or idx is None:
            logger.info("按需启动模拟器跳过:找不到 ldconsole 或实例号(serial=%s)", serial or "-")
            return False
        logger.info("按需启动模拟器:index=%s(serial=%s)", idx, serial or "-")
        subprocess.run([exe, "launch", "--index", str(idx)],
                       capture_output=True, timeout=60)
        deadline = time.time() + max(10.0, timeout)
        while time.time() < deadline:
            time.sleep(3)
            if emulator_ready(adb, serial=serial):
                return True
        logger.warning("按需启动模拟器超时(%ss):index=%s", timeout, idx)
    except Exception:  # noqa: BLE001 - 启停失败只影响"这次能不能重取"
        logger.exception("按需启动模拟器失败")
    return False


def quit_emulator(serial: str = "", *, adb: str = "") -> bool:
    """关掉**我们自己拉起来**的实例(仅当 `weread_emulator_quit_after`)。

    ⚠️ 只该在 `ensure_emulator` 确实**由本次拉起**时调用 —— 用户本来就开着的实例不要动。
    """
    try:
        from config.settings import get_settings

        st = get_settings()
        if not getattr(st, "weread_emulator_quit_after", True):
            return False
        exe = _ldconsole(str(getattr(st, "weread_ldconsole_path", "") or ""))
        idx = emulator_index(serial)
        if not exe or idx is None:
            return False
        subprocess.run([exe, "quit", "--index", str(idx)],
                       capture_output=True, timeout=60)
        logger.info("模拟器已按需关闭:index=%s", idx)
        return True
    except Exception:  # noqa: BLE001 - 关不掉不影响本次已取到的 token
        logger.exception("关闭模拟器失败")
        return False


def pull_from_emulator(adb: str = "", tmpdir: str | None = None,
                       serial: str = "") -> dict[str, Any]:
    """从雷电模拟器里把 `accessToken` / `vid` 读出来。**失败抛异常**,别返回空。

    ⚠️ **必须连 `-wal` 一起拉**:token 的最新值在 WAL 里,只拉主库会读到**上一轮**的旧值
    (实测踩过:主库给 `slJzpb3p`、带上 WAL 才是当前值)。

    ⚠️ **多开时必须给 `serial`**(`emulator-5554` / `emulator-5556` / …):
    不给就"连上哪台读哪台",而**两台里是不同账号** ⇒ 会把 A 的 token 当成 B 的写进库。
    """
    exe = _find_adb(adb)
    if not exe:
        raise RuntimeError(f"找不到 adb(试过 {DEFAULT_ADB}、$WEREAD_ADB_PATH、PATH)")
    dev = _run(exe, "devices")
    if serial:
        if serial not in dev:
            raise RuntimeError(f"没有已连接的实例 {serial}(`adb devices` 里没它)")
    elif "emulator" not in dev and "127.0.0.1" not in dev:
        raise RuntimeError("没有已连接的模拟器 —— 需要开雷电(并打开设置里的 ADB 开关)")
    _run(exe, "root", timeout=60, serial=serial)
    _run(exe, "shell",
         f"cp {DB_REL} /sdcard/_wa.db; cp {DB_REL}-wal /sdcard/_wa.db-wal 2>/dev/null; "
         f"cp {DB_REL}-shm /sdcard/_wa.db-shm 2>/dev/null; echo ok", serial=serial)

    own = tmpdir is None
    td = tmpdir or tempfile.mkdtemp(prefix="wracc_")
    try:
        local = os.path.join(td, "WRAccount")
        for src, dst in (("/sdcard/_wa.db", local),
                         ("/sdcard/_wa.db-wal", local + "-wal"),
                         ("/sdcard/_wa.db-shm", local + "-shm")):
            subprocess.run([exe, *(("-s", serial) if serial else ()), "pull", src, dst],
                           capture_output=True, timeout=180)
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
    #: ★ **deviceId 一起带上**(2026-10-22):`refresh_via_http` 铸 token 必须用它。
    #: 之前这里不存 ⇒ 用 App 凭据覆盖旧值后,纯 HTTP 路直接因"缺 deviceId"不可用,
    #: 只能回头开模拟器。读它只需一次(读不到不影响本次 App token,只是 HTTP 回落不可用)。
    try:
        device_id = read_device_id_from_emulator(adb, serial=serial)
    except Exception:  # noqa: BLE001
        logger.debug("读 deviceId 失败(本次仅影响纯 HTTP 回落)", exc_info=True)
        device_id = ""
    return {"accessToken": str(row["accessToken"]), "vid": str(row["vid"]),
            "refreshToken": str(row.get("refreshToken") or ""),
            "refreshTokenExpired": row.get("refreshTokenExpired"),
            "userName": row.get("userName") or "",
            #: 这枚 token 是**安卓 App** 签发的 ⇒ 调用时要用安卓 App 的身份头
            #: (与 HTTP 铸出来的 eink token 不能混用,见 `EINK_HEADERS` 上面的实测)
            "profile": "app",
            #: ★ **纯 HTTP 续期要用的设备号**(与上面这枚 token 无关,但同一个 App 账号)
            "deviceId": device_id,
            #: ★ **这枚凭据属于哪台模拟器**(2026-10-18 多开):续期时按它找对实例,
            #: 否则"连上哪台读哪台"会把 A 的 token 写到 B 的凭据里。
            "adb_serial": serial or "",
            "pulled_at": datetime.now().isoformat(" ", "seconds")}


def read_device_id_from_emulator(adb: str = "", serial: str = "") -> str:
    """从模拟器 `shared_prefs/device.xml` 读 `deviceid`(实测 **38 位数字**)。

    ⚠️ **只需读一次**:拿到之后纯 HTTP 续期就不再需要模拟器了。
    ⚠️ 记忆里曾把它记成「38 位 hex」——**不准**,它是纯数字(实测
    `35165432967079520699752786780101783056`)。
    ⚠️ **多开时必须给 `serial`**(和 `pull_from_emulator` 同一条纪律)。
    """
    import re

    exe = _find_adb(adb)
    if not exe:
        raise RuntimeError(f"找不到 adb(试过 {DEFAULT_ADB}、$WEREAD_ADB_PATH、PATH)")
    _run(exe, "root", timeout=60, serial=serial)
    txt = _run(exe, "shell", f"cat {DEVICE_XML_REL}", timeout=60, serial=serial)
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


def app_keys(session: Session, user_id: int) -> list[str]:
    """已配置的 **App 凭据** key,顺序稳定:`weread_app` → `weread_app#2` → …。

    多开用(一个微信读书账号 = 一台雷电实例 = 一枚 App 凭据)。
    ⚠️ 定时续期与监听轮**必须用同一份枚举** —— 两处各写一遍迟早飘一个
    (2026-10-18:定时续期只续了基础那枚,第二个号的 token 会自己烂掉)。
    """
    from sqlalchemy import or_, select

    from app.db.models import UserCookie

    try:
        rows = session.execute(select(UserCookie.platform).where(
            UserCookie.user_id == user_id,
            or_(UserCookie.platform == PLATFORM,
                UserCookie.platform.like(PLATFORM + "#%")))).scalars().all()
    except Exception:  # noqa: BLE001 - 读不到就当只有基础那枚
        return [PLATFORM]

    def _order(p: str) -> tuple[int, str]:
        suffix = p.split("#", 1)[1] if "#" in p else "1"
        return (int(suffix) if suffix.isdigit() else 999, p)

    return sorted(set(rows), key=_order) or [PLATFORM]


def load(session: Session, user_id: int, platform: str = PLATFORM) -> dict[str, Any] | None:
    """读已存的 App 凭据(JSON);没有/坏了都返回 `None` 并**留一条 warning**。

    `platform` 默认基础账号(`weread_app`);多开时传 `weread_app#2`(见 `platform_for`)。
    """
    from app.services.cookie_store import get_cookie

    try:
        raw = get_cookie(session, user_id, platform)
        if not raw:
            return None
        return json.loads(raw)
    except Exception as exc:  # noqa: BLE001 - 坏凭据 = 当没配,但要说出来
        logger.warning("App 侧凭据不可用(%s):%s", platform, str(exc)[:120])
        return None


#: 微信读书 App 的启动组件 —— 唤醒它用。
#: ⚠️ 实测 `am start -a MAIN -c LAUNCHER <pkg>` 在这个镜像上**会被拒**
#: ("unable to resolve Intent"),**必须用显式组件名**。
#: 取自 `adb shell cmd package resolve-activity --brief com.tencent.weread`。
WEREAD_ACT = "com.tencent.weread/.LauncherActivity"


def wake_app(adb: str = "", wait: float = 8.0, serial: str = "") -> bool:
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
        subprocess.run([exe, *(("-s", serial) if serial else ()),
                        "shell", "am", "start", "-n", WEREAD_ACT],
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
        try:
            WereadAppClient(str(token), str(vid), profile=use).articles(bm.weread_book_id)
        except Exception as exc:  # noqa: BLE001
            # ★★★ **额度错误 ≠ token 坏**(2026-10-13 实测定性,别再合并处理)
            # 接口的顺序是**先校认证、后校额度** ⇒ 能走到额度这一步,说明**认证已经过了**。
            # 同一本书、同一个号,只换 token:
            #     · 库里那枚旧 token ⇒ `-2012 登录超时`(**认证失败**)
            #     · 刚铸出来的新 token ⇒ `-2041`(**额度**)
            # 两者是不同层的错误 —— 所以 `-2041` 恰恰证明**这枚 token 是好的**。
            # ⚠️ 不区分会怎样(2026-10-11~13 实际发生的):
            #   好 token 被**验活自己否掉** ⇒ `refresh_via_http` 报"未成" ⇒ 永远写不回库
            #   ⇒ 库里那枚死 token 永远在用 ⇒ 监听每轮回 `-2012` ⇒ 推
            #   「App 登录态失效」告警 ⇒ 人跑去开雷电模拟器(**白跑**,token 根本没坏)。
            # ⇒ 与 `verify_by_api` 原有那条同口径:拿不准时**不阻断**
            #   (库里没有可试的号时就是 return True),但**要留痕**,不做静默绿灯。
            from app.services.weread_budget import is_quota_error

            if is_quota_error(exc):
                logger.warning("验活撞「额度」错误(%s)—— 认证已通过,**不据此判 token 坏**(%s)",
                               str(exc)[:80], bm.weread_book_id)
                return True
            raise
        return True
    return _verify


def refresh_via_http(session: Session, user_id: int, *, verify=None,
                     platform: str = PLATFORM) -> dict[str, Any]:
    """**纯 HTTP** 重取并写回(不需要模拟器)。返回 `{ok, reason?, ...}` —— **不抛异常**。

    与 `refresh()`(唤醒 App 重读账号库)是**两条并列的路**,这里**不碰 adb**。
    前提:凭据里已经存了 `deviceId`(一次性从模拟器读,见 `read_device_id_from_emulator`)。
    """
    from app.services.cookie_store import set_cookie

    cur = load(session, user_id, platform=platform)
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
        set_cookie(session, user_id, platform, json.dumps(payload, ensure_ascii=False))
        session.commit()
    except Exception as exc:  # noqa: BLE001 - 写回失败要**说出来**,别当作成功
        return {"ok": False, "reason": f"写回凭据失败:{type(exc).__name__}: {str(exc)[:120]}"}
    logger.info("微信读书 App token 已**纯 HTTP** 续期(用户 %s,无需模拟器)", user_id)
    return {"ok": True, "via": "http", **blob}


def refresh_with_wake(session: Session, user_id: int, *, adb: str = "",
                      attempts: int = 3, prefer_emulator: bool | None = None,
                      serial: str = "", platform: str = PLATFORM) -> dict[str, Any]:
    """**唤醒 App → 重取 → 立刻验活**,不行就重试。返回 `{ok, ...}`。

    ⚠️ 这是"阅读数自愈"的入口。原来的 `_reget` 只是"重取一次、拿到**同一个值**、再失败"
    —— 那是**空转**(见 quark_kouling 里同一条教训):它让"这条路全断了"在日志里
    长得像"自愈机制在正常工作"。**有验活的才叫自愈。**
    ⚠️ 全部尝试都失败才返回 `ok=False` —— 那时才该惊动人工。
    """
    last: dict[str, Any] = {"ok": False, "reason": "未尝试"}
    # ★★★ **顺序在 2026-10-22 17:50 二次定案:模拟器 App token 优先,HTTP 只做兜底**。
    #
    # 当天 00:40 曾把顺序改成"HTTP 优先、模拟器只回落",理由是"19:52 铸的 token 回 -2012
    # 是过期,不是 HTTP 不能用"。那个理由只证明了**认证**能过,**没有证明能取数** ——
    # 17:45 的干净对照(同一分钟、同一本书、两个身份档):
    #
    #     纯 HTTP 现铸 eink token + eink 头  ⇒ `-2041`(恒定,换新铸也一样)
    #     模拟器里 App 签发 token  + app 头   ⇒ **97 篇 / 93 篇带精确读数**
    #
    # ⇒ `-2041` 在 eink 档上是**身份档不被接受**,不是"额度被打光"。白天所有轮次
    #   (08:04 / 14:05 / 17:20 / 17:42)全 `-2041`,正是因为这半天库里的凭据是 eink 档。
    # ⚠️ **别再按"额度"处理这条**:加预算只能少发几次,拿不到读数还是拿不到。
    #   eink 档只在**模拟器起不来**时兜底(它至少能证明认证通过,能触续期);
    #   一旦取了 App token,就以 App 档为准(`profile="app"`)。
    http_reason = "未尝试"
    emu_online = False
    started_here = False
    if prefer_emulator is not False:
        #: ★ **按需启停**:模拟器不必一直开着,续期这一刻拉起来、取到就关。
        was_ready = emulator_ready(adb, serial=serial)
        emu_online = was_ready or ensure_emulator(serial, adb=adb)
        started_here = emu_online and not was_ready
    if emu_online:
        for i in range(max(1, attempts)):
            wake_app(adb, serial=serial)
            last = refresh(session, user_id, adb=adb, serial=serial,
                           platform=platform)   # 它自己按 app 档验活
            if last.get("ok"):
                if started_here:
                    quit_emulator(serial, adb=adb)
                if i:
                    logger.info("微信读书 App token 第 %d 次唤醒后取到有效值", i + 1)
                return last
            logger.info("第 %d 次唤醒+重取仍无效:%s", i + 1, str(last.get("reason"))[:80])
            time.sleep(3)
        if started_here:
            quit_emulator(serial, adb=adb)
        if prefer_emulator is True:
            http_reason = "未尝试(只走 App 那一路)"
    else:
        last = {"ok": False, "reason": "模拟器不在"}
    #: 模拟器取不到(没装/起不来/App 未登录)才回落纯 HTTP。
    #: ⚠️ 这条路铸出来的是 **eink 档**:能过认证,但列表接口实测恒 `-2041`
    #: (见上面的对照)。**不要**因为它 ok=True 就以为阅读数恢复了。
    #: `prefer_emulator=False` = 明确只走 HTTP(测试/特殊场景),行为与旧版一致。
    if prefer_emulator is not True:
        http_out = refresh_via_http(session, user_id, platform=platform)  # 自己按 eink 档验活
        if http_out.get("ok"):
            logger.warning("本次只有 **eink 档** token(模拟器不可用)—— 认证可用,"
                           "但列表接口实测会回 -2041,精确阅读数取不到;"
                           "下次续期必须在模拟器可用时重取 App token")
            return http_out
        http_reason = str(http_out.get("reason"))[:60]
    return {"ok": False,
            # ⚠️ **全失败时必须给出"人要做的那一步"**(这条被 `test_全失败才报错并给出人工步骤` 守着):
            #    我 2026-10-11 加纯 HTTP 那条路时,把消息改成了"两条路都没成 —— …",
            #    **信息更全了、却把"该人工做什么"弄丢了** —— 而那才是这条消息存在的理由。
            "reason": f"两条路都没成(App:{str(last.get('reason'))[:60]};"
                      f"纯 HTTP:{http_reason})—— **需要人工**:"
                      f"开一次雷电模拟器、在微信读书 App 里确认登录态(必要时重登),"
                      f"再跑 `scripts/weread_app_login.py --key <账号> --serial <实例>` 重取凭据"}


def refresh(session: Session, user_id: int, *, adb: str = "", verify=None,
            serial: str = "", platform: str = PLATFORM) -> dict[str, Any]:
    """**重取并写回** App 凭据。返回 `{ok, reason?, ...}` —— **不抛异常**。

    为什么不抛:它的调用点在监听轮里,**取不到 token 只是"这次没有兜底",
    不该把整轮监听带崩**。调用方看 `ok` 决定要不要继续用 App 路。

    `verify` 可注入一个 `(token, vid) -> bool` 的校验函数(默认走真实接口,
    测试注入桩避免联网)。

    `serial` / `platform` 是**多开**用的(2026-10-18):每个号一个雷电实例,
    `platform='weread_app#2'` 的凭据只从 `serial='emulator-5556'` 读。
    """
    from app.services.cookie_store import set_cookie

    try:
        blob = pull_from_emulator(adb, serial=serial)
    except Exception as exc:  # noqa: BLE001 - 模拟器没开是常态,不是故障
        return {"ok": False, "reason": f"{type(exc).__name__}: {str(exc)[:120]}"}

    if verify is None:
        verify = verify_by_api(session, user_id, profile="app")   # ★ 这条路读的是 App 签发的
    try:
        if not verify(blob["accessToken"], blob["vid"]):
            return {"ok": False, "reason": "取到的新 token 验活失败", **blob}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"验活异常 {type(exc).__name__}", **blob}

    set_cookie(session, user_id, platform, json.dumps(blob, ensure_ascii=False))
    logger.info("App 侧凭据已重取并写回(用户 %s,%s vid=%s)", user_id, platform, blob.get("vid"))
    return {"ok": True, **blob}
