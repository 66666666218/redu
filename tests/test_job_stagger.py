"""作业**错峰**守卫:定点作业不许挤在同一分钟(2026-10-07)。

## 为什么要有这条
用户口径把 `wechat_collect_tick` 从 `:00` 挪到 `:02`,理由是"**避开冲突时间**" ——
那是一次**手工**修正,而手工修正的问题在于:**下次加作业时会再撞上,没人拦得住**。
于是把那个判断固化成这条守卫。

⚠️ 加固它的直接原因:同一天我用"查配置里的 `*_cron` 字符串"的方式核对过一次,
结论是"无撞车" —— **那是错的**:`wechat_collect_tick` 是显式 `CronTrigger(...)` 注册的,
**根本不在那批配置里**。用**已注册的触发器**重算才发现还有 4 处真撞车:

    周一 09:40  disk_guard + recruit_reminder      ← 两个**都是推送**
    08:30       quark_kouling(模拟器,占分钟级)+ wechat_candidate_import
    08:20       resource_presence_bili + wechat_candidates
    10:05       member_renewal + xunlei_sync

⇒ 所以本测试**必须从 `build_jobs` 的真实触发器算**(那是唯一的事实源),
   而不是从配置字符串猜 —— 「配置说没有」和「实际没有」是两件事。
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from apscheduler.schedulers.background import BackgroundScheduler  # noqa: E402

from app.services.scheduler import build_jobs  # noqa: E402

#: **每分钟**都跑的三条 tick —— 它们天然在**每一分钟**上,拿它们判"挤"是假问题。
#: (把它们算进来,"撞车"会变成 1400+ 条噪音,守卫立刻没人看;噪音化的守卫等于没有守卫。)
EVERY_MINUTE = frozenset({"push_timeline", "collect_tick", "alert_fixed_time"})

#: **每半小时**跑的三条告警 tick —— 它们占住 `:00` 与 `:30`。
#: ⚠️ 与 `EVERY_MINUTE` **分开**是必须的:监听挪到 `:02` 要避开的正是这三条,
#: 把它们并进上面那类,就再也测不出"监听别挪回 `:00`"了(第一版就是这么错的)。
HALF_HOURLY = frozenset({"auto_retry_failed_runs", "collect_failed_alert",
                         "health_stall_alert"})

#: 上面两类都不参与"定点作业之间"的撞车比对(见各自说明)
PERIODIC = EVERY_MINUTE | HALF_HOURLY


def _expand(exprs: list[str], lo: int, hi: int) -> set[int]:
    """cron 字段 → 取值集合。只支持调度器里实际用过的三种写法(`*` / `*/k` / 逗号数字)。"""
    out: set[int] = set()
    for e in exprs:
        if e == "*":
            out |= set(range(lo, hi + 1))
        elif e.startswith("*/"):
            out |= set(range(lo, hi + 1, int(e[2:])))
        else:
            out.add(int(e))
    return out


def _slots(job) -> set[tuple[int, int, int]]:
    """作业实际占用的 `(周几, 时, 分)` 集合(APScheduler:**0 = 周一**)。"""
    f = {x.name: [str(e) for e in x.expressions] for x in job.trigger.fields}
    hours = _expand(f["hour"], 0, 23)
    mins = _expand(f["minute"], 0, 59)
    dow_e = f.get("day_of_week", ["*"])
    dows = set(range(7)) if dow_e == ["*"] else {int(x) for x in dow_e if x.isdigit()}
    return {(d, h, m) for d in dows for h in hours for m in mins}


def _collisions() -> dict[tuple[int, int, int], list[str]]:
    import collections

    sched = BackgroundScheduler(timezone="Asia/Shanghai")
    build_jobs(sched)
    seen: dict[tuple[int, int, int], list[str]] = collections.defaultdict(list)
    for j in sched.get_jobs():
        if j.id in PERIODIC:
            continue
        for s in _slots(j):
            seen[s].append(j.id)
    return {k: sorted(v) for k, v in seen.items() if len(v) > 1}


def test_定点作业不挤在同一分钟() -> None:
    """★ 一条真撞车都不许有。

    撞车的实际代价(实测过的那几处):两个**推送**作业同分钟 → 群里同时落两张卡;
    两个 **DB 写方**同分钟 → `database is locked`(本仓的老坑,慢活占着单写者的锁)。
    """
    wd = "一二三四五六日"
    bad = _collisions()
    msg = "\n".join(f"  {wd[d]} {h:02d}:{m:02d} → {' + '.join(ids)}"
                    for (d, h, m), ids in sorted(bad.items()))
    assert not bad, (
        f"有 {len(bad)} 处定点作业撞在同一分钟:\n{msg}\n"
        f"⇒ 把**较轻的那个**挪几分钟(错峰的意义见 settings 里各 cron 的注释)。")


def test_公众号监听不落在半小时网格上() -> None:
    """★ `:02` 是**有理由**的(用户口径:"避开冲突时间"),别把它挪回 `:00`。

    `:00` 和 `:30` 这两个分钟坐着三条 */30 的告警 tick(`auto_retry_failed_runs` /
    `collect_failed_alert` / `health_stall_alert`),而监听一轮要跑**十几分钟** ——
    跟它们挤在一起没有好处。

    ⚠️ **这条测试返工过一次,记下来**:第一版断言的是"监听那一分钟没有别的定点作业",
    而它把 `:00` 那三条 tick 当作 `PERIODIC` 排除了 —— 于是**把监听改回 `:00` 它照样绿**,
    一个什么都不检查的假守卫。改成直接断言"不落在 {0, 30} 上"才真的抓得住
    (已用变异验证:改回 `:00` ⇒ 变红)。
    """
    sched = BackgroundScheduler(timezone="Asia/Shanghai")
    build_jobs(sched)
    tick = next(j for j in sched.get_jobs() if j.id == "wechat_collect_tick")
    minutes = {m for _, _, m in _slots(tick)}
    assert minutes.isdisjoint({0, 30}), (
        f"监听落在半小时网格上({sorted(minutes & {0, 30})} 分)—— 那一分钟有三条 */30 告警 tick")


def test_监听那一分钟没有别的定点作业() -> None:
    """除"每分钟/每半小时"那几条 tick 外,监听在自己那一分钟是**独占**的。"""
    import collections

    sched = BackgroundScheduler(timezone="Asia/Shanghai")
    build_jobs(sched)
    seen: dict[tuple[int, int, int], list[str]] = collections.defaultdict(list)
    for j in sched.get_jobs():
        for s in _slots(j):
            seen[s].append(j.id)
    tick = next(j for j in sched.get_jobs() if j.id == "wechat_collect_tick")
    for d, h, m in _slots(tick):
        # ⚠️ 只排除"每分钟"那三条;**每半小时那三条要留着** ——
        # 监听一旦挪回 `:00`,这里就会亮红(它们正是 `:02` 要避开的东西)。
        peers = [x for x in seen[(d, h, m)]
                 if x != "wechat_collect_tick" and x not in EVERY_MINUTE]
        assert not peers, f"{h:02d}:{m:02d} 上监听和 {peers} 挤在一起了"


def test_守卫本身不能是空跑() -> None:
    """⚠️ 一条"什么都不检查"的守卫比没有更糟(它会给假绿)。

    所以先断言**确实枚举到了足够多的定点作业** —— 万一 `build_jobs` 因角色被挡下、
    或者 `PERIODIC` 名单被误改成把全部作业都排除,这条会先红。
    """
    sched = BackgroundScheduler(timezone="Asia/Shanghai")
    build_jobs(sched)
    fixed = [j.id for j in sched.get_jobs() if j.id not in PERIODIC]
    assert len(fixed) >= 15, f"只枚举到 {len(fixed)} 个定点作业,守卫可能是空跑:{fixed}"
    assert "wechat_collect_tick" in fixed


def test_定点作业不落在半小时网格上() -> None:
    """★ **`:00` 与 `:30` 是 `auto_retry_failed_runs` 和三条告警 tick 的地盘**,别的作业让开。

    ## 为什么这条比"两两不撞车"更强(2026-10-07 实测)
    `runs.detail` 里 `database is locked` 共 **30 条**,抽出来看**全部落在 `:00/:01` 分** ——
    那正是 `auto_retry_failed_runs`(每 30 分钟一次,会**启动重活的重试**)与其它作业
    撞在一起的时刻。"两两不撞车"抓不到这个:**它只保证同一分钟没有两个定点作业**,
    而"定点作业 + 那条会拉起重活的 tick"照样会争锁。
    ⇒ 于是把"**定点作业一律不占 `:00/:30`**"作为更强的不变式(用户口径:
    "如果会影响有侵占的风险就可以动")。原先坐在网格上的 6 个已全部挪到 `:31`~`:34`。

    ⚠️ 网格的**主人**不能参与这条判断 —— 那三条 tick 本来就是设计成每 30 分钟跑的。
    """
    sched = BackgroundScheduler(timezone="Asia/Shanghai")
    build_jobs(sched)
    bad = []
    for j in sched.get_jobs():
        if j.id in PERIODIC:
            continue
        mins = {m for _, _, m in _slots(j)}
        hit = sorted(mins & {0, 30})
        if hit:
            bad.append(f"{j.id} 在 {hit} 分")
    assert not bad, (
        "这些定点作业落在 :00/:30 网格上(那是重试与告警 tick 的地盘): "
        + " / ".join(bad))
