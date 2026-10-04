"""热点→网盘拉新 Agent 测试:精确匹配 / LLM 语义匹配+选题 / 24h 去重 / 开关 / 建议落表 / 多平台共振。"""
import datetime as dt
import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.models import (BaiduHotItem, Base, DouhotWatchSnap, HotspotSuggestion,
                           SystemConfig, User, WechatArticle, WeiboHotItem)
from app.services import alert_service, hotspot_agent


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    yield db
    db.close()


@pytest.fixture
def agent_env(session, monkeypatch):
    session.add(User(id=1, email="op@test.com", username="op", password_hash="x"))
    alerts: list[tuple] = []
    monkeypatch.setattr(alert_service, "notify_incident",
                        lambda db, user_id, kind, title, detail, settings=None, **kw:
                        alerts.append((title, detail, kw.get("push_feishu", True))) or False)
    return alerts


def _snap(session, keyword: str, growth: float, hours_ago: float = 0.5) -> None:
    session.add(DouhotWatchSnap(user_id=1, section="douhot", list_type="word",
                                keyword=keyword, trend_growth=growth, rank_now=2,
                                score=100, captured_at=dt.datetime.now() - dt.timedelta(hours=hours_ago)))
    session.commit()


def _supply(session, aid: int, title: str, hours_ago: float = 1.0,
            pan: str = "https://pan.quark.cn/s/x1") -> None:
    session.add(WechatArticle(id=aid, user_id=1, title=title, author="供应商号",
                              source="listen",
                              url=f"https://mp.weixin.qq.com/s/u{aid}",
                              pan_urls=pan,
                              created_at=dt.datetime.now() - dt.timedelta(hours=hours_ago)))
    session.commit()


def _agent_settings(**kw):
    from config.settings import Settings
    base = {"hotspot_agent_enabled": True, "hotspot_min_growth": 50.0,
            "hotspot_agent_llm_top": 3, "deepseek_api_key": ""}
    base.update(kw)
    return Settings(_env_file=None, is_dev=True, **base)


def _run(session, monkeypatch, llm_json: str | None = None, **kw):
    settings = _agent_settings(**kw)
    if llm_json is not None:
        monkeypatch.setattr(settings, "deepseek_api_key", "sk-x")

        class _Resp:
            status_code = 200

            def __init__(self, text: str) -> None:
                self._text = text

            def json(self):
                return {"choices": [{"message": {"content": self._text}}]}

        import app.services.hotspot_agent as ha
        monkeypatch.setattr(ha.requests, "post", lambda *a, **k: _Resp(llm_json))
    return hotspot_agent.run_hotspot_agent(session, 1, settings)


def test_hotspot_matches_supply_and_notifies(session, monkeypatch, agent_env) -> None:
    """热点词涨幅达标且标题字面命中资源文 → 建议跟发并附复制即用块;建议落表;24h 去重。"""
    _snap(session, "Switch模拟器", growth=180)
    _supply(session, 11, "Switch模拟器最新版整合包(附安装教程)")
    alerts = agent_env

    out = _run(session, monkeypatch, dajiala_key="")
    assert out["matched"] == 1 and out["status"] == "ok"
    title, detail, feishu = alerts[0]
    assert "1 条现成资源" in title and "Switch模拟器" in detail
    assert "pan.quark.cn/s/x1" in detail and "复制即用" in detail
    assert feishu is False
    row = session.scalars(select(HotspotSuggestion)).one()
    assert row.keyword == "Switch模拟器" and row.kind == "match"
    assert "Switch模拟器" in row.resource_title

    again = _run(session, monkeypatch, dajiala_key="")
    assert again["status"] == "all_duplicated"
    assert len(alerts) == 1


def test_hotspot_low_growth_ignored(session, monkeypatch, agent_env) -> None:
    _snap(session, "PS5游戏资源", growth=10)
    out = _run(session, monkeypatch, dajiala_key="")
    assert out["status"] == "no_hotspots"


def test_hotspot_llm_semantic_match(session, monkeypatch, agent_env) -> None:
    """语义匹配:资源标题不含热点字面词,LLM 按 article_id 匹配成功 → kind=match。"""
    _snap(session, "世界杯", growth=200)
    _supply(session, 21, "2026足球赛程表+强弱分析(免费保存)")
    llm = json.dumps({"matches": [{"hotspot": "世界杯", "article_id": 21,
                                   "why": "赛程表正是世界杯期间的即时刚需"}],
                      "plans": []}, ensure_ascii=False)
    out = _run(session, monkeypatch, llm_json=llm, deepseek_api_key="sk-x")
    assert out["matched"] == 1
    row = session.scalars(select(HotspotSuggestion)).one()
    assert row.kind == "match" and "足球赛程表" in row.resource_title
    title, detail, _ = agent_env[0]
    assert "赛程表正是世界杯期间的即时刚需" in detail   # 匹配理由透传给运营


