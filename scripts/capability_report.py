"""能力清单的**活数据**部分:每条能力"上次真跑通是什么时候"(2026-10-04)。

用法:
    python scripts/capability_report.py

**为什么拆成两半**:`doc/能力清单.md` 里是**人工维护的语义**(能力是什么、入口在哪、怎么触发、
有没有测试 —— 这些代码推不出来);而"**上次真跑通**"必须**从库里现取**,写进文档必然过期。
所以这里只打印活的那一列(顺带附上最直接的判据来源,方便回查)。

判据优先级:`job_heartbeats`(作业级,最准)→ `runs`(链路级)→ 业务表的最近写入时间(数据级)。
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# (能力, 作业id 或 None, 运行记录的 kind 或 None, 兜底:业务表+时间列)
# 三者**取得到的最靠前的一个**当"上次真跑通"。
CAPS: list[tuple[str, str | None, str | None, tuple[str, str] | None]] = [
    ("公众号监听 → 转存 → 推卡", "wechat_collect_tick", "wechat_listen", None),
    ("公众号候选号发现", "wechat_candidates", "wechat_candidates", None),
    ("抖音《口令》线索 → 解析 → 转存/加群", "douyin_leads", "douyin_leads", None),
    ("公开发现盘链(贴吧/知乎 → 转存)", "pan_discovery", "pan_discovery", None),
    ("迅雷群分享采集 → 转存", "xunlei_group", "xunlei_group", None),
    ("迅雷盘扫盘 → 二次分享", "xunlei_sync", "xunlei_sync", None),
    ("跨平台对标号发现(知乎/B站)", "cross_account_discover", None, None),
    ("跨平台资源热度(小红书/快手/贴吧/B站)", "resource_presence", "resource_presence", None),
    ("多平台热榜采集", "hot_source", "hot_source", None),
    ("公众号关键词文章", "keyword_article", None, ("wechat_articles", "created_at")),
    ("闲鱼采集", "collect_tick", "xianyu", None),
    ("微博/百度热榜", "collect_tick", "weibo", None),
    ("抖音热点宝", "douhot_window_tick", "douhot_window", None),
    ("微信读书 Cookie 续期", "weread_refresh", None, None),
    ("网盘 Cookie 保活", "pan_cookie_keepalive", None, None),
    ("过期转存清理", "xunlei_cleanup", None, None),
    ("数据清理 + 备份", "data_cleanup", None, None),
    ("对标号停更退休", "bench_retire", None, None),
    ("群会员续费提醒", "member_renewal", None, None),
    ("磁盘水位守卫", "disk_guard", None, None),
    ("采集失败/停摆告警", "collect_failed_alert", None, None),
    ("失败作业自动重试", "auto_retry_failed_runs", None, None),
    ("每日推送时段表", "push_timeline", None, None),
    ("拉新周录提醒", "recruit_reminder", None, None),
    ("Telegram 频道源", "tg_collect", None, None),
]


def main() -> int:
    from sqlalchemy import func, select, text

    from app.db.database import get_session_local
    from app.db.models import JobHeartbeat, RunRecord

    with get_session_local()() as db:
        beats = {b.job_id: b for b in db.scalars(select(JobHeartbeat)).all()}
        kinds: dict[str, datetime] = {}
        for k, ts in db.execute(
                select(RunRecord.kind, func.max(RunRecord.started_at)).group_by(RunRecord.kind)).all():
            kinds[str(k)] = ts

        def table_last(tbl: str, col: str) -> datetime | None:
            try:
                v = db.execute(text(f"SELECT MAX({col}) FROM {tbl}")).scalar()
            except Exception:  # noqa: BLE001 - 表/列不存在就别把它当判据
                return None
            # ⚠️ 裸 SQL 读 SQLite 的 DateTime 列拿回来的是**字符串**(ORM 才会转 datetime),
            # 直接 strftime 会 AttributeError —— 统一归一。
            if isinstance(v, str):
                try:
                    v = datetime.fromisoformat(v)
                except ValueError:
                    return None
            return v if isinstance(v, datetime) else None

        print(f"{'能力':<40}{'上次真跑通':<18}{'跑了':>6}{'错':>5}  判据")
        print("-" * 92)
        for name, jid, kind, fallback in CAPS:
            when, src, cnt, err = None, "—", "—", "—"
            b = beats.get(jid) if jid else None
            if b is not None:
                when, src = b.last_ok_at, "心跳(作业)"
                cnt, err = str(b.run_count), str(b.error_count)
            if when is None and kind and kinds.get(kind):
                when, src = kinds[kind], f"runs/{kind}"
            if when is None and fallback:
                when, src = table_last(*fallback), f"表/{fallback[0]}"
            txt = when.strftime("%m-%d %H:%M") if when else "—— 查无痕迹 ——"
            print(f"{name:<40}{txt:<18}{cnt:>6}{err:>5}  {src}")
    print("\n注:'查无痕迹'要么是**真的没跑过**,要么是这条能力还没接心跳/不写运行记录 —— 两者都值得查。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
