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
    # ⚠️ **小红书自 2026-10-07 改走"页面渲染"**(MediaCrawler 直连它回 461 CAPTCHA),
    # 所以它的桩要打在**新那条路**上;其余平台仍走 MediaCrawler。
    from app.services import xhs_page_source as _xhs

    monkeypatch.setattr(_xhs, "search", lambda kws: [
        {"keyword": "花少2人格测试", "snippet": "《花少2》中你和谁最像？", "name": "森*", "url": "u"},
        {"keyword": "花少2人格测试", "snippet": "花少2旅行人格测试", "name": "6***6", "url": "u2"}])
    monkeypatch.setattr(mc, "crawl", lambda plat, kws: [])

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
    """**每个平台推自己的群**;卡片**两处可点**:「资源」→我方链、「对标作品」→原帖。

    ★ 2026-10-16 用户口径:「都做成抖音推广格式,**既有网盘,也能看到对标的作品**」。
    原来**只有一处可点**(可用链),而「对标作品」既没有列、连 URL 都没被带出来
    ⇒ 员工看得到"有资源",却**核对不了这资源是谁在推、推成什么样**。
    """
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
         "samples": [], "sample_urls": ["https://www.xiaohongshu.com/explore/abc"],
         "link": {"my_link": "https://pan.quark.cn/s/OUR", "pan_url": ""}},
        {"name": "车机软件", "platform": "kuaishou", "label": "快手", "count": 1,
         "samples": [], "sample_urls": [], "link": {}},
    ], _S())
    assert ok is True and len(sent) == 2
    hooks = {h for h, _ in sent}
    assert hooks == {"https://example.com/xhs", "https://example.com/ks"}
    xhs_card = next(c for h, c in sent if h == "https://example.com/xhs")
    # ★ 两处可点,且分工说清:资源 → 我方链(**写盘名**),对标作品 → 原帖
    assert "https://pan.quark.cn/s/OUR" in str(xhs_card)
    assert "🔴夸克" in str(xhs_card), "用户 10-04 口径:写网盘名,别写「我方链」"
    assert "https://www.xiaohongshu.com/explore/abc" in str(xhs_card)
    assert "看作品" in str(xhs_card), "那一列得看得出点进去是什么"
    ks_card = next(c for h, c in sent if h == "https://example.com/ks")
    assert "pan." not in str(ks_card)                        # 库里没有就**不许**有链
    assert "源未给链接" in str(ks_card), "源没给链接也要如实说,别装成有"