def test_hotspot_llm_suggests_for_unmatched(session, monkeypatch, agent_env) -> None:
    """热点没现成资源 → LLM 给拉新选题;建议落表(kind=llm)且告警带方案。"""
    _snap(session, "剪映模板", growth=120)
    _snap(session, "考公资料", growth=90)
    llm = json.dumps({"matches": [], "plans": [
        {"hotspot": "剪映模板", "resource": "2026最新剪映爆款模板合集",
         "title": "剪映模板直接抄作业(免费保存)", "audience": "短视频新手",
         "hook": "模板要导入剪映才能用,必须转存"},
        {"hotspot": "考公资料", "resource": "国省考历年真题+冲刺笔记",
         "title": "考公真题免费自取", "audience": "应届考公人", "hook": "打印刷题"}]},
        ensure_ascii=False)
    alerts = agent_env

    out = _run(session, monkeypatch, llm_json=llm, deepseek_api_key="sk-x")
    assert out["matched"] == 0 and out["llm"] == 2
    rows = session.scalars(select(HotspotSuggestion)).all()
    assert {r.keyword for r in rows} == {"剪映模板", "考公资料"}
    assert all(r.kind == "llm" for r in rows)
    title, detail, _ = alerts[0]
    assert "剪映爆款模板合集" in detail and "2 条拉新选题" in title


def test_hotspot_agent_disabled(session, monkeypatch, agent_env) -> None:
    _snap(session, "Switch模拟器", growth=180)
    out = _run(session, monkeypatch, hotspot_agent_enabled=False)
    assert out["status"] == "disabled"


def _weibo(session, title: str, rank: int = 1, heat: int = 900_000,
           hours_ago: float = 1.0) -> None:
    session.add(WeiboHotItem(user_id=1, title=title, heat=heat, rank=rank,
                             captured_at=dt.datetime.now() - dt.timedelta(hours=hours_ago)))
    session.commit()


def _baidu(session, title: str, rank: int = 2, heat: int = 8,
           hours_ago: float = 1.0) -> None:
    session.add(BaiduHotItem(user_id=1, title=title, heat=heat, rank=rank,
                             captured_at=dt.datetime.now() - dt.timedelta(hours=hours_ago)))
    session.commit()


def test_resonance_tag_and_platforms(session, monkeypatch, agent_env) -> None:
    """抖音词在微博/百度同话题新上榜 → 输出带共振标记,platforms 落表(多平台证据)。"""
    _snap(session, "王楚钦", growth=150)
    _weibo(session, "王楚钦 男单夺冠", rank=1)
    _baidu(session, "王楚钦男单夺冠", rank=2)
    _supply(session, 31, "王楚钦比赛视频合集(持续更新)")
    alerts = agent_env

    out = _run(session, monkeypatch, dajiala_key="")
    assert out["matched"] == 1 and out["status"] == "ok"
    _, detail, _ = alerts[0]
    assert "共振" in detail and "微博榜第1名" in detail and "百度榜第2名" in detail
    row = session.scalars(select(HotspotSuggestion)).one()
    assert row.platforms == "douyin+weibo+baidu"


def test_resonance_boost_reorders(session, monkeypatch, agent_env) -> None:
    """共振分级加权(v5):百度证据 ×1.5 → 涨幅较低的百度共振热点排序上浮。

    对照:仅微博共振 ×1.2 翻不了盘(微博证据弱,80×1.2=96 < 100)——
    这正是证据分级的语义,百度(真实搜索)>微博(可运营话题)。
    """
    _snap(session, "单平台热点", growth=100)
    _snap(session, "共振热点", growth=80)
    _baidu(session, "共振热点全网刷屏", rank=3)
    _supply(session, 41, "单平台热点资源包")
    _supply(session, 42, "共振热点资源包")
    alerts = agent_env

    out = _run(session, monkeypatch, dajiala_key="")
    assert out["matched"] == 2
    _, detail, _ = alerts[0]
    assert detail.index("共振热点") < detail.index("单平台热点")   # 百度共振加权后上浮
    rows = {r.keyword: r.platforms for r in session.scalars(select(HotspotSuggestion)).all()}
    assert rows["共振热点"] == "douyin+baidu" and rows["单平台热点"] == "douyin"


