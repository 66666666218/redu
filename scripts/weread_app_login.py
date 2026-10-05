"""取微信读书 **App 侧**凭据(`accessToken` + `vid`)并写进库 —— 薄 CLI 包装(2026-10-05)。

**核心逻辑在 `app/services/weread_app_token.py`**,不在本脚本里 ——
服务层不能依赖 `scripts/`(分层要求;而且 Docker 镜像根本不 COPY `scripts/`),
而**监听轮里的自愈**要调同一段逻辑(`WereadAppClient.on_auth_error`)。

**为什么要它**:App 接口 `i.weread.qq.com/book/articles` 是目前**唯一稳定能拿到精确阅读数**的路
(网页版 `/web/mp/articles` 会被账号级拦截 `-2041`)。它的鉴权是 App 自己的 `accessToken` + `vid`,
而这两个**只存在于 App 里**。

跑法:
    python scripts/weread_app_login.py                  # 取一次,写库,并验活
    python scripts/weread_app_login.py --adb "D:/leidian/LDPlayer14/adb.exe"

⚠️ **`accessToken` 会随 App 会话轮换** ⇒ 这是**周期性**要做的事。
不过现在**不必靠人记得**:`WereadAppClient` 遇到登录态错误会**自动重取并重试一次**
(`weread_app_token.refresh`)。本脚本主要用于**首次配置**和**手动排障**。

⚠️ **取 token 需要雷电模拟器开着**(token 只在 App 的账号库里);
取到之后**任何机器都能直连**,日常调用与模拟器无关。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROBE_BOOK = "MP_WXS_3902714095"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--adb", default=os.getenv("WEREAD_ADB_PATH", ""))
    ap.add_argument("--user", type=int, default=0, help="租户 id(默认取第一个启用用户)")
    ap.add_argument("--no-verify", action="store_true", help="只取不验活")
    args = ap.parse_args()

    from sqlalchemy import select

    from app.db import get_session_local, init_db
    from app.db.models import User
    from app.services import weread_app_token as wat

    init_db()
    db = get_session_local()()
    try:
        uid = args.user or db.scalar(select(User.id).where(User.enabled.is_(True)).order_by(User.id))
        if not uid:
            print("没有启用的用户")
            return 1

        verify_fn = None
        if not args.no_verify:
            from app.services.weread_app_client import WereadAppClient

            def _probe(token: str, vid: str) -> bool:
                arts = WereadAppClient(token, vid).articles(PROBE_BOOK, count=5)
                print(f"验活 ✓ 拉到 {len(arts)} 篇" +
                      (f",首篇阅读数 = {arts[0]['read_num']}" if arts else ""))
                return True

            verify_fn = _probe

        out = wat.refresh(db, uid, adb=args.adb, verify=verify_fn)
        if not out.get("ok"):
            print(f"✗ 取凭据失败:{out.get('reason')}")
            print("  · 模拟器没开?先开雷电,并把设置里的 ADB 开关打开")
            print("  · App 里没登录?先打开一次微信读书")
            return 1
        print(f"取到:{out.get('userName')} vid={out['vid']} "
              f"token={out['accessToken'][:4]}…(len={len(out['accessToken'])})")
        print(f"=== ✓ 已加密写入 cookie_store(user={uid}, platform=weread_app) ===")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