def test_probe_reports_partial_platform_failure(session, monkeypatch) -> None:
    """**部分**失败(小红书成了、快手挂了)要能被看见。

    它不抛错(单平台挂不该拖垮整轮),但平台名要进返回值的 `failed` ——
    否则 `presence_tick` 只记 success,而"有个平台天天在挂"从运行记录上完全查不出来
    (静默的部分失败,与"假成功"同一类)。
    """
    from app.services import mediacrawler_source as mc

    monkeypatch.setattr(mc, "available", lambda: (True, "ok"))
    monkeypatch.setattr(rp, "library_names", lambda s, u, top: ["甲"])

    def _crawl(plat, names, timeout=600, session=None):
        if plat == "kuaishou":
            raise mc.MediaCrawlerError("kuaishou 超时(600s)——多半卡在扫码登录")
        return [{"uid": "u", "name": "作者", "snippet": "甲", "keyword": "甲"}]

    monkeypatch.setattr(mc, "crawl", _crawl)
    # ⚠️ **小红书自 2026-10-07 走的是页面渲染那条路,不再经过 `mc.crawl`** ——
    # 不桩它,这条用例会**真的去开一次浏览器搜一遍**(单元测试不该联网)。
    from app.services import xhs_page_source as _xhs

    monkeypatch.setattr(_xhs, "search", lambda kws: [
        {"uid": "u", "name": "作者", "snippet": "甲", "keyword": "甲", "url": "",
         "pan_link": ""}])
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

    from app.services import xhs_page_source as _xhs

    def _boom_xhs(kws):
        raise _xhs.XhsPageError("要安全验证")

    monkeypatch.setattr(mc, "crawl", _boom)
    monkeypatch.setattr(_xhs, "search", _boom_xhs)   # 小红书走的是这条新路
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

    def _transfer(s, u, url, st, snippet="", **k):
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
    assert out == {"attempted": 1, "ok": 1, "failed": 0, "skipped": 0, "reused": 0}
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
    assert called == [] and out == {"attempted": 0, "ok": 0, "failed": 0, "skipped": 1,
                                          "reused": 0}


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

        def _crawl(plat, names, session=None):
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

    def test_微博失败给的修法不是跑_MediaCrawler(self, session, monkeypatch) -> None:
        """★★★ 2026-10-13:**微博早在 10-07 就改走纯协议了,根本不经过 MediaCrawler**
        (`weibo_search` 头注 + `_crawl_platform` 的 `if plat == "weibo"`)。

        而告警原来不分平台一律叫人跑 `main.py --platform wb` —— 跑了也**白跑**:
        热度采集读的是**我们 cookie 库**里那一枚,MediaCrawler 的 `browser_data/`
        是**另一份档案**,扫完码库里那枚还是旧的。
        (与微信读书那条「叫人去开雷电模拟器」是同一类错误:**方向对、动作错**。)
        """
        import app.services.alert_service as asvc

        sent: list = []
        monkeypatch.setattr(asvc, "notify_incident", lambda *a, **k: sent.append(a) or True)
        self._patch(monkeypatch, boom="weibo", plats=("weibo", "bilibili"))

        rp.probe(session, 1, _PresS())

        assert sent, "微博失败必须推告警"
        title, detail = sent[0][3], sent[0][4]
        assert "微博" in title, title
        assert "weibo_export_cookie" in detail, f"要给**走得通**的那一步:{detail[:240]}"
        # ⚠️ 判据不能用 `"--platform wb" not in detail` —— 文案里**故意**留了一句
        #    「别去跑 `main.py --platform wb`」(把已知的错动作明写出来,免得人自己想起它)。
        #    要钉的是**"没把它当成推荐动作"**,所以看"推荐动作"才会有的那句话在不在。
        assert "用 App 扫一下即可" not in detail, (
            f"微博不走 MediaCrawler,不能把它当推荐动作给出去:{detail[:240]}")
        assert "别去跑" in detail, "要把「跑 MediaCrawler 没用」明写出来"

    def test_微博的风控类失败不该叫人去换cookie(self, session, monkeypatch) -> None:
        """⚠️ 反向:同样是微博失败,但 `kind=risk` ⇒ 该等,不是去换凭据。

        这条钉的是**判据本身**:告警必须看 `kind`,而不是看文案里有没有"登录"两个字。
        """
        import app.services.alert_service as asvc
        from app.services.mediacrawler_source import MediaCrawlerError

        sent: list = []
        monkeypatch.setattr(asvc, "notify_incident", lambda *a, **k: sent.append(a) or True)
        self._patch(monkeypatch, boom=None, plats=("weibo", "bilibili"))

        def _crawl(plat, names, session=None):
            if plat == "weibo":
                err = MediaCrawlerError("weibo(纯协议): 接口返回 ok=-100 —— 疑似风控")
                err.kind = "risk"
                raise err
            return [{"keyword": "某个资源", "snippet": "x", "uid": "u1", "name": "n",
                     "url": "http://x"}]
        monkeypatch.setattr(rp, "_crawl_platform", _crawl)

        rp.probe(session, 1, _PresS())

        assert sent, "失败必须推告警"
        detail = sent[0][4]
        assert "weibo_export_cookie" not in detail, (
            f"风控不是凭据问题,不该叫人去换 cookie:{detail[:240]}")
        assert "跳过" in detail, f"风控该「等」,让其余平台照常:{detail[:240]}"

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
        # ⚠️ **2026-10-07 语义扩展**:微博改成纯协议后也进了这条"走 API 的快车道"
        # ⇒ 这里不该再断言"只有 B站",而该断言"**只有 API 平台**"
        #(走浏览器的三个绝不能进来 —— 那才是这条测试真正要防的)。
        from app.services.resource_presence import API_PLATFORMS
        assert seen.get("p"), f"快作业没带任何平台:{seen}"
        assert set(seen["p"]) <= set(API_PLATFORMS), f"浏览器平台混进快车道了:{seen}"
        assert "xiaohongshu" not in seen["p"] and "kuaishou" not in seen["p"]


# ---------------------------------------------------------------------------
# ★ 2026-10-08:同一份资源的多个命名变体必须**合并成一行**
# ---------------------------------------------------------------------------