def test_opportunity_prefers_blue_ocean(session, monkeypatch, agent_env) -> None:
    """机会分排序(v5):高涨幅红海(多家供货)让位低涨幅蓝海(竞争空白)。"""
    _snap(session, "红海热点", growth=150)
    _snap(session, "蓝海热点", growth=90)
    for i in range(41, 46):     # 红海:5 家已供货 → 稀疏度 1/6
        _supply(session, i, f"红海热点资源包{i}")
    _supply(session, 50, "蓝海热点资源包")   # 蓝海:1 家 → 稀疏度 1/2
    alerts = agent_env

    out = _run(session, monkeypatch, dajiala_key="")
    assert out["matched"] == 2
    _, detail, _ = alerts[0]
    # 机会分:蓝海 90×(1/2)=45 > 红海 150×(1/6)=25 → 蓝海排前(红海虽热但挤满供货)
    assert detail.index("蓝海热点") < detail.index("红海热点")
    assert "竞争" in detail
    rows = {r.keyword: r.opportunity for r in session.scalars(select(HotspotSuggestion)).all()}
    assert set(rows) == {"红海热点", "蓝海热点"}
    assert all(v > 0 for v in rows.values())


def test_window_factor_by_momentum(session, monkeypatch, agent_env) -> None:
    """窗口因子(v5):score 动量转跌 → 输出带「窗口将关闭」,机会分被压低。"""
    now = dt.datetime.now()
    for i, (s, hrs) in enumerate([(1000.0, 1.0), (500.0, 0.3)]):   # 两拍,环比 -50%
        session.add(DouhotWatchSnap(user_id=1, section="douhot", list_type="word",
                                    keyword="退烧热点", trend_growth=120, rank_now=2,
                                    score=s, captured_at=now - dt.timedelta(hours=hrs)))
    session.commit()
    _supply(session, 61, "退烧热点资源包")
    alerts = agent_env

    out = _run(session, monkeypatch, dajiala_key="")
    assert out["matched"] == 1
    _, detail, _ = alerts[0]
    assert "窗口将关闭" in detail and "2h" in detail
    row = session.scalars(select(HotspotSuggestion)).one()
    assert row.opportunity > 0    # 窗口因子 0.3 已折进机会分


def test_resonance_requires_newcomer(session, monkeypatch, agent_env) -> None:
    """微博条目 12h 前就上榜(超出 6h 新上榜窗口)→ 不算共振,不虚标多平台。"""
    _snap(session, "老热点", growth=150)
    _weibo(session, "老热点持续霸榜", hours_ago=12)
    _supply(session, 51, "老热点资源包")
    out = _run(session, monkeypatch, dajiala_key="")
    assert out["matched"] == 1
    _, detail, _ = agent_env[0]
    assert "共振" not in detail
    row = session.scalars(select(HotspotSuggestion)).one()
    assert row.platforms == "douyin"


def test_mark_acted_api(session) -> None:
    """一键标记已发(v5 下注环节):只有 acted 建议 + save_pv 才构成预测→结算样本。"""
    from app.api.hotspot import list_suggestions, mark_acted

    session.add(User(id=2, email="u@test.com", username="u2", password_hash="x"))
    session.add(HotspotSuggestion(user_id=2, keyword="测试热点", kind="llm", plan="x"))
    session.commit()
    user = session.get(User, 2)

    out = mark_acted(1, None, user=user, db=session)
    assert out["acted"] is True and out["keyword"] == "测试热点"
    row = session.get(HotspotSuggestion, 1)
    assert row.acted is True and row.acted_at is not None

    listing = list_suggestions(limit=50, acted=True, user=user, db=session)
    assert listing["total"] == 1 and listing["list"][0]["saves"] == 0

    # 取消标记 / 越权 404
    mark_acted(1, type("P", (), {"acted": False})(), user=user, db=session)
    assert session.get(HotspotSuggestion, 1).acted is False
    import pytest as _pytest
    from fastapi import HTTPException
    with _pytest.raises(HTTPException):
        mark_acted(999, None, user=user, db=session)


def test_settle_suggestions_repost_gain(session) -> None:
    """结算(去 dajiala 版):acted 建议按盘链归因到发文,repost_gain=发文后新增盘链记录数。"""
    from app.db.models import WechatArticle, WechatPanLink
    from app.services.hotspot_agent import settle_suggestions

    link = "https://pan.quark.cn/s/settle1"
    now = dt.datetime.now()
    art = WechatArticle(id=71, user_id=1, title="测试发文", author="我",
                        source="manual", url="https://mp.weixin.qq.com/s/z1",
                        my_pan_urls=link, created_at=now - dt.timedelta(days=1))
    session.add(art)
    session.commit()
    session.add(HotspotSuggestion(user_id=1, keyword="结算热点", kind="match",
                                  link=link, acted=True, acted_at=now - dt.timedelta(hours=2)))
    session.commit()

    # acted 之后全网出现 3 条该文盘链记录(被转载/扩散)
    for i in range(3):
        session.add(WechatPanLink(user_id=1, article_id=71,
                                  pan_url=f"https://pan.quark.cn/s/spread{i}",
                                  created_at=now - dt.timedelta(hours=1)))
    # acted 之前的 1 条不计入(基线=acted_at)
    session.add(WechatPanLink(user_id=1, article_id=71,
                              pan_url="https://pan.quark.cn/s/base",
                              created_at=now - dt.timedelta(days=1)))
    session.commit()

    out = settle_suggestions(session, 1)
    assert out["settled"] == 1
    row = session.scalars(select(HotspotSuggestion)).one()
    assert row.article_id == 71 and row.repost_gain == 3 and row.settled_at is not None

    # 重复结算:扩散继续增长则 gain 刷新
    session.add(WechatPanLink(user_id=1, article_id=71,
                              pan_url="https://pan.quark.cn/s/more",
                              created_at=now))
    session.commit()
    settle_suggestions(session, 1)
    assert session.scalars(select(HotspotSuggestion)).one().repost_gain == 4


