"""公众号内容选题分析智能体单测(纯逻辑,无网络/无库)。"""
from app.services.wechat_analyzer import analyze_articles, _parse_time


def test_content_only_articles_still_yield_topics() -> None:
    """仅有正文、无标题的文章也要参与主题分析。

    回归:旧实现用 zip(articles, titles) 构造 contents,而 titles 过滤掉了无标题文章;
    当一篇文章都没标题时 titles 为空,zip 直接产出空 contents → 主题/摘要全部丢失。
    """
    articles = [
        {"content": "考研 考研 考研 复习计划", "author": "学长"},
        {"content": "考研 数学 英语 真题", "author": "学长"},
    ]
    r = analyze_articles(articles)
    assert r["count"] == 2
    assert r["topics"], "无标题文章仍应提取到主题词"
    assert r["topics"][0]["word"] == "考研"


def test_title_misalignment_not_propagated() -> None:
    """zip(articles, titles) 错位:中间文章有标题会让尾部无标题文章整篇被丢弃。"""
    articles = [
        {"content": "咖啡 咖啡"},                # 无标题
        {"title": "茶叶", "content": "茶叶"},      # 唯一标题 → 旧 zip 长度=1
        {"content": "红酒 红酒 红酒"},              # 无标题:旧 zip 只迭代到第 1 篇,此篇被丢弃
    ]
    r = analyze_articles(articles)
    words = {t["word"] for t in r["topics"]}
    assert "红酒" in words, "尾部无标题文章的正文不应被丢弃"
    assert "咖啡" in words and "茶叶" in words



def test_analyze_empty() -> None:
    r = analyze_articles([])
    assert r["count"] == 0 and r["topics"] == [] and "暂无" in r["suggestions"][0]


def test_analyze_basic_output() -> None:
    articles = [
        {"title": "揭秘AI副业3个方法,新手也能月入过万", "content": "AI 副业 变现 教程 拆解", "author": "科技君", "publish_at": "2026-09-06 21:00"},
        {"title": "速看!退休后做这个副业,避开3个坑", "content": "退休 副业 避坑 测评", "author": "生活家", "publish_at": "2026-09-06 20:00"},
        {"title": "这套模板拯救了我的副业,原来这么简单", "content": "模板 副业 教程 干货", "author": "科技君", "publish_at": "2026-09-07 09:00"},
    ]
    r = analyze_articles(articles)
    assert r["count"] == 3
    assert r["topics"][0]["word"] == "副业"           # 词频最高的主题
    assert r["title_style"]["num_pct"] == 0.67        # 2/3 含数字(3个/3个)
    assert r["title_style"]["hook_pct"] == 1.0        # 悬念词全有
    assert r["publish"]["peak_hours"]                # 有发布高峰
    assert len(r["authors"]) == 2                    # 2 个公众号对比
    assert any("副业" in s for s in r["suggestions"])  # 选题建议提到主线
    assert r["summary"]


def test_parse_time_handles_iso_and_date() -> None:
    assert _parse_time("2026-09-06") is not None
    assert _parse_time("2026-09-06T20:30:00") is not None
    assert _parse_time(None) is None
    assert _parse_time("not-a-date") is None
