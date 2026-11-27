"""分批激活 2026-11-26 回填救回的对标号(每次 ≤15 个,一批隔一轮监听)。

## 为什么不一次全开(2026-11-26 实测)
51 个救回号**都不在微信读书书架**(书架水位 142 条命中 0)⇒ 书架闸门给 tier=0、
列表记号也是 0 ⇒ 新激活的号**第一轮必排在队头**。列表预算只有
`reads_budget = 20 × len(app_client) = 40`/轮,模拟一次全开:第 1 轮 6 个、
**第 2 轮 40 个新号占满全部预算**,现有高产号连续两轮拿不到阅读数。
≤15/批 ⇒ 每轮新号最多占 15,现有号仍 ≥25;拿到列表记号后回落正常轮转。

## 节奏怎么保证
状态存 `system_config[backfill_activation_state]`(上次激活时刻)。每次运行:
  1. 上一批激活之后**没有跑过一轮成功监听** ⇒ 本轮不动(批的第一轮才占队头;
     没隔轮就连开两批 = 单轮 30 个,破坏预算保护);
  2. 近 24h 无成功监听(链路死了)⇒ 不动,别在故障期添乱;
  3. 通过 ⇒ 激活池(created_at 最新优先)的前 15 个。

## ⚠️ 激活时把 `created_at` 重置为现在 —— 不是随手刷新
死号清理(`retire_dormant_benchmarks`)对「有源且有过答案」的号按
`last_item_at < cutoff(7天)` 判死。这些号 `created_at` 在 10-01~11-18(激活前
早就满 7 天),而激活后它们**立刻开始被问**:第一轮 cover 答上但没新文 ⇒
miss_count=1 ⇒ 次日 05:30 就会被停 —— 那是"监测满 7 天",实际只监测了 1 天。
重置 created_at = 从激活时刻起算 7 天宽限,符合用户原口径
(「一星期没发文就取消监控」—— 从开始监控那天数起)。

## 用法
    python scripts/activate_backfilled.py            # 只报告(池/闸/下批名单)
    python scripts/activate_backfilled.py --apply    # 真激活下一批

⚠️ 别并发跑(没有加锁;节奏闸本身幂等,双跑最坏多激活一批)。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import select  # noqa: E402

from app.db import get_session_local  # noqa: E402
from app.db.models import RunRecord, SystemConfig, WechatBenchmark  # noqa: E402

STATE_KEY = "backfill_activation_state"
BATCH_SIZE = 15
LISTEN_KIND = "wechat_listen"


def _pool(db) -> list[WechatBenchmark]:
    """待激活池:**这次回填救回的那批** —— active=False、有源、`miss_count=0` 且
    `last_item_at` 为空(=「从未答上」= 误杀签名,2026-11-26 实测 51 个)。

    ⚠️ 不能只按「active=False 且有 biz」选:那会把**正常停用**的老号也捞回来 ——
    它们 miss_count ≥ 1 / last_item_at 有值 = 被问过、确认过停更,复活它们等于
    把死号重新灌进每轮监听(实测全库 inactive+有源 = 268,其中误杀候选只有 51)。
    """
    return list(db.scalars(select(WechatBenchmark).where(
        WechatBenchmark.active.is_(False),
        WechatBenchmark.biz.like("MP_WXS_%"),
        WechatBenchmark.miss_count == 0,
        WechatBenchmark.last_item_at.is_(None))
        .order_by(WechatBenchmark.created_at.desc(), WechatBenchmark.id)).all())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="真激活(默认只报告)")
    args = ap.parse_args()

    # ⚠️ 门面先加载:子模块直接 import 会撞循环依赖
    from app.services import wechat_monitor as wm  # noqa: F401

    db = get_session_local()()
    try:
        pool = _pool(db)
        if not pool:
            print("待激活池为空 —— 全部已激活,任务结束。")
            return 0

        state_row = db.get(SystemConfig, STATE_KEY)
        state = json.loads(state_row.value) if state_row and state_row.value else {}
        last_at = None
        if state.get("at"):
            try:
                last_at = datetime.fromisoformat(state["at"])
            except ValueError:
                last_at = None

        # 闸 1:上一批激活后要跑过至少一轮成功监听(节奏:一批隔一轮)
        if last_at is not None:
            rounds = db.scalar(select(RunRecord.id).where(
                RunRecord.kind == LISTEN_KIND,
                RunRecord.status.in_(("success", "partial")),
                RunRecord.started_at > last_at).limit(1))
            if rounds is None:
                print(f"上一批激活于 {state['at']},之后还没有成功监听轮 —— 本轮不动。")
                print(f"(当前池 {len(pool)} 个;下一批名单见 dry-run 输出)")
                _print_next(pool)
                return 0

        # 闸 2:链路安全阀 —— 近 24h 无成功监听就别添乱
        day_ago = datetime.now() - timedelta(hours=24)
        alive = db.scalar(select(RunRecord.id).where(
            RunRecord.kind == LISTEN_KIND,
            RunRecord.status.in_(("success", "partial")),
            RunRecord.started_at >= day_ago).limit(1))
        if alive is None:
            print("近 24h 无成功监听(疑似链路故障)—— 不激活,先修链路。")
            return 0

        batch = pool[:BATCH_SIZE]
        print(f"池 {len(pool)} 个,本批激活 {len(batch)} 个({'只报告' if not args.apply else '**真激活**'}):")
        now = datetime.now()
        for b in batch:
            age = (now - b.created_at).days
            print(f"  {'+' if args.apply else '·'} [{b.id}] {b.nickname}  (收录 {b.created_at:%m-%d},已 {age} 天)")
        if not args.apply:
            print(f"\n[dry-run] 什么都没写;剩余 {len(pool) - len(batch)} 个等下一轮。加 --apply 真激活")
            return 0

        for b in batch:
            b.active = True
            # created_at 重置 = 死号清理的 7 天宽限从激活时刻起算(理由见文件头)
            b.created_at = now
            b.note = f"{(b.note or '').strip()} 激活{now:%m-%d}".strip()[:255]
        state_row = db.get(SystemConfig, STATE_KEY)
        value = json.dumps({"at": now.isoformat(timespec="seconds")}, ensure_ascii=False)
        if state_row is None:
            db.add(SystemConfig(key=STATE_KEY, value=value))
        else:
            state_row.value = value
        db.commit()
        print(f"✅ 已激活 {len(batch)} 个;剩余 {len(pool) - len(batch)} 个等下一轮监听后再开。")
        return 0
    finally:
        db.close()


def _print_next(pool: list[WechatBenchmark]) -> None:
    batch = pool[:BATCH_SIZE]
    print("下一批名单(created_at 最新优先):")
    for b in batch:
        print(f"  [{b.id}] {b.nickname}  (收录 {b.created_at:%m-%d})")


if __name__ == "__main__":
    raise SystemExit(main())