def test_settle_skips_unacted_or_unattributed(session) -> None:
    """结算纪律:未 acted 或盘链对不上发文的不结算(宁少样本不脏样本)。"""
    from app.services.hotspot_agent import settle_suggestions

    session.add(WechatArticle(id=81, user_id=1, title="别的文", author="我",
                              source="manual", url="https://mp.weixin.qq.com/s/z2",
                              my_pan_urls="https://pan.quark.cn/s/other",
                              created_at=dt.datetime.now()))
    session.add(HotspotSuggestion(user_id=1, keyword="没发过的", kind="llm", link="",
                                  acted=False, plan="x"))
    session.add(HotspotSuggestion(user_id=1, keyword="发了但盘链陌生", kind="match",
                                  link="https://pan.quark.cn/s/unknown", acted=True))
    session.commit()

    out = settle_suggestions(session, 1)
    assert out["settled"] == 0
    for row in session.scalars(select(HotspotSuggestion)).all():
        assert row.article_id is None and row.settled_at is None


def test_recruits_upsert_and_list(session) -> None:
    """方案B:拉新周录 upsert(同周覆盖)+ 列表倒序。

    假 payload 要带上 `channels`(真实 `RecruitIn` 有默认值 `{}`,手搓的没有 → 会 AttributeError)。
    """
    from app.api.hotspot import list_recruits, upsert_recruit

    session.add(User(id=1, email="op@test.com", username="op", password_hash="x"))
    session.commit()
    user = session.get(User, 1)
    p = type("P", (), {"week_start": "2026-09-22", "recruits": 12, "note": "首周", "channels": {}})
    out = upsert_recruit(p, user=user, db=session)
    assert out["recruits"] == 12
    p2 = type("P", (), {"week_start": "2026-09-22", "recruits": 15, "note": "", "channels": {}})
    upsert_recruit(p2, user=user, db=session)          # 同周重录=覆盖
    # **分渠道**(2026-10-03 用户口径):给了明细就以**明细之和**为准,免得总数和明细打架
    p3 = type("P", (), {"week_start": "2026-09-29", "recruits": 999, "note": "",
                        "channels": {"douyin": 42, "wechat": 18}})
    upsert_recruit(p3, user=user, db=session)
    listing = list_recruits(limit=12, user=user, db=session)
    assert listing["total"] == 2
    assert listing["list"][0]["week_start"] == "2026-09-29"
    weeks = {r["week_start"]: r["recruits"] for r in listing["list"]}
    assert weeks["2026-09-22"] == 15
    assert weeks["2026-09-29"] == 60                    # 42+18,不是传进来的 999
    assert listing["list"][0]["channels"] == {"douyin": 42, "wechat": 18}


def test_auto_mark_acted_from_pan_link_and_title(session, monkeypatch) -> None:
    """自动归因(2026-09-30):运营者不知道员工发没发——从发文数据反推 acted。
    match 类按盘链精确匹配;llm 类按建议后入库文章的标题命中;建议之前的发文不算。"""
    from datetime import datetime, timedelta

    from app.db.models import HotspotSuggestion, WechatArticle
    from app.services.hotspot_agent import settle_suggestions

    now = datetime.now()
    # match 类:建议带盘链 link
    sug_link = HotspotSuggestion(
        user_id=1, keyword="世界杯", kind="match", growth=0,
        resource_title="赛程表", link="https://pan.quark.cn/s/match1",
        created_at=now - timedelta(days=2))
    # llm 类:无链接,靠标题命中
    sug_kw = HotspotSuggestion(
        user_id=1, keyword="亚运电竞", kind="llm", growth=0,
        created_at=now - timedelta(days=2))
    # 员工发文:一篇覆盖同盘链、一篇标题命中热点词——都在建议之后入库
    art1 = WechatArticle(user_id=1, title="世界杯赛程表合集", author="号A",
                         url="https://mp.weixin.qq.com/s/a1",
                         pan_urls="https://pan.quark.cn/s/match1",
                         created_at=now - timedelta(days=1))
    art2 = WechatArticle(user_id=1, title="亚运电竞观赛指南", author="号A",
                         url="https://mp.weixin.qq.com/s/a2",
                         created_at=now - timedelta(days=1))
    # 干扰项:建议**之前**就入库的同标题文章——不算执行
    art_old = WechatArticle(user_id=1, title="亚运电竞旧闻", author="号A",
                            url="https://mp.weixin.qq.com/s/old",
                            created_at=now - timedelta(days=3))
    session.add_all([sug_link, sug_kw, art1, art2, art_old])
    session.commit()

    out = settle_suggestions(session, 1)
    session.refresh(sug_link)
    session.refresh(sug_kw)
    assert sug_link.acted is True and sug_link.acted_at is not None  # 盘链反推
    assert sug_kw.acted is True                                      # 标题命中反推
    assert sug_link.article_id == art1.id                            # 归因到具体发文
    assert sug_link.repost_gain == 0                                 # 暂无盘链扩散,基线已立
    assert out["auto_acted"] == 2


