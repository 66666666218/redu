"""链路全检脚本(2026-10-04)。

它存在的理由:用户要「链路都跑通之后再来完善 agent」——
所以在完善 agent **之前**,得先有一个**可重复跑**的东西回答"每条链到底通不通",
而不是靠人回忆。这里钉住的是它的**汇报口径**:红/黄/绿的判据要能自己复核。
"""
import os

import pytest

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

from app.services.chain_health import CHAINS, GREEN, RED, YELLOW, render  # noqa: E402


def test_render_counts_and_names_the_red_ones() -> None:
    """★ 红项要**点名** —— 体检报告的价值全在"先修哪个"这一句上。"""
    sections = [("依赖", [{"name": "WeRSS", "level": GREEN, "detail": "HTTP 200"},
                          {"name": "newsnow", "level": RED, "detail": "容器不存在"}]),
                ("产出", [{"name": "监听", "level": YELLOW, "detail": "13h 前"}]),
                ("凭证", [{"name": "迅雷", "level": GREEN, "detail": "0 天前"}])]
    txt = render(sections)
    assert "🔴 1" in txt and "🟡 1" in txt and "🟢 2" in txt
    assert "先修红的:newsnow" in txt
    assert all(t in txt for t in ("【依赖】", "【产出】", "【凭证】"))


def test_render_without_red_omits_the_fix_line() -> None:
    txt = render([("依赖", [{"name": "A", "level": GREEN, "detail": "ok"}])])
    assert "先修红的" not in txt


def test_every_chain_declares_how_to_judge_it() -> None:
    """每条链都要声明"用哪个字段判产出" —— 判据是体检的灵魂。

    ⚠️ `metric=None` 是**允许**的(有些链没有产出计数),但不能全是 None,
    否则这个体检就退化成了"只看有没有跑过",看不出"跑了但什么都没产出"。
    """
    assert len(CHAINS) >= 6
    assert any(m for _l, _k, m, _d in CHAINS), "至少要有一条链声明了产出字段"
    for entry in CHAINS:
        label, kind, metric, desc = entry[0], entry[1], entry[2], entry[3]
        assert label and kind and desc, (label, kind, desc)
        if metric:
            assert isinstance(metric, str) and "=" not in metric, metric
        # 侧标记只允许 local/remote(远程侧**不能按本地标准判红** —— 两库独立)
        assert (entry[4] if len(entry) > 4 else "local") in ("local", "remote")


def test_newsnow_is_not_red_on_a_wechat_only_instance(monkeypatch) -> None:
    """★ **依赖检查要按实例角色判断**。

    newsnow 只服务于 `hot_source`(role=**hotspot**),而本机是 role=**wechat**
    ⇒ **本来就不该有它**,报红是**误报**。
    ⚠️ 一条永远红的项会训练人忽略整份报告 —— 与"闸门失准"告警是同一条教训
    (那时也是"把不该报的报了",导致真问题淹在噪音里)。
    """
    import config.settings as cs

    from app.services import chain_health as ch

    monkeypatch.setattr(cs, "get_settings",
                        lambda: type("S", (), {"scheduler_role": "wechat"})())
    row = [r for r in ch.check_dependencies() if "newsnow" in r["name"]][0]
    assert row["level"] == GREEN, f"wechat 侧不该报红,实际 {row}"
    assert "不该跑热榜" in row["detail"]


def test_newsnow_is_checked_on_a_hotspot_instance(monkeypatch) -> None:
    """但**热点侧就必须真查** —— 那边它是要用的,缺了就该红/黄。"""
    import config.settings as cs

    from app.services import chain_health as ch

    monkeypatch.setattr(cs, "get_settings",
                        lambda: type("S", (), {"scheduler_role": "hotspot"})())
    row = [r for r in ch.check_dependencies() if "newsnow" in r["name"]][0]
    assert row["level"] in (RED, YELLOW), f"hotspot 侧必须真查,实际 {row}"


