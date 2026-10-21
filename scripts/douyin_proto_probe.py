"""抖音**纯协议探针**(2026-10-22)—— 只测协议，不写库、不推送、不转存。

## 什么时候用
- 想确认"现在协议到底能不能用"；
- 改过签名/请求头/参数后做一次受控回归；
- 想观察 `verify_check` 是不是又出现了。

## 纪律
- **每打一次都是一次风控输入**。默认只跑 **3 个词、间隔 15 秒**，别连发；
- 撞到 `verify_check` 会**自动进冷却**（`note_verify`），后续请求会直接拒发；
- 不写任何业务表；只看 `status_code` / `search_nil_info` / 命中词数。

用法:
    python scripts/douyin_proto_probe.py                 # 3 个词
    python scripts/douyin_proto_probe.py --words 1       # 只探一个词
    python scripts/douyin_proto_probe.py --gap 20        # 拉开间隔
    python scripts/douyin_proto_probe.py --kw 短剧资源 网盘资源
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_KWS = ["短剧资源", "网盘资源", "夸克网盘"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--words", type=int, default=3, help="最多跑几个词(默认 3)")
    ap.add_argument("--gap", type=float, default=15.0, help="词间隔秒数(默认 15)")
    ap.add_argument("--kw", nargs="*", default=None, help="自定义关键词")
    ap.add_argument("--user", type=int, default=1)
    args = ap.parse_args()

    words = (args.kw or DEFAULT_KWS)[: max(1, args.words)]
    from app.db.database import get_session_local, init_db
    from app.services import douyin_protocol_source as dps

    #: 临时改间隔 —— 只影响本进程，不改生产常量。
    dps._GAP = max(5.0, float(args.gap))
    init_db()
    db = get_session_local()()
    stats: dict = {}
    try:
        rows = dps.search(words, session=db, user_id=args.user, stats=stats)
        total = int(stats.get("kw_total") or 0)
        hit = int(stats.get("kw_hit") or 0)
        print(f"✓ 协议可用:词 {hit}/{total} 命中,共 {len(rows)} 条")
        for r in rows[:5]:
            print(f"  · {str(r.get('name') or '')[:16]} | {str(r.get('snippet') or '')[:60]}")
        if rows and hit * 2 < total:
            print("⚠️ 命中词不到一半 —— 形态与限流一致，别当成'正常结果少'")
        return 0
    except Exception as exc:  # noqa: BLE001 - 探针要把原因原样打出来
        kind = getattr(exc, "kind", "")
        print(f"✗ 协议不可用(kind={kind or '-'}):{str(exc)[:240]}")
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