def test_generate_draft_with_library_link(session, monkeypatch) -> None:
    """文案生成(v2.5.0):建议→LLM→落库;资源库有现成我方链自动带上;失败状态可辨。"""
    from datetime import datetime as dt

    from app.db.models import HotspotSuggestion, WechatArticle, WechatPanLink
    from app.services import hotspot_agent as ha

    sug = HotspotSuggestion(user_id=1, keyword="花少2人格测试", kind="llm", growth=0,
                            plan="【做什么】测试入口整理", created_at=dt.now())
    session.add(sug)
    session.commit()
    # 资源库里有现成我方链
    art = WechatArticle(user_id=1, title="花少2人格测试入口", author="号A",
                        url="https://mp.weixin.qq.com/s/x",
                        pan_urls="https://pan.quark.cn/s/gold", my_pan_urls="https://pan.quark.cn/s/mine1")
    session.add(art)
    session.flush()
    session.add(WechatPanLink(user_id=1, article_id=art.id, pan_url="https://pan.quark.cn/s/gold"))
    session.commit()

    captured = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content":
                "标题甲|标题乙|标题丙" + chr(10) + chr(10) + "正文草稿内容"}}]}

    monkeypatch.setattr(ha, "_draft_llm_check", None, raising=False)  # noqa: ARG005
    import app.services.llm_client as llm
    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: _Resp())
    monkeypatch.setattr(llm, "_record_usage", lambda *a, **k: None)

    out = ha.generate_draft(session, 1, sug.id, settings=_agent_settings(deepseek_api_key="sk-x"))
    assert out["status"] == "ok"
    assert out["titles"] == ["标题甲", "标题乙", "标题丙"]
    assert "mine1" in out["my_link"]          # 资源库我方链自动带上
    # 公众号 SEO 模式(2026-10-01):正文**不放链**(带外链影响微信收录与排名),
    # 链接挪到「公众号配置」区,由运营配到关键词自动回复里
    assert "mine1" not in out["content"]
    session.refresh(sug)
    assert "标题甲" in sug.draft
    assert "mine1" in sug.draft               # 链接在配置区,不在正文

    # 未配 key → 明确状态
    assert ha.generate_draft(session, 1, sug.id, settings=_agent_settings())["status"] == "no_llm_key"
    # 归属校验
    assert ha.generate_draft(session, 99, sug.id, settings=_agent_settings())["status"] == "not_found"


def test_platform_family_normalizes_multi_listing_ids() -> None:
    """同平台的多个榜单算一个平台。

    36氪有 quick/renqi/主榜三条、财联社有 hot/depth/telegraph —— 标题常常一模一样,
    不归一的话"同一个 36氪内容挂三个 id"会被当成"3 平台共振"霸榜,把真正跨平台的
    热点(如豆瓣+爱奇艺同现的剧)挤下去。2026-10-01 平台扩容后实测踩到这个。
    """
    from app.services.hotspot_agent import _family

    assert _family("36kr-quick") == _family("36kr-renqi") == _family("36kr") == "36kr"
    assert _family("cls-hot") == _family("cls-depth") == "cls"
    assert _family("douban") == "douban"      # 不在映射表里的原样返回
    assert _family("") == ""


def test_settle_revives_reads_gain_from_read_count(session) -> None:
    """★ **reads_gain 复活**(2026-10-04)。

    它当初停用的理由写在 docstring 里:「dajiala 阅读采样放弃后**无免费阅读数源**」——
    而现在**有了**(微信读书列表接口带精确 `readNum`,额度轮转也修好了)。
    口径:走 `conversion` 单一事实源(**公众号第一原则 = 阅读数 × 30%**)。
    """
    from app.db.models import WechatArticle
    from app.services import conversion
    from app.services.hotspot_agent import settle_suggestions

    link = "https://pan.quark.cn/s/reads1"
    now = dt.datetime.now()
    session.add(WechatArticle(id=81, user_id=1, title="有阅读数的发文", author="我",
                              source="manual", url="https://mp.weixin.qq.com/s/r1",
                              my_pan_urls=link, read_num=1000,
                              created_at=now - dt.timedelta(days=1)))
    session.commit()
    session.add(HotspotSuggestion(user_id=1, keyword="kw", kind="match", category="影视",
                                  link=link, acted=True, acted_at=now - dt.timedelta(hours=2)))
    session.commit()

    settle_suggestions(session, 1)
    row = session.scalars(select(HotspotSuggestion)).one()
    expect = conversion.estimate("wechat", {"read_num": 1000})["estimate"]
    assert row.reads_gain == expect == 300, "阅读数 × 30% 必须走 conversion 算出来"

    # 品类聚合里两列**并列**(口径不同,互相印证,不是替代)
    out = settle_suggestions(session, 1)
    cat = out["by_category"]["影视"]
    assert cat["reads_gain"] == 300 and "repost_gain" in cat


