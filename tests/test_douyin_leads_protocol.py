"""抖音线索「**从哪取数**」的选择测试(2026-10-08 接入纯协议那天加)。

`tests/test_douyin_leads.py` 验的是判据/去重/卡片,并且**autouse 关掉了纯协议**;
本文件专门验选择逻辑本身:什么时候走协议、什么时候回落、回落有没有留痕。

⚠️ 这里**一个真请求都不发** —— 协议层被整个替换成桩。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_publish_cutoff(monkeypatch):
    """发布时间过滤与本文件无关,关掉它免得干扰。"""
    monkeypatch.setattr("app.services.douyin_leads._min_publish_ts", lambda s: 0)


@pytest.fixture
def use_protocol(monkeypatch):
    """把这轮钉成**启用纯协议**(不依赖 .env 里正好是什么)。"""
    from config.settings import get_settings

    monkeypatch.setattr(get_settings(), "douyin_leads_use_protocol", True, raising=False)


def _row(**kw) -> dict:
    d = {"uid": "u1", "name": "某推广号", "share_count": 5, "metrics": {},
         "pan_link": "", "keyword": "词",
         "url": "https://www.douyin.com/video/7380308675841297704",
         "snippet": "《白泽的梦》diplay软件下载教程", "publish_at": 1759800000}
    d.update(kw)
    return d


@pytest.fixture
def mc_spy(monkeypatch):
    """把 MediaCrawler 换成**间谍**:记下有没有被调、被调时给了什么。"""
    from app.services import mediacrawler_source as mc

    calls: list[tuple[str, list[str]]] = []

    def fake_crawl(platform, keywords, **kw):
        calls.append((platform, list(keywords)))
        return [_row(uid="from-mc", url="https://www.douyin.com/video/1111111111111111111")]

    monkeypatch.setattr(mc, "crawl", fake_crawl)
    return calls


def _patch_protocol(monkeypatch, rows=None, err=None):
    """把协议层换成桩;**顺便把收到的 `session` 记下来**(那是要验的东西之一)。"""
    from app.services import douyin_leads as dl

    seen: list[object] = []

    def fake(keywords, session):
        seen.append(session)
        return (rows or []), err

    monkeypatch.setattr(dl, "_try_protocol", fake)
    return seen


# ---------------------------------------------------------------------------
# 走哪条路
# ---------------------------------------------------------------------------


def test_抖音优先走纯协议(monkeypatch, use_protocol, mc_spy):
    """协议有数据时**不开浏览器** —— 这是这次切换的全部意义。"""
    from app.services import douyin_leads as dl

    _patch_protocol(monkeypatch, rows=[_row()])
    leads, source = dl.collect_leads(["词"])
    assert source == "协议"
    assert len(leads) == 1 and leads[0]["author"] == "某推广号"
    assert mc_spy == [], "协议拿到数据了还去开浏览器 = 白烧几分钟"


def test_协议抛错要回落浏览器并且留痕(monkeypatch, use_protocol, mc_spy, caplog):
    """★ 回落**不能静默** —— 它本身就是"没搜到"的生产者。

    不喊出来的话,"协议挂了"会被读成"抖音上没人在推资源"(本仓最忌讳的假阴性)。
    """
    from app.services import douyin_leads as dl

    class _Boom(Exception):
        kind = "need_login"

    with caplog.at_level("WARNING"):
        _patch_protocol(monkeypatch, err=_Boom("2483 请先登录"))
        _, source = dl.collect_leads(["词"])
    assert source == "浏览器兜底"
    assert len(mc_spy) == 1, "协议失败必须回落,不能就此认输"
    assert any("回落浏览器" in r.message for r in caplog.records), "回落没留痕"
    assert any("need_login" in r.message for r in caplog.records), "没报出失败的类型"


def test_协议一条都没搜到也要回落(monkeypatch, use_protocol, mc_spy, caplog):
    """★ 这条是**有意的**:抖音限流时的响应是 `status_code=0 + data:[]`,
    **与"真没结果"形状完全相同** ⇒ 分不出来,就只能按"没搜到"处理。

    为什么敢这么设计:今天(改之前)**每一次都开浏览器**,回落最坏与今天持平。
    """
    from app.services import douyin_leads as dl

    with caplog.at_level("WARNING"):
        _patch_protocol(monkeypatch, rows=[])
        _, source = dl.collect_leads(["词"])
    assert source == "浏览器兜底"
    assert len(mc_spy) == 1
    assert any("一条都没搜到" in r.message for r in caplog.records)
    assert any("限流" in r.message for r in caplog.records), \
        "「全空」与「抛错」要能分辨 —— 前者可能是限流"


def test_非抖音平台直接走浏览器(monkeypatch, use_protocol, mc_spy):
    """协议只实现了抖音;快手/小红书照旧 —— **也别去调协议层**。"""
    from app.services import douyin_leads as dl

    seen = _patch_protocol(monkeypatch, rows=[_row()])
    _, source = dl.collect_leads(["词"], platform="kuaishou")
    assert source == "浏览器兜底"
    assert seen == [], "非抖音平台不该碰抖音协议层"
    assert len(mc_spy) == 1


def test_开关关掉就完全不碰协议(monkeypatch, mc_spy):
    """`douyin_leads_use_protocol=False` 时应当**连协议层都不进**(回滚开关要真能回滚)。"""
    from app.services import douyin_leads as dl
    from config.settings import get_settings

    monkeypatch.setattr(get_settings(), "douyin_leads_use_protocol", False, raising=False)
    seen = _patch_protocol(monkeypatch, rows=[_row()])
    _, source = dl.collect_leads(["词"])
    assert source == "浏览器兜底" and seen == []


def test_两条路都挂就抛错不吞(monkeypatch, use_protocol):
    """★ 协议挂了、浏览器也挂了 ⇒ **必须抛**。

    吞掉的话会被记成 `success(线索0)`,而这条链是**无人值守**跑的 ——
    失败将没有任何信号(2026-10-02 就是这么定的规矩)。
    """
    from app.services import douyin_leads as dl
    from app.services import mediacrawler_source as mc

    _patch_protocol(monkeypatch, err=RuntimeError("协议挂"))

    def boom(*a, **k):
        raise mc.MediaCrawlerError("扫码没通过")

    monkeypatch.setattr(mc, "crawl", boom)
    with pytest.raises(mc.MediaCrawlerError):
        dl.collect_leads(["词"])


# ---------------------------------------------------------------------------
# session 透传(不传就是静默不带凭据)
# ---------------------------------------------------------------------------


def test_session_必须透传给协议层(monkeypatch, use_protocol, mc_spy):
    """★ 这条守的是一个**真踩过的坑**:协议层读凭据**只在传了 session 时**才去查加密库,
    不传就回落到空的 `.env` ⇒ 请求不带登录态 ⇒ 2483,而**表现只是"搜不到"**。

    ⚠️ 但它**只覆盖到 `_try_protocol` 这一跳**。下一跳(→ `dps.search`)见下面那条 ——
    这里是**变异测试教我的**:把 `session=session` 从 `_try_protocol` 里删掉,这条照样绿。
    """
    from app.services import douyin_leads as dl

    sentinel = object()
    seen = _patch_protocol(monkeypatch, rows=[_row()])
    dl.collect_leads(["词"], session=sentinel)
    assert seen == [sentinel], "session 没透传 ⇒ 协议层会静默不带凭据"


def test_session_一路透传到_protocol_source内部(monkeypatch, use_protocol):
    """★ **覆盖整条链**:直接打到 `douyin_protocol_source.search`,别在中途把它换成桩。

    为什么非要有这一条:上面那条把 `_try_protocol` 整个替换掉了,
    于是"`_try_protocol` 里到底传没传 session"**不在它的视野内**。
    变异测试实测:删掉那一跳的 `session=session`,上面那条**照样绿**。
    """
    from app.services import douyin_leads as dl
    from app.services import douyin_protocol_source as dps

    got: list[dict] = []
    monkeypatch.setattr(dps, "search",
                        lambda keywords, **kw: (got.append(kw), [])[1])
    sentinel = object()
    dl._try_protocol(["词"], sentinel)
    assert got == [{"session": sentinel}], (
        f"session 没走到 `douyin_protocol_source.search`(收到的是 {got})⇒ "
        f"生产会静默不带凭据、回 2483,而表现只是「搜不到」"
    )


def test_find_leads_仍然只返回列表(monkeypatch, use_protocol, mc_spy):
    """薄封装**形状不变** —— 既有调用方(测试里到处都是)不该被迫改。"""
    from app.services import douyin_leads as dl

    _patch_protocol(monkeypatch, rows=[_row()])
    out = dl.find_leads(["词"])
    assert isinstance(out, list) and len(out) == 1
    assert "mark" in out[0]


def test_没有关键词时来源为空串(monkeypatch, use_protocol, mc_spy):
    """没有词 ⇒ 不发请求也不回落;来源留空,免得运行记录里出现一个假的来源。"""
    from app.services import douyin_leads as dl

    leads, source = dl.collect_leads([])
    assert leads == [] and source == "" and mc_spy == []
