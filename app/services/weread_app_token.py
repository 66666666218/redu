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
    return {"accessToken": str(row["accessToken"]), "vid": str(row["vid"]),
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
