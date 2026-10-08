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
DEFAULT_ADB = r"D:\leidian\LDPlayer14\adb.exe"
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
    # ★ **`refreshToken` 也一起读**(2026-10-08):它是**长效**凭据,拿它就能
    # `POST https://i.weread.qq.com/login` **纯 HTTP 换新的 accessToken** ——
    # 也就是说模拟器**只需要偶尔开一次**(换/续 refreshToken),日常续期不再需要它。
    # 这条是查历史抓包(`data/_mitm_weread.txt`)发现的:`/login` 的请求体里
    # `refreshToken` 就是那个"长效→短效"的兑换凭据。
    return {"accessToken": str(row["accessToken"]), "vid": str(row["vid"]),
            "refreshToken": str(row.get("refreshToken") or ""),
            "refreshTokenExpired": row.get("refreshTokenExpired"),
            "userName": row.get("userName") or "",
            "pulled_at": datetime.now().isoformat(" ", "seconds")}


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


def verify_by_api(session: Session, user_id: int):
    """造一个 `(token, vid) -> bool` 的验活函数:**真拿一个号问一次 App 接口**。

    ⚠️ 关键在于**真的问了**。调用方要知道的是"这个 token 现在能不能用",
    不是"我有没有试着取"—— 后者就是本仓反复出现的**假绿灯**。
    库里没有可试的号时返回 True(**不阻断**,与探针同口径)。
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
        WereadAppClient(str(token), str(vid)).articles(bm.weread_book_id)   # 抛 = 验活失败
        return True
    return _verify


def refresh_with_wake(session: Session, user_id: int, *, adb: str = "",
                      attempts: int = 3) -> dict[str, Any]:
    """**唤醒 App → 重取 → 立刻验活**,不行就重试。返回 `{ok, ...}`。

    ⚠️ 这是"阅读数自愈"的入口。原来的 `_reget` 只是"重取一次、拿到**同一个值**、再失败"
    —— 那是**空转**(见 quark_kouling 里同一条教训):它让"这条路全断了"在日志里
    长得像"自愈机制在正常工作"。**有验活的才叫自愈。**
    ⚠️ 全部尝试都失败才返回 `ok=False` —— 那时才该惊动人工。
    """
    last: dict[str, Any] = {"ok": False, "reason": "未尝试"}
    for i in range(max(1, attempts)):
        wake_app(adb)
        last = refresh(session, user_id, adb=adb,
                       verify=verify_by_api(session, user_id))
        if last.get("ok"):
            if i:
                logger.info("微信读书 App token 第 %d 次唤醒后取到有效值", i + 1)
            return last
        logger.info("第 %d 次唤醒+重取仍无效:%s", i + 1, str(last.get("reason"))[:80])
        time.sleep(3)
    return {"ok": False,
            "reason": f"唤醒 App {attempts} 次后重取仍失效 —— 可能真需要人工打开/重登。"
                      f"最后原因:{last.get('reason')}"}


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

    if verify is not None:
        try:
            if not verify(blob["accessToken"], blob["vid"]):
                return {"ok": False, "reason": "取到的新 token 验活失败", **blob}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": f"验活异常 {type(exc).__name__}", **blob}

    set_cookie(session, user_id, PLATFORM, json.dumps(blob, ensure_ascii=False))
    logger.info("App 侧凭据已重取并写回(用户 %s,vid=%s)", user_id, blob.get("vid"))
    return {"ok": True, **blob}
