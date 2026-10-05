"""取微信读书 **App 侧**凭据(`accessToken` + `vid`)并写进库 —— 一条命令搞定(2026-10-05)。

**为什么要它**:App 接口 `i.weread.qq.com/book/articles` 是目前**唯一还能拿到精确阅读数**的路
(网页版 `/web/mp/articles` 已被账号级拦截 `-2041`)。它的鉴权是 App 自己的
`accessToken` + `vid` 两个头 —— 而这两个**只存在于 App 里**。

**做法**(与抓包结论一致,但不需要抓包):从**雷电模拟器**里把微信读书的账号库
`/data/data/com.tencent.weread/databases/WRAccount` 拉出来,读 `Account` 表的
`accessToken` / `vid`。

跑法:
    python scripts/weread_app_login.py                  # 取一次,写库,并验活
    python scripts/weread_app_login.py --adb "D:/leidian/LDPlayer14/adb.exe"

⚠️ **`accessToken` 会随 App 会话轮换**(实测 `3RRAf8Il` → `slJzpb3p` → `ALRPC8tY`),
所以这是**周期性**要做的事,不是一次性。验活失败时本脚本会明确说"该重取了"。

⚠️ **模拟器只需在做这件事时开着**。token 取到后,`WereadAppClient` 从**任何机器**都能直连
(实测:请求从本机 Windows 直接发出,与模拟器无关)。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_ADB = r"D:\leidian\LDPlayer14\adb.exe"
PKG = "com.tencent.weread"
DB_REL = "/data/data/com.tencent.weread/databases/WRAccount"
# 顺带取一个号做验活;取不到就只验"能拉到列表"
PROBE_BOOK = "MP_WXS_3902714095"


def _sh(adb: str, *args: str, timeout: int = 90) -> tuple[int, str]:
    p = subprocess.run([adb, *args], capture_output=True, timeout=timeout)
    out = (p.stdout + p.stderr).decode("utf-8", "replace").strip()
    return p.returncode, out


def _find_adb(explicit: str) -> str:
    for cand in (explicit, os.environ.get("ADB_PATH", ""), shutil.which("adb") or ""):
        if cand and Path(cand).exists():
            return cand
    raise SystemExit(f"找不到 adb。用 --adb 指定(默认试过 {DEFAULT_ADB})")


def _pull_account_db(adb: str, tmpdir: str) -> str:
    """把 App 的账号库(含 WAL)拉到本地。

    ⚠️ **必须连 `-wal` 一起拉**:token 的最新值在 WAL 里,只拉主库会读到**上一轮**的旧 token
    (实测踩过:主库给 `slJzpb3p`、带 WAL 才是当前值)。
    """
    subprocess.run([adb, "root"], capture_output=True, timeout=60)
    # 库里文件属 app 用户,先经 /sdcard 中转
    _sh(adb, "shell", f"cp {DB_REL} /sdcard/_wa.db; "
                      f"cp {DB_REL}-wal /sdcard/_wa.db-wal 2>/dev/null; "
                      f"cp {DB_REL}-shm /sdcard/_wa.db-shm 2>/dev/null; echo ok")
    local = os.path.join(tmpdir, "WRAccount")
    for src, dst in (("/sdcard/_wa.db", local),
                     ("/sdcard/_wa.db-wal", local + "-wal"),
                     ("/sdcard/_wa.db-shm", local + "-shm")):
        subprocess.run([adb, "pull", src, dst], capture_output=True, timeout=180)
    if not Path(local).exists():
        raise SystemExit("拉取账号库失败(模拟器没开?微信读书没装/没登录?)")
    return local


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--adb", default=DEFAULT_ADB)
    ap.add_argument("--user", type=int, default=0, help="租户 id(默认取第一个启用用户)")
    ap.add_argument("--no-verify", action="store_true", help="只取不验活")
    args = ap.parse_args()

    adb = _find_adb(args.adb)
    code, out = _sh(adb, "devices")
    if "device" not in out.replace("List of devices attached", "").strip()[:20]:
        raise SystemExit("没有已连接的模拟器。先开雷电(并把设置里的 ADB 打开),再跑本脚本。")

    with tempfile.TemporaryDirectory() as td:
        db = _pull_account_db(adb, td)
        con = sqlite3.connect(db)
        cols = [c[1] for c in con.execute("PRAGMA table_info(Account)")]
        rows = [dict(zip(cols, r)) for r in con.execute("SELECT * FROM Account")]
        con.close()

    rows = [r for r in rows if r.get("accessToken") and r.get("vid")]
    if not rows:
        raise SystemExit("账号库里没有有效的 accessToken/vid —— App 里是不是没登录?")
    row = rows[0]
    blob = {"accessToken": row["accessToken"], "vid": str(row["vid"]),
            "userName": row.get("userName") or "", "pulled_at": datetime.now().isoformat(" ", "seconds")}
    print(f"取到:{blob['userName']} vid={blob['vid']} token={blob['accessToken'][:4]}…(len={len(blob['accessToken'])})")

    if not args.no_verify:
        from app.services.weread_app_client import WereadAppClient, WereadAppError
        try:
            arts = WereadAppClient(blob["accessToken"], blob["vid"]).articles(PROBE_BOOK, count=5)
            print(f"验活 ✓ 拉到 {len(arts)} 篇" +
                  (f",首篇阅读数 = {arts[0]['read_num']}" if arts else ""))
        except WereadAppError as exc:
            print(f"验活 ✗ {exc}")
            print("  ⚠️ 若为登录态问题(-2010/-2012),请在模拟器里重新打开一次微信读书再跑本脚本。")
            return 1

    from sqlalchemy import select

    from app.db import get_session_local, init_db
    from app.db.models import User
    from app.services.cookie_store import set_cookie

    init_db()
    db2 = get_session_local()()
    try:
        uid = args.user or db2.scalar(select(User.id).where(User.enabled.is_(True)).order_by(User.id))
        if not uid:
            print("没有启用的用户"); return 1
        set_cookie(db2, uid, "weread_app", json.dumps(blob, ensure_ascii=False))
        print(f"=== ✓ 已加密写入 cookie_store(user={uid}, platform=weread_app) ===")
    finally:
        db2.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
