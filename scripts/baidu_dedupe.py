"""百度网盘**跨目录重复**清单 —— 只读,不删。

## 为什么只读
用户口径(2026-10-16):「**先做识别 + 清单**」。理由是**百度没有回收站:删了不可逆**
(`baidupan_transfer.delete_paths` 的文档里就写着「调用方必须自己确认路径」)。

⚠️ **本脚本没有 `--apply`,而且不是忘了** —— 删除开关要等你**看过一份真实清单**、
确认判据没有误判之后再开(与夸克那边"先开 `DRY_RUN` 跑几轮再关"同一条纪律,
以及"给错动作比不给动作更坏"那条教训)。

用法:
    python scripts/baidu_dedupe.py            # 扫一遍、打清单、写 data/pan_dedupe_baidu.json
    python scripts/baidu_dedupe.py --json     # 只打 JSON(给别的工具吃)
    python scripts/baidu_dedupe.py --top 40   # 明细多打几行
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="只打 JSON")
    ap.add_argument("--top", type=int, default=15, help="明细打前几组(默认 15)")
    ap.add_argument("--root", default="", help="扫描根(默认取配置,再退到 /redian百度转存)")
    a = ap.parse_args()

    from config.settings import get_settings

    from app.db import get_session_local
    from app.services import baidu_dup, pan_accounts
    from app.services.baidupan_transfer import BaiduPanClient

    settings = get_settings()
    db = get_session_local()()
    try:
        ck = (pan_accounts.cookie_for(db, "baidupan", user_id=1) or "").strip()
    finally:
        db.close()
    if not ck:
        print("❌ 取不到百度网盘凭据(Cookie 管理页里没配 baidupan)")
        return 2
    root = (a.root or getattr(settings, "pan_auto_clean_baidu_root", "") or "").strip() \
        or baidu_dup.DEFAULT_ROOT
    plan = baidu_dup.build_plan(
        BaiduPanClient(ck), roots=(root,),
        intro_dir=(getattr(settings, "pan_intro_baidu_dir", "") or ""))
    if a.json:
        print(json.dumps(plan, ensure_ascii=False, indent=1))
        return 0

    from pathlib import Path

    p = Path("data/pan_dedupe_baidu.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"root": root, **plan}, ensure_ascii=False, indent=1),
                 encoding="utf-8")
    print(f"扫描根 = {root}")
    print(f"**{baidu_dup.summary(plan)}**")
    print(f"清单已写 {p}(**只读:本脚本不删任何东西**)")
    print()
    print(f"最大的 {a.top} 组重复:")
    for g in plan["groups"][:a.top]:
        n = int(g["size"]) / (1 << 20)
        print(f"  {n:9.1f} MiB × {g['copies']} 份 | {g['name'][:46]}")
        print(f"        留着: {g['keep']}")
        for d in g["dirs"][1:4]:
            print(f"        重复: {d}")
    if plan["truncated"]:
        print("\n⚠️ **没列完**(撞了深度/文件数上限)⇒ 上面的数字**偏小**,不是全部。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