def test_settle_leaves_reads_gain_alone_when_no_read_count(session) -> None:
    """⚠️ **`read_num` 为 0 时不写** —— 0 是"这一轮没采到"(列表额度轮转,每轮 25 个号、
    ~2 天一圈),**不是"没人读"**。写成 0 会让"没采样"和"热度为零"混成一样(本仓的老毛病)。"""
    from app.db.models import WechatArticle
    from app.services.hotspot_agent import settle_suggestions

    link = "https://pan.quark.cn/s/reads0"
    now = dt.datetime.now()
    session.add(WechatArticle(id=82, user_id=1, title="还没轮到采的号", author="我",
                              source="manual", url="https://mp.weixin.qq.com/s/r2",
                              my_pan_urls=link, read_num=0,
                              created_at=now - dt.timedelta(days=1)))
    session.commit()
    session.add(HotspotSuggestion(user_id=1, keyword="kw", kind="match",
                                  link=link, acted=True, acted_at=now - dt.timedelta(hours=2)))
    session.commit()

    settle_suggestions(session, 1)
    row = session.scalars(select(HotspotSuggestion)).one()
    assert row.settled_at is not None, "还是要结算(repost_gain 那条路照走)"
    assert int(row.reads_gain or 0) == 0, "没采到就保持 0,不许编一个值"


# ------------------------------------------------ agent 完善:资源库证据
def test_library_evidence_boosts_and_tags(session, monkeypatch) -> None:
    """★ **资源库证据**:库里有没有这个资源、我方有没有现成的链 ⇒ 加权 + 打标。

    为什么这条值得单独一档:榜单说"**大家在讨论**",而资源库说
    "**已经有人在发这个资源的盘链**" —— 对拉新业务,后者更接近可执行;
    ⭐ 若我方**已经有链**,那就是"**点一下就能发**"(最高档)。
    """
    from app.services import hotspot_agent as ha
    from app.services import resource_library

    def fake_search(db, uid, q, days=30, limit=1):
        if q == "有链的热点":
            return [{"pan_url": "p1", "my_link": "our1", "titles": ["某某资源全集"]}]
        if q == "没链的热点":
            return [{"pan_url": "p2", "my_link": "", "titles": ["另一个资源"]}]
        return []

    monkeypatch.setattr(resource_library, "search_resources", fake_search)
    hotspots = [{"keyword": "有链的热点", "growth": 10, "effective_growth": 10},
                {"keyword": "没链的热点", "growth": 10, "effective_growth": 10},
                {"keyword": "库里没有的热点", "growth": 10, "effective_growth": 10},
                {"keyword": "短", "growth": 10, "effective_growth": 10}]
    ha._library_evidence(session, 1, hotspots)

    assert hotspots[0]["library_boost"] == ha.LIBRARY_BOOST_WITH_LINK == 1.4
    assert hotspots[0]["effective_growth"] == 14.0
    assert "库内已有链" in ha._library_tag(hotspots[0])

    assert hotspots[1]["library_boost"] == ha.LIBRARY_BOOST_NO_LINK == 1.2
    assert hotspots[1]["effective_growth"] == 12.0
    assert "还没搬" in ha._library_tag(hotspots[1])

    # ⚠️ **拿不到不算负面**:匹配不上就是 1.0,**不惩罚**
    # ("库里没有"可能是"我们还没搬",不是"这事不行")
    assert hotspots[2]["library"] is None and hotspots[2]["library_boost"] == 1.0
    assert hotspots[2]["effective_growth"] == 10.0
    assert ha._library_tag(hotspots[2]) == ""

    # 两字以下不检索(与 search_resources 同口径)
    assert hotspots[3]["library"] is None and hotspots[3]["library_boost"] == 1.0


def test_library_evidence_survives_search_failure(session, monkeypatch) -> None:
    """★ 检索炸了**不该拖垮选题** —— 这一档是"锦上添花",不是主路径。"""
    from app.services import hotspot_agent as ha
    from app.services import resource_library

    def boom(*a, **k):
        raise RuntimeError("库查询炸了")
    monkeypatch.setattr(resource_library, "search_resources", boom)

    hotspots = [{"keyword": "随便什么热点", "growth": 5, "effective_growth": 5}]
    ha._library_evidence(session, 1, hotspots)          # 不该抛
    assert hotspots[0]["library_boost"] == 1.0
    assert hotspots[0]["effective_growth"] == 5


