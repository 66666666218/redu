"""夸克**删除**能力的安全探针:建一个临时文件夹 → 删它。**绝不碰真数据**。

## 为什么必须有这一步
`QuarkTransfer.delete_files` 的端点是**按公开协议写的,没在真数据上验证过**。
删除是不可逆的(进回收站还能捞,彻底删就没了)—— 所以**先证明它删得动、也删得准**,
再让它碰真资源。本脚本:
  1. 建一个名字带时间戳、一眼能认出的空文件夹;
  2. 调 `delete_files([fid])`(默认进回收站);
  3. 再搜一次,确认**它确实不在了**;
  4. 打印结论。失败也只影响那个临时文件夹。

用法:`python scripts/quark_delete_probe.py`
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import get_session_local  # noqa: E402
from app.services.cookie_store import get_cookie  # noqa: E402
from app.services.quark_transfer import QuarkTransfer  # noqa: E402


def main() -> int:
    db = get_session_local()()
    try:
        ck = (get_cookie(db, 1, "quark") or "").strip()
    finally:
        db.close()
    if not ck:
        print("✗ 没配夸克 Cookie")
        return 1
    qt = QuarkTransfer(ck)
    name = f"ZZ删除探针_{int(time.time())}"
    print(f"① 建临时文件夹:{name}")
    fid = qt._ensure_dir(f"/{name}")
    if not fid:
        print("✗ 建目录失败,停止")
        return 1
    print(f"   fid = {fid}")
    hits = [x for x in qt.search_files(name, size=10) if name in str(x.get("file_name"))]
    print(f"   搜索确认存在:{len(hits)} 条")

    print("② 删它(action_type=2 → 回收站,可恢复)")
    res = qt.delete_files([fid], to_recycle=True)
    print("   ", res)

    print("③ 再搜一次,确认不在了")
    time.sleep(2)
    left = [x for x in qt.search_files(name, size=10) if name in str(x.get("file_name"))]
    print(f"   还搜得到 {len(left)} 条")
    ok = res.get("ok") and not left
    print("⇒", "✅ 删除能力可用(且只删掉了那一个临时文件夹)" if ok
          else "❌ 不可用或没删掉 —— **别让它碰真资源**")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