def test_同一份资源的两个命名变体合并成一行() -> None:
    """★ 用户报的现象:快手卡片上 `伪装直男` 与 `伪装直男等2个文件` 各占一行,
    且一行有链一行没有。

    `等N个文件` 是**盘商保存多文件分享时的自动命名**(夸克/迅雷都有),
    于是同一份资源在库里有两条标题、造出两个搜索词、爬两遍、出两行。
    """
    rows = [{"keyword": "伪装直男", "snippet": "a"},
            {"keyword": "伪装直男等2个文件", "snippet": "b"},
            {"keyword": "伪装直男", "snippet": "c"}]
    merged = rp.merge_hits_by_resource(rows, ["伪装直男", "伪装直男等2个文件"])
    assert len(merged) == 1, f"同一份资源该并成一行,实际 {merged}"
    g = merged[0]
    assert g["name"] == "伪装直男", "展示名取**最短**那个(裸名匹配面最宽)"
    assert len(g["hits"]) == 3, "内容数要相加"


def test_不同资源不许被合并() -> None:
    rows = [{"keyword": "伪装直男", "snippet": "a"},
            {"keyword": "完全不相干的另一个资源", "snippet": "b"}]
    merged = rp.merge_hits_by_resource(rows, ["伪装直男", "完全不相干的另一个资源"])
    assert len(merged) == 2


def test_不在本轮关键词里的命中要丢掉() -> None:
    rows = [{"keyword": "别的资源", "snippet": "a"}]
    assert rp.merge_hits_by_resource(rows, ["伪装直男"]) == []


def test_跨盘已有我方链就不另存一份(monkeypatch) -> None:
    """★ **三盘互通(跨盘那一层)**(2026-10-11,用户口径「**一个盘里面有一个资源就行**」)。

    上面那条 `link.get("my_link")` 只挡「**同一条链**已经搬过」—— 它挡不住
    「**同一资源已经在另一个盘里**」。实测场景:资源在夸克已经有了,而这条命中来自一条
    **百度**链 ⇒ `my_link` 是空的 ⇒ 照搬 ⇒ **两个盘各存一份**(正是要避免的浪费)。
    """
    from app.services import pan_discovery, resource_presence as rp

    calls: list[str] = []
    monkeypatch.setattr(
        pan_discovery, "transfer_pan_url",
        lambda s, u, url, st, snippet="", **k: (calls.append(url),
                                                {"status": "ok", "our_url": "our:" + url})[1])
    monkeypatch.setattr(pan_discovery, "reuse_if_have",
                        lambda s, u, t: {"my_link": "https://pan.quark.cn/s/HAVE"})

    items = [{"name": "某资源", "link": {"pan_url": "https://pan.baidu.com/s/1a", "my_link": ""}}]
    out = rp.transfer_missing_links(_Sess(), 1, items, _TransferS())

    assert calls == [], "库里已有 ⇒ **不该再打转存接口**(它占盘、又慢)"
    assert out["reused"] == 1 and out["attempted"] == 0, out
    assert items[0]["link"]["my_link"] == "https://pan.quark.cn/s/HAVE"


class TestXhsEgressGuard:
    """★★ **动用账号之前,先过出口门禁**(2026-10-13)。

    这条是用"一个号都活不过一天"换来的:一天内在一台机器上连登 4 个档案之后,
    **连游客请求都开始回 `300012`(`IP存在风险`)**。
    ⇒ 那时候**再换任何新号都没用** —— 号换了,出口没换。

    ⚠️ 判错的根源:把"IP 的**静态属性**"(家宽 / 不漂移 / 非机房)当成了
    "IP 没被标记"。**前者说明不了后者** —— 只有发一次游客请求才试得出来。

    顺序绝不能反:① 游客探针(零账号成本)② 确认 `-101` ③ **然后**才拿真号试。
    """

    def test_出口被标记时要拦住(self, monkeypatch) -> None:
        monkeypatch.setattr("app.services.xhs_net.ip_blocked",
                            lambda *a, **k: (True, "300012 / IP存在风险"))
        assert rp._xhs_egress_guard() is False

    def test_出口通时要放行(self, monkeypatch) -> None:
        monkeypatch.setattr("app.services.xhs_net.ip_blocked",
                            lambda *a, **k: (False, "-101 无登录信息"))
        assert rp._xhs_egress_guard() is True

    def test_探测自己失败时不能拦采集(self, monkeypatch) -> None:
        """⚠️ 门禁的目的是"别撞脏出口",不是"多一个会误报的开关"。
        网络抖一下就把采集停掉,是**另一种假故障**。
        """
        def _boom(*a, **k):
            raise RuntimeError("网络抖了一下")

        monkeypatch.setattr("app.services.xhs_net.ip_blocked", _boom)
        assert rp._xhs_egress_guard() is True

    def test_被拦时采集要抛错而不是返回空(self, monkeypatch) -> None:
        """★ **"被拦"不能混进"真没有"** —— 否则结论就成"小红书没热度"这种假结论。"""
        from app.services.mediacrawler_source import MediaCrawlerError

        monkeypatch.setattr(rp, "_xhs_egress_guard", lambda: False)
        with pytest.raises(MediaCrawlerError) as ei:
            rp._crawl_platform("xiaohongshu", ["美食"], None)
        assert "出口" in str(ei.value)