def test_remote_side_chain_is_not_flagged_red_locally(session) -> None:
    """★ **远程侧的链不能按本地标准判红**(2026-10-05 补)。

    两库独立 —— 本机**本来就查不到**远程作业的运行记录,报红就是**误报**。
    而"一条永远红的项会训练人忽略整份报告"(与"闸门失准"告警同一条教训:
    那次也是把不该报的报了,真问题淹在噪音里)。
    """
    from app.services import chain_health as ch

    rows = ch.check_chains(session, days=3)
    agent = [r for r in rows if "Agent" in r["name"]]
    assert agent, "热点选题 Agent 应在体检表里"
    assert agent[0]["level"] != ch.RED, "远程侧不该判红"
    assert "远程" in agent[0]["detail"]

@pytest.fixture()
def session():
    """这个脚本的 `check_chains` 要查库(虽然只读)。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db import models as _models      # 建表要靠它把模型注册进 metadata
    from app.db.database import Base

    assert _models is not None

    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    yield db
    db.close()


# ---------------- 阅读数覆盖专检(2026-10-05) ----------------

def _db_with(rows):
    """造一个只含 wechat_articles 的内存库;`rows` = [(days_ago, read_num)]。"""
    from datetime import datetime, timedelta

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db.models import Base, WechatArticle

    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    for ago, rn in rows:
        db.add(WechatArticle(user_id=1, title="t", url=f"https://mp.weixin.qq.com/s/{id(object())}",
                             read_num=rn, created_at=datetime.now() - timedelta(days=ago)))
    db.commit()
    return db


def test_采文正常但阅读数全丢_要判红() -> None:
    """★ **这一条对着 2026-10-05 的真实事故**:网页路被账号级拦了整整一周
    (近 3 天入库 162 篇、有阅读数的 0 篇),而"监听采文"那条链一直是绿
    —— 因为它看的是 `new=N`,**采到文章就绿,不关心阅读数**。

    ⇒ "采到文章"与"采到阅读数"是两件事,必须分开盯。少了这条,
       同一个事故可以再发生一次而没人知道。
    """
    from app.services.chain_health import check_read_num_coverage

    db = _db_with([(0, 0), (1, 0), (2, 0)])
    try:
        out = check_read_num_coverage(db)
    finally:
        db.close()
    assert out[0]["level"] == RED
    assert "全丢" in out[0]["detail"]
    # 报红时**必须给出下一步**,否则运维只知道坏、不知道修哪儿
    assert "weread_app_login" in out[0]["detail"]


def test_阅读数正常要判绿() -> None:
    from app.services.chain_health import check_read_num_coverage

    db = _db_with([(0, 27), (1, 15)])
    try:
        assert check_read_num_coverage(db)[0]["level"] == GREEN
    finally:
        db.close()


def test_近几天没新文时不下结论() -> None:
    """没有样本就**别假装知道** —— "统计不出来"和"统计为 0"不是一回事。"""
    from app.services.chain_health import check_read_num_coverage

    db = _db_with([(9, 27)])
    try:
        out = check_read_num_coverage(db)[0]
        assert out["level"] == YELLOW and "无从判断" in out["detail"]
    finally:
        db.close()


def test_覆盖率偏低判黄不判红() -> None:
    """红只留给"全丢";少量缺失是额度窗口的正常表现(不是故障)。"""
    from app.services.chain_health import check_read_num_coverage

    db = _db_with([(0, 5), (0, 0), (0, 0), (0, 0), (0, 0), (0, 0), (0, 0), (0, 0), (0, 0), (0, 0)])
    try:
        assert check_read_num_coverage(db)[0]["level"] == YELLOW
    finally:
        db.close()


# ---------------------------------------------------------------------------
# ★★ 2026-10-08 **埋点覆盖面审计**时发现的三处洞
# ---------------------------------------------------------------------------


def _chain_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db.database import Base
    from app.db import models  # noqa: F401

    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng)()


def _add_run(db, kind, detail, status="success", minutes_ago=1):
    from datetime import datetime, timedelta

    from app.db.models import RunRecord

    db.add(RunRecord(user_id=1, run_id=f"{kind}-{minutes_ago}", kind=kind, status=status,
                     detail=detail, started_at=datetime.now() - timedelta(minutes=minutes_ago)))
    db.commit()


def _row(db, name):
    from app.services.chain_health import check_chains

    return next(r for r in check_chains(db) if r["name"] == name)


class TestPartialFailureIsNotDrowned:
    """★ 洞一:`resource_presence` 的 detail 是 `平台2 命中12 失败:xiaohongshu` ——
    命中 12 > 0 ⇒ 原来判**绿**,而小红书已经**连挂 5 轮**。
    判据必须是「**有没有点名失败**」,不是「总数是不是 0」。"""

    def test_点名失败就不许判绿(self) -> None:
        db = _chain_session()
        try:
            _add_run(db, "resource_presence", "平台2 命中12 失败:xiaohongshu")
            r = _row(db, "小红书/贴吧·名字型热度")
            assert r["level"] in (RED, YELLOW), f"点名了失败还判 {r['level']}:{r['detail']}"
        finally:
            db.close()

    def test_连续几轮都点名失败要判红(self) -> None:
        db = _chain_session()
        try:
            for i in range(1, 5):
                _add_run(db, "resource_presence", "平台2 命中12 失败:xiaohongshu", minutes_ago=i)
            assert _row(db, "小红书/贴吧·名字型热度")["level"] == RED, "连挂多轮必须红"
        finally:
            db.close()

    def test_失败计数为零不算失败(self) -> None:
        """⚠️ 反面对照:`xunlei_group` 的 detail 里有 `失败0`(**计数**为 0)——
        把它读成"有失败"会把一切判红,告警立刻变噪音。"""
        from app.services.chain_health import _partial_failures

        assert _partial_failures("群18 新0 转存0 跳过0 失败0") == []
        assert _partial_failures("平台2 命中12 失败:xiaohongshu") == ["xiaohongshu"]
        assert _partial_failures("扫9个号 标题30条") == []


class TestUnwatchedChainsAreNowWatched:
    """★ 洞二:这几条链**以前根本不在体检里** —— 有运行记录、也有失败,
    但**没有任何一行报告会读到它们**("埋了但没人看")。"""

    def test_夸克口令在体检里且能解析无等号指标(self) -> None:
        """★ 它状态是 `success`,而 detail 写着 `试8 成功0 失败2;主因:雷电窗口不在前台` ——
        **作业没崩 ≠ 事情做成了**,实测白跑一整天(23 轮)没人知道。"""
        db = _chain_session()
        try:
            assert "quark_kouling" in {c[1] for c in CHAINS}
            _add_run(db, "quark_kouling",
                     "试8 成功0 (其中**三盘互通复用0**) 失败2;主因:环境前置检查未过:雷电窗口不在前台")
            r = _row(db, "网盘·夸克口令转存")
            assert "成功=0" in r["detail"], f"无等号写法没解析出来:{r['detail']}"
            assert r["level"] != GREEN, "一整轮一点都没成功,不许判绿"
        finally:
            db.close()

    def test_B站对标号扫描与闲鱼深采也在体检里(self) -> None:
        db = _chain_session()
        try:
            _add_run(db, "bili_account_scan", "B站限流 code=-352 风控校验失败", status="failed")
            assert _row(db, "B站·对标号扫描")["level"] == RED
            _add_run(db, "xianyu_deep", "items=10")
            assert _row(db, "闲鱼·深采")["level"] == GREEN
        finally:
            db.close()


def test_指标解析要容忍无等号写法() -> None:
    """★ 洞三:`标题0条` / `成功0` 这种**没有等号**的 detail,
    只认 `key=` 的话指标**永远解析不出来** ⇒ 落到 else 分支 ⇒ **默认判绿** ——
    等于加了个瞎指标(比不加更糟:它看着有指标)。"""
    db = _chain_session()
    try:
        _add_run(db, "bili_account_scan", "扫9个号 标题0条 冷却跳过36个")
        assert "标题=0" in _row(db, "B站·对标号扫描")["detail"]
        _add_run(db, "quark_kouling", "试8 成功0 失败2")
        assert "成功=0" in _row(db, "网盘·夸克口令转存")["detail"]
    finally:
        db.close()
