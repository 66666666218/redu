"""跨平台资源热度单测(2026-10-02):抓**资源名** → 网盘链从**资源库**匹配。

用户口径:"小红书可以只抓取资源名称,然后网盘链接可以从资源库里面匹配,别的也照这个模式"
—— 关键洞察:平台上**有没有链不重要**,只要有人在做同一个资源,库里就有链。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db import models  # noqa: F401
from app.db.models import User, WechatArticle, WechatPanLink
from app.services import resource_presence as rp


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    db.add(User(id=1, username="u1", email="u1@b.c", password_hash="x", enabled=True))
    db.commit()
    yield db
    db.close()


class _S:
    brand_name = "念飞思雪"
    presence_platforms = "xiaohongshu,kuaishou"
    presence_names = 3
    feishu_webhook_xiaohongshu = "https://example.com/xhs"
    feishu_webhook_kuaishou = "https://example.com/ks"
    feishu_webhook = ""
    feishu_secret = ""
    presence_enabled = True


def _mk_library(session, title, author, pan_url, my=""):
    a = WechatArticle(user_id=1, title=title, author=author,
                      url=f"https://mp.weixin.qq.com/s/{author}", pan_urls=pan_url,
                      my_pan_urls=my, created_at=datetime.now() - timedelta(days=1))
    session.add(a)
    session.flush()
    session.add(WechatPanLink(user_id=1, article_id=a.id, pan_url=pan_url))
    session.commit()


def test_platforms_of_parses_and_ignores_unknown() -> None:
    class _K:
        presence_platforms = "xiaohongshu, kuaishou ,, 不存在的, xiaohongshu"

    assert rp.platforms_of(_K()) == ["xiaohongshu", "kuaishou"]

    class _Empty:
        presence_platforms = ""

    assert rp.platforms_of(_Empty()) == []          # 空配置 = 不探(而不是默认探一堆)


def test_probe_groups_by_name_and_attaches_library_link(session, monkeypatch) -> None:
    """核心链路:平台搜到的内容按**搜索词**归组 → 回库里匹配链 → 只留有命中的。

    这里是这条路的**关键洞察**:平台内容里没有链没关系,链来自**我们自己的资源库**。
    """
    from app.services import mediacrawler_source as mc

    _mk_library(session, "花少2人格测试", "号甲", "https://pan.quark.cn/s/LIB",
                my="https://pan.quark.cn/s/OUR")
    monkeypatch.setattr(mc, "available", lambda: (True, "ok"))
    monkeypatch.setattr(rp, "library_names", lambda s, u, top: ["花少2人格测试", "没人做的资源"])
    monkeypatch.setattr(mc, "crawl", lambda plat, kws: (
        [{"keyword": "花少2人格测试", "snippet": "《花少2》中你和谁最像？", "name": "森*", "url": "u"},
         {"keyword": "花少2人格测试", "snippet": "花少2旅行人格测试", "name": "6***6", "url": "u2"}]
        if plat == "xiaohongshu" else []))

    out = rp.probe(session, 1, settings=_S())
    assert out["status"] == "ok"
    assert len(out["items"]) == 1                       # 快手没结果 → 不产生条目
    it = out["items"][0]
    assert it["name"] == "花少2人格测试" and it["label"] == "小红书" and it["count"] == 2
    assert it["link"]["my_link"] == "https://pan.quark.cn/s/OUR"   # ← 链是从**库里**来的
    assert len(it["samples"]) == 2


def test_probe_without_tool_is_noop(session, monkeypatch) -> None:
    """MediaCrawler 不可用 → 明确 no_tool,不报错(发现任务不该被工具拖垮)。"""
    from app.services import mediacrawler_source as mc

    monkeypatch.setattr(mc, "available", lambda: (False, "未安装"))
    monkeypatch.setattr(rp, "library_names", lambda s, u, top: ["甲"])
    assert rp.probe(session, 1, settings=_S())["status"] == "no_tool"


def test_library_link_prefers_wechat_then_discovered(session) -> None:
    """匹配口径与资源库一致:公众号优先(带验证强度),发现链补缺口;都没有 → 空。"""
    _mk_library(session, "某资源", "号甲", "https://pan.quark.cn/s/W")
    r = rp._library_link(session, 1, "某资源")
    assert r["pan_url"] == "https://pan.quark.cn/s/W" and r["source"] == "公众号"
    assert rp._library_link(session, 1, "库里完全没有的词") == {}


def test_push_items_cards_per_platform(monkeypatch) -> None:
    """**每个平台推自己的群**,卡片里给出**库里匹配到的可用链**(点开即用)。"""
    from app.services import feishu_client

    sent = []

    class _C:
        def __init__(self, webhook, secret):
            self.hook = webhook

        def send_card(self, card):
            sent.append((self.hook, card))
            return True

    monkeypatch.setattr(feishu_client, "FeishuClient", _C)
    ok = rp.push_items([
        {"name": "花少2人格测试", "platform": "xiaohongshu", "label": "小红书", "count": 2,
         "samples": [], "link": {"my_link": "https://pan.quark.cn/s/OUR", "pan_url": ""}},
        {"name": "车机软件", "platform": "kuaishou", "label": "快手", "count": 1,
         "samples": [], "link": {}},
    ], _S())
    assert ok is True and len(sent) == 2
    hooks = {h for h, _ in sent}
    assert hooks == {"https://example.com/xhs", "https://example.com/ks"}
    xhs_card = next(c for h, c in sent if h == "https://example.com/xhs")
    assert "https://pan.quark.cn/s/OUR" in str(xhs_card)      # 可用链在卡片里
    ks_card = next(c for h, c in sent if h == "https://example.com/ks")
    assert "库内暂无链" in str(ks_card)                        # 库里没有就如实说


def test_probe_reports_partial_platform_failure(session, monkeypatch) -> None:
    """**部分**失败(小红书成了、快手挂了)要能被看见。

    它不抛错(单平台挂不该拖垮整轮),但平台名要进返回值的 `failed` ——
    否则 `presence_tick` 只记 success,而"有个平台天天在挂"从运行记录上完全查不出来
    (静默的部分失败,与"假成功"同一类)。
    """
    from app.services import mediacrawler_source as mc

    monkeypatch.setattr(mc, "available", lambda: (True, "ok"))
    monkeypatch.setattr(rp, "library_names", lambda s, u, top: ["甲"])

    def _crawl(plat, names, timeout=600):
        if plat == "kuaishou":
            raise mc.MediaCrawlerError("kuaishou 超时(600s)——多半卡在扫码登录")
        return [{"uid": "u", "name": "作者", "snippet": "甲", "keyword": "甲"}]

    monkeypatch.setattr(mc, "crawl", _crawl)
    out = rp.probe(session, 1, settings=_S())
    assert out["failed"] == ["kuaishou"]                  # 挂的那个**记名**
    assert len(out["items"]) == 1                         # 成了的那个照常产出


def test_probe_raises_when_every_platform_fails(session, monkeypatch) -> None:
    """全平台都挂 → 抛错(让整轮记 failed),不是"今天没热度"。"""
    from app.services import mediacrawler_source as mc

    monkeypatch.setattr(mc, "available", lambda: (True, "ok"))
    monkeypatch.setattr(rp, "library_names", lambda s, u, top: ["甲"])

    def _boom(plat, names, timeout=600):
        raise mc.MediaCrawlerError(f"{plat} 未安装")

    monkeypatch.setattr(mc, "crawl", _boom)
    with pytest.raises(mc.MediaCrawlerError) as ei:
        rp.probe(session, 1, settings=_S())
    assert "全部抓取失败" in str(ei.value)


def test_bilibili_uses_public_api_not_browser(session, monkeypatch) -> None:
    """B站 走**公开 API**(不开浏览器)—— MediaCrawler 抓它起不来
    (`Chromium distribution 'chrome' is not found`),而 wbi 签名本地可算、匿名即可搜。"""
    from app.services import cross_accounts as ca
    from app.services import mediacrawler_source as mc

    called = {"mc": 0}

    def _no_mc(*a, **k):
        called["mc"] += 1
        return []

    monkeypatch.setattr(mc, "crawl", _no_mc)
    monkeypatch.setattr(ca, "search_bilibili_videos",
                        lambda kw, limit=20, cookie="": [{"uid": "BV1", "name": "UP主",
                                                          "url": "https://b/v/1",
                                                          "snippet": kw, "pan_link": "",
                                                          "keyword": kw}])
    monkeypatch.setattr(rp, "_BILI_GAP", 0)
    rows = rp._crawl_platform("bilibili", ["甲", "乙"])
    assert len(rows) == 2                      # 两个词各搜一次
    assert called["mc"] == 0, "B站不该去开 MediaCrawler"


def test_bilibili_api_failure_is_visible(session, monkeypatch) -> None:
    """B站 API 硬失败(风控/网络)要让整轮**看得见**,不能混成"这个资源没人推"。"""
    from app.services import cross_accounts as ca

    def _boom(kw, limit=20, cookie=""):
        raise ca.SearchSourceError("B站返回 code=-412 请求被拦截")

    monkeypatch.setattr(ca, "search_bilibili_videos", _boom)
    monkeypatch.setattr(rp, "_BILI_GAP", 0)
    monkeypatch.setattr(rp, "library_names", lambda s, u, top: ["甲"])
    monkeypatch.setattr(rp, "platforms_of", lambda s: ["bilibili"])
    # 全平台都失败 → probe 统一抛 MediaCrawlerError(带上最后一个平台的原因)
    from app.services.mediacrawler_source import MediaCrawlerError
    with pytest.raises(MediaCrawlerError) as ei:
        rp.probe(session, 1, settings=_S())
    assert "全部抓取失败" in str(ei.value) and "-412" in str(ei.value)


def test_probe_does_not_need_mediacrawler_for_api_only_platforms(session, monkeypatch) -> None:
    """全是 API 平台时,MediaCrawler 装没装都不该拦(否则 B站 会被它连累成 no_tool)。"""
    from app.services import cross_accounts as ca
    from app.services import mediacrawler_source as mc

    monkeypatch.setattr(mc, "available", lambda: (False, "未安装"))
    monkeypatch.setattr(ca, "search_bilibili_videos",
                        lambda kw, limit=20, cookie="": [{"uid": "BV1", "name": "UP",
                                                          "url": "u", "snippet": kw, "pan_link": "",
                                                          "keyword": kw}])
    monkeypatch.setattr(rp, "_BILI_GAP", 0)
    monkeypatch.setattr(rp, "library_names", lambda s, u, top: ["甲"])
    monkeypatch.setattr(rp, "platforms_of", lambda s: ["bilibili"])
    out = rp.probe(session, 1, settings=_S())
    assert out["status"] == "ok" and len(out["items"]) == 1


# ------------------------------------------------ 配上了 → 自动转存 + 推我们的链
class _TransferS:
    presence_transfer_limit: int = 3


class _Sess:
    """桩 session:只记 `commit()` 次数。

    ⚠️ 原来这里传的是 `None` —— 而 `transfer_missing_links` **必须在慢活(转存)前提交**,
    于是加 `session.commit()` 就把三个测试打红了。**该改的是测试不是生产代码**:
    "传 None 也能跑"是测试的偷懒,生产调用永远带真 session。
    转存是网络慢活(1–3 秒/条),占着 SQLite 的单写者会把别的作业饿死
    (成批 `database is locked` / `作业心跳写入失败`,见 `tests/test_slowwork_guard.py`)。
    """

    def __init__(self) -> None:
        self.commits = 0

    def commit(self) -> None:
        self.commits += 1


def test_transfer_missing_links_only_touches_ones_without_our_link(monkeypatch) -> None:
    """★ **已有我方链的不碰** —— 重复转存既占盘又白打接口。"""
    from app.services import pan_discovery

    calls: list[str] = []
    sess = _Sess()
    commits_at_transfer: list[int] = []

    def _transfer(s, u, url, st, snippet=""):
        # 转存的那一刻,commit 必须**已经发生过** —— 否则写锁还握在手里
        commits_at_transfer.append(sess.commits)
        calls.append(url)
        return {"status": "ok", "our_url": f"our:{url}"}

    monkeypatch.setattr(pan_discovery, "transfer_pan_url", _transfer)

    items = [
        {"name": "有链的", "link": {"pan_url": "p1", "my_link": "already"}},   # 不碰
        {"name": "没链的", "link": {"pan_url": "p2", "my_link": ""}},          # 转
        {"name": "没匹配上", "link": {}},                                       # 不碰
    ]
    out = rp.transfer_missing_links(sess, 1, items, _TransferS())
    assert calls == ["p2"]
    assert commits_at_transfer == [1], (
        "**转存前必须先 commit 放掉写锁** —— SQLite 单写者,"
        f"整轮占着库会把别的作业饿死。实际转存时的 commit 次数:{commits_at_transfer}")
    assert out == {"attempted": 1, "ok": 1, "failed": 0, "skipped": 0}
    assert items[0]["link"]["my_link"] == "already"      # 原样不动
    assert items[1]["link"]["my_link"] == "our:p2"


def test_transfer_missing_links_respects_the_batch_cap(monkeypatch) -> None:
    """★ **每轮有上限** —— 转存是网络写操作且占盘,一轮开几十个会把盘顶满。
    超额的要**说明原因**(不是静默跳过)。"""
    from app.services import pan_discovery

    monkeypatch.setattr(pan_discovery, "transfer_pan_url",
                        lambda *a, **k: {"status": "ok", "our_url": "our"})
    items = [{"name": f"r{i}", "link": {"pan_url": f"p{i}", "my_link": ""}} for i in range(5)]
    out = rp.transfer_missing_links(_Sess(), 1, items, _TransferS())          # limit=3
    assert out["attempted"] == 3 and out["ok"] == 3 and out["skipped"] == 2
    assert "额度已用完" in items[4]["link"]["transfer_error"]


def test_transfer_failure_keeps_the_item_and_records_why(monkeypatch) -> None:
    """★ **失败不改判**:转存没成,这条**照样带着原链推出去**,只把原因记下来。
    别让"没转成"变成"这条没热度"(与推送口径里"未搬要说明原因"同源)。"""
    from app.services import pan_discovery

    monkeypatch.setattr(pan_discovery, "transfer_pan_url",
                        lambda *a, **k: {"status": "failed", "our_url": "",
                                         "message": "盘满/单个资源太大"})
    items = [{"name": "r", "link": {"pan_url": "p", "my_link": ""}}]
    out = rp.transfer_missing_links(_Sess(), 1, items, _TransferS())
    assert out["failed"] == 1 and out["ok"] == 0
    assert "单个资源太大" in items[0]["link"]["transfer_error"]
    assert items[0]["link"]["pan_url"] == "p"            # 条目本身没被丢掉


def test_transfer_can_be_disabled(monkeypatch) -> None:
    """`presence_transfer_limit = 0` = 不自动转存(退回旧行为:只附库里已有的链)。"""
    from app.services import pan_discovery

    called = []
    monkeypatch.setattr(pan_discovery, "transfer_pan_url",
                        lambda *a, **k: called.append(1) or {"status": "ok"})

    class _Off:
        presence_transfer_limit = 0

    items = [{"name": "r", "link": {"pan_url": "p", "my_link": ""}}]
    out = rp.transfer_missing_links(_Sess(), 1, items, _Off())
    assert called == [] and out == {"attempted": 0, "ok": 0, "failed": 0, "skipped": 1}


class _PresS:
    presence_names = 3
    presence_platforms = "xiaohongshu,bilibili"


class TestPartialFailureAlerts:
    """★ **部分失败也要有人知道**(2026-10-06)。

    原来它只把平台名拼进运行记录的 detail(注释里自己写着"否则查不出来"),
    而**运行记录没人天天看** —— 实测小红书登录态过期后**连挂 3 天**
    (10-04/05/06 各失败一次),最后是用户来问"群里的机器人都配置好了吗"才被发现。
    """

    def _patch(self, monkeypatch, boom: str | None = "xiaohongshu", plats=("xiaohongshu", "bilibili")):
        from app.services import mediacrawler_source as mc

        monkeypatch.setattr(mc, "available", lambda: (True, "ok"))
        monkeypatch.setattr(rp, "library_names", lambda s, u, top=5: ["某个资源"])
        monkeypatch.setattr(rp, "platforms_of", lambda st: list(plats))

        def _crawl(plat, names):
            if boom is None or plat == boom:
                raise mc.MediaCrawlerError(f"{plat} 超时(600s)——多半卡在扫码登录")
            return [{"keyword": "某个资源", "snippet": "x", "uid": "u1", "name": "n",
                     "url": "http://x"}]
        monkeypatch.setattr(rp, "_crawl_platform", _crawl)

    def test_部分失败推告警并给出扫码命令(self, session, monkeypatch) -> None:
        sent: list = []
        import app.services.alert_service as asvc
        monkeypatch.setattr(asvc, "notify_incident", lambda *a, **k: sent.append(a) or True)
        self._patch(monkeypatch)

        out = rp.probe(session, 1, _PresS())

        assert out["failed"] == ["xiaohongshu"], out
        assert sent, "部分失败必须推告警 —— 只写进运行记录等于没人知道"
        # notify_incident(db, uid, kind, title, detail) ⇒ [3]=标题 [4]=详情
        title, detail = sent[0][3], sent[0][4]
        assert "小红书" in title, title
        assert "--platform xhs" in detail, f"要给**能照着做的那一步**(平台 id):{detail[:180]}"

    def test_全部失败仍然冒泡(self, session, monkeypatch) -> None:
        """⚠️ 反向:全挂必须抛(不能记成 success 空)—— 别被新加的告警顺手吞掉。"""
        import app.services.alert_service as asvc
        monkeypatch.setattr(asvc, "notify_incident", lambda *a, **k: True)
        self._patch(monkeypatch, boom=None)

        with pytest.raises(Exception):
            rp.probe(session, 1, _PresS())


class TestPlatformSplit:
    """★ **四个平台的成本差一个数量级,所以拆成两条作业**(2026-10-07)。

    用户口径:「小红书/B站/贴吧能否跟抖音一样两小时一轮」。
    查下来的事实:B站走**公开 API**(不开浏览器、无风控),而小红书/快手/贴吧走 MediaCrawler
    **每轮各开一次浏览器**(实测小红书单次 157 秒 ⇒ 一轮 5–8 分钟)。
    加密到 2 小时 ⇒ 12 轮/天 ≈ 1~1.5 小时浏览器自动化 + 风控暴露 ×12。
    ⇒ **B站单独每 2 小时;那三个保持每天两轮**。
    """

    def test_浏览器平台不含b站(self) -> None:
        from config.settings import Settings
        s = Settings(_env_file=None, is_dev=True)
        allp = rp.platforms_of(s)
        assert "bilibili" in allp, "前提:B站是配了的"
        assert "bilibili" not in rp.browser_platforms_of(s), \
            "B站不该出现在'走浏览器'那份里 —— 它有自己每 2 小时的快作业"

    def test_没配b站时空转不发请求(self, monkeypatch) -> None:
        """⚠️ 反向:没配 B站时 `presence_bili_tick` 要**直接返回 0**,别去空跑一轮采集。"""
        from config.settings import Settings
        s = Settings(_env_file=None, is_dev=True, presence_platforms="xiaohongshu,tieba")
        monkeypatch.setattr("app.services.resource_presence.presence_tick",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该跑")))
        assert rp.presence_bili_tick(s) == 0

    def test_b站快作业只带b站(self, monkeypatch) -> None:
        from config.settings import Settings
        s = Settings(_env_file=None, is_dev=True)
        seen: dict = {}
        monkeypatch.setattr("app.services.resource_presence.presence_tick",
                            lambda st, platforms=None: seen.update(p=platforms) or 0)
        rp.presence_bili_tick(s)
        assert seen.get("p") == ["bilibili"], f"只该带 B站,实际 {seen}"