def test_微博走同行资料群_没配才回落(monkeypatch) -> None:
    """★ **2026-10-16 用户新建了「微博同行资料」群**:微博上推的资源发那个新群。

    ⚠️ 与 `feishu_webhook_weibo` **分开**的原因同抖音那条:那个 section 也被
    `alert_service` 的采集失败告警用着。另外**只对微博生效** —— 别的平台不许被带走。
    """
    from app.services import feishu_client

    sent = []

    class _C:
        def __init__(self, webhook, secret):
            self.hook = webhook

        def send_card(self, card):
            sent.append((self.hook, card))
            return True

    monkeypatch.setattr(feishu_client, "FeishuClient", _C)

    class _S2:
        feishu_webhook_weibo_peer = "https://example.com/wb-peer"
        feishu_webhook_kuaishou = "https://example.com/ks"
        feishu_webhook_weibo = "https://example.com/wb"
        feishu_secret = ""
        brand_name = ""

    rp.push_items([{"name": "某资源", "platform": "weibo", "label": "微博", "count": 1,
                    "samples": [], "sample_urls": [], "link": {}},
                   {"name": "另一个", "platform": "kuaishou", "label": "快手", "count": 1,
                    "samples": [], "sample_urls": [], "link": {}}], _S2())
    hooks = {h for h, _ in sent}
    assert "https://example.com/wb-peer" in hooks, "微博该发同行群"
    assert "https://example.com/wb" not in hooks, "旧的微博群不该再收到内容卡"
    assert "https://example.com/ks" in hooks, "⚠️ 别的平台不许被带走"


def test_卡片要带对标账号与对标数据(monkeypatch) -> None:
    """★ 用户口径(2026-10-16):**网盘监控只看 5 个东西** ——
    对标账号 / 对标作品 / 对标数据 / 资源 / 可用链。

    采集层本来就有 `name`(账号)与 `metrics`(转发·评论·赞),只是**没被带到卡上**
    ⇒ 卡上原来看不到"是谁在推、热度多少"。这里钉住它俩必须进卡。
    """
    from app.services import feishu_client

    sent: list[dict] = []

    class _C:
        def __init__(self, webhook, secret):
            self.hook = webhook

        def send_card(self, card):
            sent.append(card)
            return True

    monkeypatch.setattr(feishu_client, "FeishuClient", _C)

    class _S3:
        feishu_webhook_weibo = "https://example.com/wb"      # ⚠️ `_S` 里没有这一条 ⇒ 卡片根本发不出去
        feishu_secret = ""
        brand_name = ""

    rp.push_items([{"name": "花少2", "platform": "weibo", "label": "微博", "count": 2,
                    "samples": [], "sample_urls": ["https://weibo.com/1/2"],
                    "author": "某博主", "metrics": {"share_count": 8, "liked_count": 12},
                    "link": {"my_link": "https://pan.quark.cn/s/OUR", "pan_url": ""}}], _S3())
    blob = str(sent[0])
    assert "**对标账号**" in blob and "**对标数据**" in blob, "列头要有这两个"
    assert "某博主" in blob, "账号要进卡"
    assert "↗8" in blob and "♥12" in blob, f"热度要进卡:{blob[-400:]}"


def test_对标数据为0时写破折号而不是0() -> None:
    """⚠️ 0 会被读成"没人看" —— 拿不到就 `—`(与采集层"为 0 就不放进去"同一条口径)。"""
    assert rp._metrics_md({}) == "—"
    assert rp._metrics_md({"share_count": 0}) == "—"
    assert rp._metrics_md({"share_count": 3}) == "↗3"