def test_library_evidence_does_not_resurrect_a_none_growth(session, monkeypatch) -> None:
    """⚠️ 加权要基于**现有的** effective_growth;拿不到就当 0,**别编一个数**。"""
    from app.services import hotspot_agent as ha
    from app.services import resource_library

    monkeypatch.setattr(resource_library, "search_resources",
                        lambda *a, **k: [{"pan_url": "p", "my_link": "ours", "titles": ["x"]}])
    hotspots = [{"keyword": "某某热点", "growth": 0, "effective_growth": 0}]
    ha._library_evidence(session, 1, hotspots)
    assert hotspots[0]["effective_growth"] == 0.0, "0 × 1.4 还是 0,不该被'补'成别的数"


# ------------------------------- agent 完善:跨平台资源同现(2026-10-05)
def test_cross_platform_evidence_boosts_only_when_seen_on_two_plus(session) -> None:
    """★★ **跨平台资源同现**是现在能拿到的**最硬**的需求证据:
    榜单说"大家在**讨论**",而"同一个资源在**多个平台**都被贴过"说
    "**有人已经在不止一处靠它拉新**"(与"多号同发"同一逻辑,但把**号**扩到了**平台**)。

    ⚠️ **必须 `>=2` 个平台才加权** —— 单个平台命中是常态(某个词本来就在某平台流行),
    给它加分等于给所有词加一样的分,没有信息量。
    """
    from app.db.models import DiscoveredPanLink
    from app.services import hotspot_agent as ha

    now = dt.datetime.now()
    for plat, title in (("weibo", "武林外传 全集 网盘"), ("zhihu", "武林外传 资源"),
                        ("tieba", "武林外传 下载"), ("weibo", "别的资源")):
        session.add(DiscoveredPanLink(user_id=1, platform=plat, origin_url=f"u-{title}",
                                      title=title, found_at=now))
    session.commit()

    hs = [{"keyword": "武林外传", "growth": 10, "effective_growth": 10},   # 3 个平台
          {"keyword": "别的资源", "growth": 10, "effective_growth": 10},   # 只 1 个平台
          {"keyword": "短", "growth": 10, "effective_growth": 10}]         # 两字以下不匹配
    ha._cross_platform_evidence(session, 1, hs)

    assert hs[0]["platforms_found"] == ["tieba", "weibo", "zhihu"]
    assert hs[0]["cross_boost"] == 1.6 and hs[0]["effective_growth"] == 16.0
    assert "跨平台资源同现" in ha._cross_platform_tag(hs[0])
    assert "微博" in ha._cross_platform_tag(hs[0])

    assert hs[1]["cross_boost"] == 1.0, "单平台不该加分"
    assert hs[1]["effective_growth"] == 10.0
    assert ha._cross_platform_tag(hs[1]) == ""

    assert hs[2]["cross_boost"] == 1.0 and hs[2]["platforms_found"] == []


def test_cross_platform_counts_wechat_as_a_platform(session) -> None:
    """★ **公众号算一个平台** —— 同一个资源既被公众号发、又被微博贴 = 跨平台同现,
    这是最典型的"已经在运行"信号。"""
    from app.db.models import WechatArticle, WechatPanLink
    from app.services import hotspot_agent as ha
    from app.db.models import DiscoveredPanLink

    now = dt.datetime.now()
    art = WechatArticle(user_id=1, author="某号", title="齐民要术 资源合集",
                        url="https://mp.weixin.qq.com/s/x", created_at=now)
    session.add(art)
    session.flush()
    session.add(WechatPanLink(user_id=1, article_id=art.id, pan_url="https://pan.quark.cn/s/a",
                              created_at=now))
    session.add(DiscoveredPanLink(user_id=1, platform="weibo", origin_url="u2",
                                  title="齐民要术 网盘", found_at=now))
    session.commit()

    hs = [{"keyword": "齐民要术", "growth": 10, "effective_growth": 10}]
    ha._cross_platform_evidence(session, 1, hs)
    assert set(hs[0]["platforms_found"]) == {"wechat", "weibo"}
    assert hs[0]["cross_boost"] == 1.3
    assert "公众号" in ha._cross_platform_tag(hs[0])


