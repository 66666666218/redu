"""网盘拉新适配度判据单测(v2.3.0 用户命题:监控什么信息适合做网盘拉新)。"""
from app.services.niche_fit import assess


def test_strong_fit_for_derivable_hotspot() -> None:
    # 游戏/二次元热点 + 高可衍生平台 → 强适配
    r = assess("《原神》六周年主题曲发布", source="bilibili")
    assert r.level == "strong" and r.doable
    assert any("平台人群" in x for x in r.reasons)

    # 资料形态词直接命中(真题/课件) → 强适配(与平台无关)
    r2 = assess("2026考研专业课真题汇总", source="weibo")
    assert r2.level == "strong" and r2.window == "longtail"


def test_weak_fit_for_news_spectacle() -> None:
    # 纯新闻围观:泛资讯平台 + 无任何衍生信号 → weak(挡在 LLM 之前)
    for title, src in (("迪拜航空确认航班发生事故", "weibo"),
                       ("热门中概股美股盘前普涨", "36kr"),
                       ("天安门广场国庆升旗仪式", "weibo")):
        r = assess(title, source=src)
        assert r.level == "weak" and not r.doable, f"{title} 应 weak,got {r.level}"


def test_entity_words_and_risk_downgrade() -> None:
    # 实体词(新番/剧)加分
    r = assess("十月新番开播一览", source="bilibili")
    assert r.doable and any("可衍生" in x for x in r.reasons)

    # 风险降权:影视全集类在高危词表 → risk=high 且被减分
    r2 = assess("某电视剧全集4K蓝光资源", source="douban")
    assert r2.risk == "high"
    assert any("版权清扫" in x for x in r2.reasons)
    # 同平台无风险词基线的对比:风险必须体现在分数上
    r3 = assess("某电视剧新剧开播", source="douban")
    assert r2.score < r3.score


def test_proven_categories_from_business_facts() -> None:
    """验证品类层(用户一线业务事实):资料/影视/漫剧/问卷/大瓜 命中即强信号。"""
    from app.services.niche_fit import assess

    # 五类验证品类全部可做
    for title, src in (("2026考研英语真题答案解析", "weibo"),
                       ("某剧全集在线看中字", "douban"),
                       ("十月新番漫剧推荐合集", "bilibili"),
                       ("花少2人格测试最新入口直达（可自取）", "weibo"),   # 回灌实证:共振×13
                       ("某明星聊天记录截图曝光", "weibo")):
        r = assess(title, source=src)
        assert r.doable, f"{title} 应可做,got {r.level}"
    # 验证品类的理由要可解释
    r = assess("十月新番漫剧推荐合集", source="bilibili")
    assert any(("验证品类" in x) or ("平台人群" in x) for x in r.reasons)