def test_小红书优先走CDP路_且每轮只喂两个词(session, monkeypatch) -> None:
    """★ 用户 2026-10-16 拍板:「**接 CDP 路**」+「**每轮 1~2 词**」。

    为什么 CDP 排在页面路**前面**:CDP 用**真人开着的那个浏览器**(脚本只 `connect_over_cdp`,
    实测 `navigator.webdriver=False`);而页面路是 **Playwright 自己开**的(脚本形状)——
    历史上被 `461/300011` 打过的正是"设备特征对不上"这件事。**谁更像人谁先走。**
    ⚠️ 词数上限是**账号安全**问题,不是性能问题(2026-10-14 压测的代价就是账号)。
    """
    from app.services import xhs_cdp_source as cdp
    from app.services import xhs_page_source as _xhs

    called: dict = {}
    monkeypatch.setattr(cdp, "available", lambda settings=None: (True, "CDP 可达"))

    def _search(kws, settings=None, **k):
        called["kws"] = list(kws)
        return [{"uid": "u", "name": "作者", "snippet": "甲", "keyword": kws[0],
                 "url": "", "pan_link": ""}]

    monkeypatch.setattr(cdp, "search", _search)
    monkeypatch.setattr(rp, "library_names", lambda s, u, top: ["甲", "乙", "丙", "丁"])
    monkeypatch.setattr(_xhs, "search",
                        lambda *a, **k: pytest.fail("CDP 通了就不该再走页面路"))
    monkeypatch.setattr(rp, "_xhs_egress_guard", lambda: True)
    #: ⚠️ 词数上限是**从 settings 现取**的(那条分支在内层函数里,拿不到外层的 `settings`)
    #: ⇒ 用例必须**自己给一份 settings**,否则会读到 `.env` 里的真值
    #: (实测:`.env` 改成 8 之后这条用例就会变红 —— 而它测的是"上限生效",不是"上限是几")。
    monkeypatch.setattr("config.settings.get_settings",
                        lambda: type("S", (), {"xhs_max_keywords": 2})())
    out = rp.probe(session, 1, settings=_S(), platforms=["xiaohongshu"])
    assert called.get("kws") == ["甲", "乙"], f"每轮最多 2 个词,实际 {called.get('kws')}"
    assert out["items"], "CDP 路的产物要能走到 items"


def test_小红书_CDP窗口没开时回落页面路(session, monkeypatch) -> None:
    """⚠️ **窗口没开是"回落",不是"失败"** —— 但**必须把原因说出来**(别静默)。

    这正是这条路的生产形态:人开着窗口时走 CDP(更像人),关着时退回页面路(能用但形状差)。
    """
    from app.services import xhs_cdp_source as cdp
    from app.services import xhs_page_source as _xhs

    monkeypatch.setattr(cdp, "available",
                        lambda settings=None: (False, "CDP 不可达:先由人把浏览器开起来"))
    monkeypatch.setattr(cdp, "search", lambda *a, **k: pytest.fail("不可达就不该调它"))
    monkeypatch.setattr(rp, "library_names", lambda s, u, top: ["甲"])
    hit = {"n": 0}

    def _page(kws, *a, **k):
        hit["n"] += 1
        return [{"uid": "u", "name": "作者", "snippet": "甲", "keyword": "甲",
                 "url": "", "pan_link": ""}]

    monkeypatch.setattr(_xhs, "search", _page)
    monkeypatch.setattr(rp, "_xhs_egress_guard", lambda: True)
    out = rp.probe(session, 1, settings=_S(), platforms=["xiaohongshu"])
    assert hit["n"] == 1, "CDP 不可达 ⇒ 该回落页面路"
    assert out["items"]


def test_小红书的词要轮转_不是每天重复同一批(session) -> None:
    """★★ **2026-10-16 自查出来的坑**:小红书每轮只喂 2 个词(低频=账号安全),
    可如果每次都取 `names[:2]`,那**一天 6 轮搜的是同 2 个词** ——
    既没覆盖新资源,又让**同一批请求重复 6 次**(对风控比"搜不同词"更糟)。

    ⇒ 用游标每轮前进 n,一天把整份词表走一遍。
    ⚠️ **拿不到游标/没有会话 ⇒ 退回前 n 个**(绝不因为"转不动"就不发)。
    """
    names = ["甲", "乙", "丙", "丁", "戊", "己"]
    seen = [rp._xhs_rotating_slice(session, 1, names, 2) for _ in range(3)]
    assert seen == [["甲", "乙"], ["丙", "丁"], ["戊", "己"]], f"没轮转起来:{seen}"
    # 走完一圈要**绕回开头**(不是越界变空)
    assert rp._xhs_rotating_slice(session, 1, names, 2) == ["甲", "乙"]
    # 没有会话(离线/单测)⇒ 退回前 n 个,不许抛也不许空
    assert rp._xhs_rotating_slice(None, 1, names, 2) == ["甲", "乙"]
    assert rp._xhs_rotating_slice(None, 1, [], 2) == []