def test_cross_platform_ignores_old_and_unmatched(session) -> None:
    """只看窗口内的;匹配不上的**不加分**(不是减分)—— "没发现"≠"不行"。"""
    from app.db.models import DiscoveredPanLink
    from app.services import hotspot_agent as ha

    old = dt.datetime.now() - dt.timedelta(days=60)
    session.add(DiscoveredPanLink(user_id=1, platform="weibo", origin_url="o1",
                                  title="老资源 网盘", found_at=old))
    session.commit()
    hs = [{"keyword": "老资源", "growth": 5, "effective_growth": 5},
          {"keyword": "谁也没提过", "growth": 5, "effective_growth": 5}]
    ha._cross_platform_evidence(session, 1, hs, days=14)
    assert hs[0]["cross_boost"] == 1.0 and hs[0]["platforms_found"] == []
    assert hs[1]["cross_boost"] == 1.0 and hs[1]["effective_growth"] == 5.0


# ------------------------------- agent 完善:证据可观测性(2026-10-05)
def test_evidence_tag_lists_only_the_tiers_that_fired() -> None:
    """★★ **可观测性**:把"这条靠哪几档证据顶上来"摊开,而且**只列触发过的**。

    这是它真正的用处:一眼看出哪一档在起作用、**哪一档从没触发过** ——
    没触发的那档要么数据源没接上、要么阈值太高,两种都值得看一眼
    (本仓反复出现的"废弃链只摘了一半"就是这类:功能在、但永远不生效)。
    """
    from app.services import hotspot_agent as ha

    full = {"keyword": "k", "growth": 100, "baidu": {"rank": 3}, "weibo": {"rank": 9},
            "library": {"my_link": "our://x", "titles": ["t"]}, "library_boost": 1.4,
            "platforms_found": ["weibo", "zhihu", "tieba"], "cross_boost": 1.6,
            "category_weight": 1.2}
    tag = ha._evidence_tag(full)
    for want in ("百度榜×1.5", "微博榜×1.2", "资源库已有链×1.4", "跨平台(3个)×1.6",
                 "品类权重×1.2"):
        assert want in tag, f"{want} 没列出来:{tag}"

    # 只触发一档 → 只列那一档(不把没触发的补零,否则看不出"哪档没动")
    one = ha._evidence_tag({"keyword": "k", "growth": 1, "baidu": {"rank": 1}})
    assert "百度榜×1.5" in one and "微博榜" not in one and "跨平台" not in one

    # 一档都没有 → 空串(**这条信息本身也重要**:说明它完全靠原生热度上来)
    assert ha._evidence_tag({"keyword": "k", "growth": 1}) == ""

    # 资源库"待搬"(没我方链)要能区别于"已有链" —— 两者的可执行性完全不同
    assert "资源库待搬" in ha._evidence_tag(
        {"keyword": "k", "library": {"my_link": "", "titles": ["t"]}, "library_boost": 1.2})


def test_evidence_tally_calls_out_never_fired_tiers() -> None:
    """★ 体检行要**点名"一档都没触发"** —— 那是"接了但没生效"的信号,
    不是"今天恰好没有"。不点名的话,一条永远不生效的链能瞒很久。"""
    from app.services import hotspot_agent as ha

    t = ha._evidence_tally([{"keyword": "a"}, {"keyword": "b", "baidu": {"rank": 1}}])
    assert "百度榜1" in t and "微博榜0" in t
    assert "未触发" not in t, "有档触发过就不该整行报警"

    t2 = ha._evidence_tally([{"keyword": "a"}, {"keyword": "b"}])
    assert "⚠️未触发" in t2 and "微博榜" in t2
    assert "值得看一眼" in t2


def test_evidence_tag_is_wired_into_both_entry_types() -> None:
    """★ 两种展示行(有现成资源 / LLM 选题)**都要带** —— 否则可观测性只覆盖一半。"""
    import inspect

    from app.services import hotspot_agent as ha
    src = inspect.getsource(ha.run_hotspot_agent)
    assert src.count("_evidence_tag(h)") >= 2, "两种 entry 都要带证据标记"
    assert "_evidence_tally(fresh)" in src, "体检行要进推送头部"


def test_agent_records_a_run_so_health_can_see_it() -> None:
    """★ **agent 此前一行运行记录都不留**(2026-10-05 补)。

    后果有两层:⒜ 链路体检**根本看不见这个作业**;⒝ 更看不见"**哪档证据从没触发过**"——
    而那是"接了但没生效"的信号(数据源没接上 / 阈值太高),本仓反复出现的
    "废弃链只摘了一半"就是这类。所以把证据触发统计写进运行记录的 detail。
    """
    import inspect

    from app.services import hotspot_agent as ha

    tick_src = inspect.getsource(ha.hotspot_agent_tick_all_users)
    assert "_record_run" in tick_src, "tick 必须记运行记录"
    assert '"hotspot_agent"' in tick_src, "运行记录的 kind 要对(体检按它查)"
    assert "evidence_tally" in tick_src, "证据统计要写进 detail,否则体检读不到"

    # `run_hotspot_agent` 要把统计**带出去**给 tick 用
    assert "evidence_tally" in inspect.getsource(ha.run_hotspot_agent)
