# -*- coding: utf-8 -*-
"""网盘拉新适配度判据(v2.3.0,用户命题落地)。

**产品命题(2026-10-01 用户)**:监控热点的唯一目的是网盘拉新——
「监控什么样的信息适合做网盘拉新」与「监控到之后如何利用」才是重点,不是"看到热点就报"。

本模块用**确定性规则**回答第一问:这个热点能不能变成"一份让人必须转存的文件"?
规则先判、LLM 后写方案——噪声挡在 LLM 之前(省 token、降噪、可解释、可回测、可调词表)。

四问判据:
    ① 资料形态:热点背后是否天然存在可打包的文件(真题/课件/模板/安装包/壁纸/攻略/赛程...)
       ——这是**核心判据**:没有文件可交付,网盘拉新无从谈起;
    ② 搜索意图:用户会主动"搜着找文件"吗(求资源/完整版/下载/PDF/哪里能看...)——
       刷到看看 ≠ 会转存,搜索意图才是转存的前置动作;
    ③ 需求窗口:即时型(赛事当天,过了就凉) vs 长尾型(考证资料,常年有人要)——
       决定"抢时效"还是"做常青",影响发布策略(不是优劣);
    ④ 风险(复用 resource_risk 词表):影视/付费课程版权清扫高发 → 降权。

输出可直接解释给运营("为什么建议做这条/为什么这条不建议")。
"""
from __future__ import annotations

from dataclasses import dataclass, field

# ---- ① 资料形态词(命中 = 热点自带"文件交付物") ----
FORM_WORDS = (
    # 学习/考试("网课"与风险表交集,剔除)
    "真题", "试题", "答案", "课件", "讲义", "笔记", "教程", "教学", "课程",
    "考研", "考公", "考编", "考证", "四六级", "雅思", "托福", "期末", "复习", "提纲",
    # 办公/素材
    "模板", "表格", "PPT", "简历", "方案", "策划", "素材", "字体", "笔刷", "预设",
    "壁纸", "头像", "表情包", "图标", "插件", "脚本", "源码", "代码",
    # 工具/软件
    "安装包", "绿色版", "激活", "工具", "软件", "客户端", "补丁", "模拟器",
    # 游戏/娱乐("全集"与风险表交集,剔除——它指向盗版影视而非素材形态)
    "攻略", "教程", "资源", "合集", "网盘", "整合", "整合包", "MOD", "mod",
    # 赛事/活动
    "赛程", "对阵", "名单", "阵容", "数据", "集锦", "回放", "预测",
    # 资料出版
    "报告", "白皮书", "手册", "指南", "手册", "大全", "资料", "文档", "PDF", "pdf",
    "电子书", "书单", "清单", "书单", "文献", "论文",
)

# ---- ② 搜索意图词(命中 = 用户会"搜着找") ----
INTENT_WORDS = (
    "求", "哪里", "怎么", "如何", "在哪", "有没有", "完整版", "全套", "免费",
    "下载", "领取", "获取", "分享", "打包", "整理", "汇总", "在线看", "能看",
)

# ---- ③ 长尾信号词(命中 = 常青需求,值得做成长期资产) ----
LONGTAIL_WORDS = (
    "教程", "入门", "进阶", "模板", "真题", "考证", "教程", "指南", "手册",
    "合集", "大全", "整理", "汇总", "清单", "资料",
)

# ---- ④ 风险词(与 hotspot_agent._RISK_* 同源语义,此处独立定义避免循环 import) ----
# 只收"盗版整套信号词"——影视/电视剧等**话题类型词**不算高危(热点提它是话题;
# 原 resource_risk 的宽表适用于"资源标题",对"热点词"会误伤,2026-10-01 实测修正)
RISK_HIGH_WORDS = ("全集", "4k", "蓝光", "破解", "付费课程", "网课", "盗版", "免费领全集")
# ---- 平台可衍生权重(核心新层,2026-10-01 实测驱动):热榜标题是新闻式的,
# 资料形态词常不出现,但**平台人群决定衍生可能**——二次元/游戏/影视/技术人群
# 天然需要配套文件(壁纸/攻略/素材/教程),时政财经快讯难衍生。 ----
SOURCE_FIT = {
    "bilibili": 0.50,    # 二次元/游戏/学习:壁纸/攻略/素材/教程
    "douban": 0.50,      # 影视书评:资源合集/清单
    "juejin": 0.55,      # 技术:源码/工具链/教程
    "ithome": 0.50,      # IT:软件/驱动/工具包
    "iqiyi": 0.45,       # 影视:剧集资源
    "zhihu": 0.40,       # 知识/学习资料
    "hupu": 0.35, "dongqiudi": 0.35,   # 体育:赛程/数据/壁纸
    "kuaishou": 0.20, "weibo": 0.15, "36kr": 0.10,   # 泛资讯:低可衍生
}