def test_词序游标要把写事务交掉() -> None:
    """★★★ 2026-10-16 21:13 实测的那次 **181 秒撞锁**。

    `flush()` 只把语句发出去、**事务还开着**,而紧接着跑的是 **CDP 爬取**
    (8 个词 × ~30 秒 ≈ 4 分钟)⇒ 这一轮**抱着 SQLite 的写锁**整整 181 秒,
    把同一时刻的 `bili_account_scan` 与 `runs` 自己的 INSERT 全打成 `database is locked`
    (app.log:`SQLite 撞锁…当前持有写锁的是 … _xhs_rotating_slice`,持锁 120.9s → 181.0s)。
    ⇒ 游标写进去之后**必须 commit**(本仓老纪律:**慢活一律不要在事务里做**)。
    """
    calls: list[str] = []

    class _Sess:
        def get(self, *a):
            return None

        def add(self, obj):
            calls.append("add")

        def commit(self):
            calls.append("commit")

        def rollback(self):
            calls.append("rollback")

        def flush(self):
            calls.append("flush")

    rp._xhs_rotating_slice(_Sess(), 1, ["甲", "乙"], 1)
    assert "commit" in calls, f"只 flush ⇒ 写锁会被抱到爬取结束(实测 181 秒):{calls}"


def test_慢爬之前先放掉写事务(monkeypatch) -> None:
    """同一条纪律的**结构性**守卫:`_crawl_platform` 一进门就要把写事务交掉。

    为什么放在**门槛**上而不只放在游标那里:这一层爬的是浏览器/爬虫(几分钟),
    只要调用方的会话里挂着**任何**未提交的写(游标、告警、别的平台的余量…),
    这整段时间写锁都在我们手里 ⇒ 别的作业只能 `database is locked`。
    """
    from app.services import mediacrawler_source as mcs

    order: list[str] = []

    class _Sess:
        def commit(self):
            order.append("commit")

        def rollback(self):
            order.append("rollback")

    def _crawl(plat, names, **k):
        order.append("crawl")
        return []

    monkeypatch.setattr(mcs, "crawl", _crawl)
    rp._crawl_platform("kuaishou", ["甲"], session=_Sess())
    assert order[:2] == ["commit", "crawl"], f"慢活之前没交写事务:{order}"


def test_push_items_逐平台结局要留痕(monkeypatch) -> None:
    """★★ 2026-10-16(用户报「快手…」时暴露的那条口径)。

    `push_items` **按平台各发一张卡**,而返回值只是"**至少有一个平台成功**"
    ⇒ 「快手那张没到、其余都到了」在 `runs.detail` 里与「全都到了」**长得一模一样**
    —— 而用户看的正是**那一个群**。⇒ `detail` 把每个平台的结局单独留下来。
    """
    from app.services import feishu_client

    class _C:
        def __init__(self, webhook, secret):
            self.hook = webhook

        def send_card(self, card):
            return "ks" not in self.hook                  #: 快手那个群**明确拒绝**

    monkeypatch.setattr(feishu_client, "FeishuClient", _C)
    detail: dict = {}
    ok = rp.push_items([
        {"name": "甲", "platform": "xiaohongshu", "label": "小红书", "count": 1,
         "samples": [], "sample_urls": [], "link": {}},
        {"name": "乙", "platform": "kuaishou", "label": "快手", "count": 1,
         "samples": [], "sample_urls": [], "link": {}},
    ], _S(), detail=detail)
    assert ok is True, "整体返回值仍是「至少有一个成功」——契约不变"
    assert detail == {"xiaohongshu": "已推", "kuaishou": "未推"}, detail


def test_没配群的平台也要留痕() -> None:
    """⚠️ `无群` 与 `未推` 是两件事:前者是**我们没配**,后者是**飞书没收**。"""
    class _S4:
        brand_name = ""
        feishu_secret = ""
        feishu_webhook = ""

    detail: dict = {}
    rp.push_items([{"name": "甲", "platform": "weibo", "label": "微博", "count": 1,
                    "samples": [], "sample_urls": [], "link": {}}], _S4(), detail=detail)
    assert detail == {"weibo": "无群"}, detail
