"""**板块/作业归哪一侧跑** —— 全项目唯一一份归属判据。

分体部署(`SCHEDULER_ROLE`)下:本机 `wechat` 侧跑公众号+闲鱼+资源链,远程 `hotspot`
侧跑微博/抖音/百度。凡是要问"这个板块/作业该不该由本实例跑"的地方,**只许调这里**。

⚠️⚠️ **为什么单独成模块**(2026-11-23 合并):此前同一张映射有**三份副本** ——
`scheduler._SECTION_ROLE`、`health.py::_SECTION_ROLE`(多一个 `pan`)、
`alert_service.check_health_stalls` 里的内联 `_section_role`。三份内容当时**恰好一致**,
但改分工时漏改任何一份,后果都是**静默的**:调度护栏漏改 ⇒ 两端重复采同一个上游账号;
健康页/停摆告警漏改 ⇒ 对着**本实例不管的板块**天天报红灯(2026-10-02、10-24 各踩过一次)。
"两份映射目前一致"不是保障,**只有一份**才是。

依赖方向:scheduler / health / alert_service / api 均单向依赖本模块,本模块只依赖
`config.settings`(取类型),所以不会引入循环导入。
"""
from __future__ import annotations

from config.settings import Settings

#: 板块 → 归属侧。`wechat` 侧 = 公众号 + 闲鱼 + 网盘资源链(`pan` 只出现在健康页,
#: 没有独立的调度板块,但它对应的 `xunlei_*` / `pan_discovery` 作业确实在 wechat 侧跑)。
SECTION_ROLE = {
    "wechat": "wechat", "xianyu": "wechat", "pan": "wechat",
    "weibo": "hotspot", "douhot": "hotspot", "baidu": "hotspot",
}

#: **运行记录 kind → 板块**。`runs.kind` 比 `user_schedules.section` 细
#: (`xianyu_deep` / `wechat_listen` / `douyin_leads` …),而**归属判断必须能问到 kind 上** ——
#: 重试、手动采集、失败告警三处拿到的都是 kind,不是 section。
KIND_SECTION = {
    "weibo": "weibo", "douhot": "douhot", "baidu": "baidu",
    "xianyu": "xianyu", "xianyu_deep": "xianyu",
    "wechat": "wechat", "wechat_listen": "wechat", "wechat_sync": "wechat",
    "douyin_leads": "wechat",       # 与本机 CDP 档案/登录态绑定(见 scheduler.build_jobs 的归属注释)
    "douyin_leads_dagua": "wechat",
}


def section_allowed(section: str, settings: Settings) -> bool:
    """本实例该不该跑这个板块(未登记的板块不拦,向后兼容)。

    `all` / `both` 角色一律放行 —— 单机部署(`SCHEDULER_ROLE=both`)不该被拦死。
    """
    want = SECTION_ROLE.get(section)
    if not want:
        return True
    cur = (getattr(settings, "scheduler_role", "all") or "all").strip().lower()
    return cur in ("all", "both") or cur == want


def kind_allowed(kind: str, settings: Settings) -> bool:
    """按**运行记录 kind** 判归属;映射表里没有的 kind 不拦(向后兼容)。

    ★★ **2026-11-23 事故**:远程实例(`SCHEDULER_ROLE=hotspot`)上,`collect_tick` 有护栏、
    但**重试/手动采集/失败告警都没有** —— 用户 4 于 11-13 20:20 注册后,面板手点了一次闲鱼
    (`POST /api/collect/xianyu`)失败了,此后 `retry_failed_runs` **每 30 分钟**把它捞出来重跑;
    而远程**从来没装过 Playwright 浏览器**(Dockerfile 明确"纯 requests,无需浏览器")。
    于是两台机器上"闲鱼失败 16/16、连续 20 轮"的告警,其实全是**远程在自己跑不该它跑的板块**。
    判据只有一处(`SECTION_ROLE`),这里是它面向 kind 的入口,别在调用方各写一份。
    """
    section = KIND_SECTION.get(kind)
    if section is None:
        return True
    return section_allowed(section, settings)