# ---- 热点类型实体词(命中 = 指向"必然要配套文件"的人群) ----
ENTITY_WORDS = (
    "原神", "王者", "荣耀", "崩坏", "星穹", "铁道", "阴阳师", "第五人格", "永劫", "蛋仔",
    "新番", "番剧", "动漫", "二次元", "漫画", "手办", "漫展", "漫剧", "短剧", "综艺",
    "电影", "上映", "票房", "剧集", "电视剧", "纪录片", "游戏", "手游", "端游", "赛季",
    "考试", "考研", "考公", "考证", "报名", "招生", "开学", "期末", "真题",
    "软件", "工具", "开源", "编程", "代码", "模型", "插件",
)

RISK_LOW_WORDS = ("真题", "课件", "模板", "壁纸", "笔记", "汇总", "攻略", "素材", "赛程", "题库")


@dataclass
class FitResult:
    """适配度评估结果(全部可解释)。"""

    score: float                 # 0~1
    level: str                   # strong / mid / weak
    reasons: list[str] = field(default_factory=list)   # 给运营看的人话理由
    window: str = "unknown"      # instant(即时型) / longtail(长尾型)
    risk: str = "low"            # low / mid / high

    @property
    def doable(self) -> bool:
        """是否值得进入 LLM 选题(weak 挡在门外,宁缺毋滥)。"""
        return self.level in ("strong", "mid")


def assess(title: str, extra: str = "", source: str = "") -> FitResult:
    """对一条热点(标题+补充+来源平台)做网盘拉新适配度评估。"""
    blob = f"{title or ''} {extra or ''}".lower()
    score = 0.0
    reasons: list[str] = []

    # ⓪ 平台可衍生权重(核心层,实测驱动):平台人群决定衍生可能
    src_key = str(source or "").split("+")[0].strip()   # 共振串取首个平台
    src_w = SOURCE_FIT.get(src_key, 0.0)
    if src_w:
        score += src_w
        if src_w >= 0.4:
            reasons.append(f"平台人群天然需配套文件({src_key})")

    # ⓪b 热点类型实体词:命中 = 指向必然要文件的人群
    ent_hits = [w for w in ENTITY_WORDS if w in blob]
    if ent_hits:
        score += min(0.25, 0.15 + 0.05 * len(ent_hits))
        reasons.append(f"热点类型可衍生({'/'.join(ent_hits[:3])})")

    # ① 资料形态(独立证据):命中越多越强,封顶 0.55
    form_hits = [w for w in FORM_WORDS if w.lower() in blob]
    if form_hits:
        score += min(0.55, 0.30 + 0.08 * len(form_hits))
        reasons.append(f"自带资料形态({'/'.join(form_hits[:3])})")

    # ② 搜索意图:封顶 0.25
    intent_hits = [w for w in INTENT_WORDS if w in blob]
    if intent_hits:
        score += min(0.25, 0.15 + 0.05 * len(intent_hits))
        reasons.append(f"有搜索意图({'/'.join(intent_hits[:3])})")

    # ③ 窗口类型(不直接加分,决定策略):长尾词命中 → longtail
    window = "longtail" if any(w in blob for w in LONGTAIL_WORDS) else \
        ("instant" if form_hits or intent_hits else "unknown")

    # ④ 风险降权
    risk = "low"
    if any(w in blob for w in RISK_HIGH_WORDS):
        risk = "high"
        score -= 0.30
        reasons.append("版权清扫高危类(慎投时效,适合长尾低风险包装)")
    elif any(w in blob for w in RISK_LOW_WORDS):
        risk = "low"
        score += 0.05
        reasons.append("整理/自制类(长尾安全)")

    score = max(0.0, min(1.0, score))
    if risk == "high":
        score = min(score, 0.5)  # 盗版整套信号:可做但高危,封顶压级,不优先推
    level = "strong" if score >= 0.6 else ("mid" if score >= 0.35 else "weak")
    if not reasons:
        reasons.append("未识别到资料形态/搜索意图——更像新闻围观,转存动机弱")
    return FitResult(score=round(score, 2), level=level, reasons=reasons, window=window, risk=risk)


def assess_many(hotspots: list[dict]) -> list[dict]:
    """批量评估并原地写 h["fit"];返回按适配度降序的新列表(不改变原顺序语义)。

    调用方(run_hotspot_agent)据此把 weak 挡在 LLM 之前。
    """
    out = []
    for h in hotspots:
        fit = assess(str(h.get("keyword") or ""), str(h.get("extra") or ""),
                     source=str(h.get("platforms") or ""))
        h["fit"] = fit
        out.append(h)
    out.sort(key=lambda x: (-x["fit"].score, -(x.get("growth") or 0)))
    return out
