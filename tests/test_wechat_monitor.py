"""公众号监听/同步单测(dajiala 全 mock,零花费):盘链识别/加号/监听去重/翻页同步/余额保护。"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest
from datetime import datetime, timedelta
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db import models  # noqa: F401
from app.db.models import FeishuAlert, RunRecord, WechatArticle, WechatBenchmark, WechatCandidate, WechatPanLink, WechatTrafficSample
from config.settings import Settings
from app.services import wechat_monitor
from app.services.dajiala_client import DajialaClient
from app.services import feishu_client


@pytest.fixture(autouse=True)
def _no_quiet_hours(monkeypatch):
    """默认关闭免打扰(测试推送行为);时段判断本身单独测。

    生产代码在函数内 lazy import(from feishu_client import is_quiet_hours),
    所以要 patch 源头模块的属性。"""
    from app.services import feishu_client as fc
    monkeypatch.setattr(fc, "is_quiet_hours", lambda settings, now=None: False)
    monkeypatch.setattr(wechat_monitor, "is_quiet_hours", lambda settings, now=None: False)



def _settings(**kw) -> Settings:
    base = {"dajiala_key": "JZLTEST", "dajiala_min_balance": 1.0, "wechat_sync_max_pages": 2}
    base.update(kw)
    return Settings(_env_file=None, is_dev=True, **base)


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    yield db
    db.close()


class FakeClient:
    """按脚本回放的假 DajialaClient,记录调用序列。"""

    def __init__(self, remain: float = 10.0, pc: dict | None = None, hist: list[dict] | None = None):
        self.remain_value = remain
        self.pc_map = pc or {}          # anchor_url → post_condition 响应
        self.hist_pages = hist or []    # history_by_ghid 按调用次序回放
        self.calls: list[tuple] = []

    def remain_money(self) -> float:
        self.calls.append(("remain",))
        return self.remain_value

    def post_condition(self, url: str) -> dict:
        self.calls.append(("pc", url))
        if url in self.pc_map:
            return self.pc_map[url]
        return {"code": 0, "nickname": "微信派", "ghid": "gh_bc5ec2ee663f", "data": []}

    def history_by_ghid(self, ghid: str = "", article_url: str = "", offset: str = "") -> dict:
        self.calls.append(("hist", ghid, article_url, offset))
        idx = sum(1 for c in self.calls if c[0] == "hist") - 1
        return self.hist_pages[min(idx, len(self.hist_pages) - 1)]

    def read_zan_pro(self, url: str) -> dict:
        self.calls.append(("zan", url))
        return {"read": 100, "zan": 2, "looking": 3, "share_num": 4, "collect_num": 5, "comment_count": 6}


# ---------------------------------------------------------------- 盘链识别
def test_detect_pan_types_matches_all_four() -> None:
    text = ("夸克: https://pan.quark.cn/s/1a2b3c 百度: https://pan.baidu.com/s/abc-123 "
            "UC: https://drive.uc.cn/s/xyz 迅雷: https://pan.xunlei.com/s/t00")
    assert wechat_monitor.detect_pan_types(text) == ["夸克网盘", "百度网盘", "UC网盘", "迅雷云盘"]
    assert wechat_monitor.detect_pan_types("普通文章,没有任何链接") == []


def test_title_hits_keywords() -> None:
    assert wechat_monitor.title_hits("某资源全套分享")
    assert not wechat_monitor.title_hits("今天天气不错")


def test_fetch_article_content_strips_html_and_detects_antibot(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Resp:
        def __init__(self, status: int, text: str) -> None:
            self.status_code, self.text = status, text

    html_ok = '<html><div id="js_content"><p>链接 https://pan.quark.cn/s/aa </p></div><script>x</script></html>'
    monkeypatch.setattr(wechat_monitor.requests, "get",
                        lambda url, timeout, headers, allow_redirects=True: _Resp(200, html_ok))
    out = wechat_monitor.fetch_article_content("https://mp.weixin.qq.com/s/x")
    assert "pan.quark.cn/s/aa" in out and "<p>" not in out
    assert wechat_monitor.detect_pan_types(out) == ["夸克网盘"]

    monkeypatch.setattr(wechat_monitor.requests, "get",
                        lambda url, timeout, headers, allow_redirects=True: _Resp(200, "环境异常 请完成验证"))
    assert wechat_monitor.fetch_article_content("https://mp.weixin.qq.com/s/x") == ""


# ---------------------------------------------------------------- 正文里的链抽取
def test_fetch_article_content_keeps_anchor_href_and_original_link(monkeypatch) -> None:
    """资源号把夸克链做成超链接锚文本("点此保存")或只放在「阅读原文」里时也要抽得到。

    回归:旧实现 `re.sub(r"<[^>]+>", " ", html)` 把 <a href> 连同 URL 一起删了,
    msg_source_url 也从来没解析 → 正文有链却永远识别不到,飞书卡上一片"—"。"""
    class _Resp:
        def __init__(self, status: int, text: str) -> None:
            self.status_code, self.text = status, text

    page = ('<html><script>var msg_source_url = "https://pan.quark.cn/s/OnlyInReadOrig";'
            'document.title=""</script>'
            '<div id="js_content"><p>复制下面链接保存</p>'
            '<a href="https://pan.quark.cn/s/Anchored">点此保存</a></div><script>x</script></html>')
    monkeypatch.setattr(wechat_monitor.requests, "get",
                        lambda url, timeout, headers, allow_redirects=True: _Resp(200, page))
    out = wechat_monitor.fetch_article_content("https://mp.weixin.qq.com/s/y")
    assert "https://pan.quark.cn/s/Anchored" in out      # 锚文本后面的真链保住了
    assert "https://pan.quark.cn/s/OnlyInReadOrig" in out  # 阅读原文目标
    assert "document.title" not in out                   # 脚本整块丢掉,不再当正文
    assert wechat_monitor._extract_pan_urls("", out) == ["https://pan.quark.cn/s/Anchored",
                                                          "https://pan.quark.cn/s/OnlyInReadOrig"]


def test_fetch_article_content_rejects_js_shell_page(monkeypatch) -> None:
    """出口 IP 被微信挡时返回的是没有 #js_content 的 JS 壳页:判失败,不把十几 KB 脚本入库。"""
    class _Resp:
        def __init__(self, status: int, text: str) -> None:
            self.status_code, self.text = status, text

    shell = ("<html><script>(() => { const ua = navigator.userAgent;"
             "document.title = '微信公众平台'; })(); var PAGE_JS = 1</script></html>")
    monkeypatch.setattr(wechat_monitor.requests, "get",
                        lambda url, timeout, headers, allow_redirects=True: _Resp(200, shell))
    assert wechat_monitor.fetch_article_content("https://mp.weixin.qq.com/s/z") == ""


def test_backfill_pan_urls_repairs_body_links_missed_at_insert(session) -> None:
    """早于百度提取上线入库的文章:正文里 42 条百度链,`pan_urls` 却为空 → 永远不进补转存队列。

    回填后必须:补上 pan_urls/pan_types、写归一化表,才会被每轮补转存队列捞到换我方链。"""
    had = WechatArticle(user_id=1, title="网盘资料分享(可自取)", author="号A",
                        url="https://mp.weixin.qq.com/s/had", source="listen", pan_types="百度网盘",
                        content="链接：https://pan.baidu.com/s/1TgLPOEpzMg4neKwOsg8lUQ?pwd=6666 "
                                "链接：https://pan.quark.cn/s/abc123")
    blank = WechatArticle(user_id=1, title="纯资讯文", author="号B",
                          url="https://mp.weixin.qq.com/s/blank", source="listen",
                          content="北京骑手注意了,白牌电动车开始换外卖绿牌。")
    other = WechatArticle(user_id=2, title="别租户", author="号C",
                          url="https://mp.weixin.qq.com/s/other", source="listen",
                          content="链接:https://pan.baidu.com/s/1zzzzzzzzzzzzzzzzzzzzzzz")
    session.add_all([had, blank, other])
    session.commit()

    assert wechat_monitor._backfill_pan_urls(session, 1) == 1
    assert "pan.baidu.com/s/1TgLPOEpzMg4neKwOsg8lUQ" in had.pan_urls
    assert "pan.quark.cn/s/abc123" in had.pan_urls
    assert set(had.pan_types.split(",")) == {"百度网盘", "夸克网盘"}
    assert {p.pan_url for p in session.scalars(select(WechatPanLink)).all()} == {
        "https://pan.baidu.com/s/1TgLPOEpzMg4neKwOsg8lUQ",   # 归一化表按"文章×链接"建行
        "https://pan.quark.cn/s/abc123"}
    assert blank.pan_urls == "" and other.pan_urls == ""    # 无链行不动、跨租户不动
    assert wechat_monitor._backfill_pan_urls(session, 1) == 0  # 幂等:第二轮不再重复回填


def test_weread_mp_content_extracts_links_from_forwarded_page(monkeypatch) -> None:
    """微信读书转发页同样要保住 <a href> 与「阅读原文」,否则免费源的链永远抽不到。"""
    from app.services.weread_client import WereadClient

    class _Resp:
        status_code = 200
        text = ('<html><script>var msg_source_url = "https://pan.quark.cn/s/ORIG"</script>'
                '<div id="js_content"><a href="https://pan.quark.cn/s/ANCH">点这里</a></div>'
                '</body></html>')

    monkeypatch.setattr("app.services.weread_client.requests.get", lambda *a, **kw: _Resp())
    out = WereadClient("ck=1").mp_content("rev1")
    assert "https://pan.quark.cn/s/ANCH" in out and "https://pan.quark.cn/s/ORIG" in out
    assert "msg_source_url" not in out   # 脚本不再混进正文


# ---------------------------------------------------------------- 加号
def test_add_benchmark_free_and_dedupe(session, settings: Settings) -> None:
    st = _settings(dajiala_key="")  # 未配 key 也允许加号
    row = wechat_monitor.add_benchmark(session, 1, "https://mp.weixin.qq.com/s/abc", nickname="资源号甲")
    assert row["nickname"] == "资源号甲"
    with pytest.raises(ValueError):  # 同链接重复加
        wechat_monitor.add_benchmark(session, 1, "https://mp.weixin.qq.com/s/abc")
    with pytest.raises(ValueError):  # 非链接拒绝
        wechat_monitor.add_benchmark(session, 1, "资源号乙")


def test_add_benchmark_resolves_nickname_via_key(session, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeClient(pc={"https://mp.weixin.qq.com/s/abc": {
        "code": 0, "nickname": "微信派", "ghid": "gh_bc5ec2ee663f", "data": []}})
    monkeypatch.setattr(wechat_monitor, "DajialaClient", lambda key: fake)
    row = wechat_monitor.add_benchmark(session, 1, "https://mp.weixin.qq.com/s/abc", settings=_settings())
    assert row["nickname"] == "微信派" and row["ghid"] == "gh_bc5ec2ee663f"


# ---------------------------------------------------------------- 监听
def test_listen_inserts_new_and_dedupes(session) -> None:
    b = WechatBenchmark(user_id=1, nickname="号A", anchor_url="https://mp.weixin.qq.com/s/A")
    session.add(b)
    session.commit()
    fake = FakeClient(pc={"https://mp.weixin.qq.com/s/A": {"code": 0, "data": [
        {"title": "百度网盘资源合集", "url": "https://mp.weixin.qq.com/s/n1"},
        {"data": [{"title": "夸克网盘资源", "content_url": "https://mp.weixin.qq.com/s/n2"}]},
    ]}})
    monkey = pytest.MonkeyPatch()
    monkey.setattr(wechat_monitor, "fetch_article_content", lambda url, timeout=15: "")
    out = wechat_monitor.run_wechat_listen(session, 1, settings=_settings(), client=fake)
    monkey.undo()
    assert out["status"] == "success" and out["new"] == 2
    rows = session.scalars(select(WechatArticle)).all()
    assert {r.url for r in rows} == {"https://mp.weixin.qq.com/s/n1", "https://mp.weixin.qq.com/s/n2"}
    assert all(r.source == "listen" and r.benchmark_id == b.id for r in rows)
    pan = next(r for r in rows if r.title.startswith("百度网盘"))
    assert pan.pan_types == "百度网盘"  # 标题本身含盘名,无需正文

    out2 = wechat_monitor.run_wechat_listen(session, 1, settings=_settings(), client=fake)
    assert out2["new"] == 0  # 第二轮按链接去重


def test_listen_records_miss_and_skips(monkeypatch: pytest.MonkeyPatch, session) -> None:
    # 无对标号 → skipped,并写运维记录(否则后台看不到"公众号情况")
    out = wechat_monitor.run_wechat_listen(session, 1, settings=_settings(), client=FakeClient())
    assert out["reason"] == "no_benchmarks"
    runs = session.scalars(select(RunRecord)).all()
    assert len(runs) == 1 and runs[0].status == "skipped" and "no_benchmarks" in runs[0].detail

    # 当天没有发文 → miss_count 累积
    b = WechatBenchmark(user_id=1, nickname="号A", anchor_url="https://mp.weixin.qq.com/s/A")
    session.add(b)
    session.commit()
    fake = FakeClient(pc={"https://mp.weixin.qq.com/s/A": {"code": 0, "msg": "当天没有发文!", "data": []}})
    out = wechat_monitor.run_wechat_listen(session, 1, settings=_settings(), client=fake)
    assert out["new"] == 0 and b.miss_count == 1

    # 余额低于阈值 → 仅禁用 dajiala(不调 post_condition),不再整轮跳过
    monkeypatch_ = pytest.MonkeyPatch()
    monkeypatch_.setattr(wechat_monitor, "fetch_article_content", lambda url, timeout=15: "")
    fake = FakeClient(remain=0.5)
    out = wechat_monitor.run_wechat_listen(session, 1, settings=_settings(), client=fake)
    monkeypatch_.undo()
    assert out["new"] == 0 and all(c[0] != "pc" for c in fake.calls)
    assert out["dajiala_skipped"] == "low_balance" and out["balance"] == 0.5
    run = session.scalars(select(RunRecord).order_by(RunRecord.id.desc())).first()
    assert run.kind == "wechat_listen" and "dajiala_off" in run.detail


def test_listen_pushes_pan_articles_to_feishu(monkeypatch: pytest.MonkeyPatch, session) -> None:
    import json

    import app.services.feishu as feishu_mod
    import app.services.feishu_client as fc_mod

    b = WechatBenchmark(user_id=1, nickname="号A", anchor_url="https://mp.weixin.qq.com/s/A")
    session.add(b)
    session.commit()
    fake = FakeClient(pc={"https://mp.weixin.qq.com/s/A": {"code": 0, "data": [
        {"title": "夸克网盘资源", "url": "https://mp.weixin.qq.com/s/n1"},
    ]}})
    monkeypatch.setattr(wechat_monitor, "fetch_article_content",
                        lambda url, timeout=15: "正文含 https://pan.quark.cn/s/qwerty")
    monkeypatch.setattr(feishu_mod, "webhook_for", lambda settings, section: "https://open.feishu.cn/hook/x")
    sent: list[dict] = []

    class _FakeFeishu:
        def __init__(self, webhook, secret="") -> None:
            pass

        def send(self, msg: str) -> bool:
            sent.append({"text": msg})
            return True

        def send_card(self, card: dict) -> bool:
            sent.append(card)
            return True

    monkeypatch.setattr(fc_mod, "FeishuClient", _FakeFeishu)
    out = wechat_monitor.run_wechat_listen(session, 1, settings=_settings(), client=fake)
    assert out["new"] == 1 and sent, "应推公众号专属群"
    row = session.scalar(select(WechatArticle))
    assert row.pan_types == "夸克网盘" and row.content
    card = sent[0]
    text = json.dumps(card, ensure_ascii=False)
    assert "夸克网盘" in text  # 网盘列(盘链识别)
    assert "mp.weixin.qq.com/s/n1" in text  # 无转存时标题链接回落原文
    assert "📡" in text  # 卡片标题


def test_push_listen_sends_all_articles_grouped_by_account(session, monkeypatch) -> None:
    """飞书是员工唯一能看到内容的地方:全量新发文都要推,按账号分组,
    不再截断到 20 篇并把剩下的甩给"见平台文章列表"(平台只有运营者能看到)。

    回归:旧实现 `for r in rows[:20]` + 末尾"…另有 N 篇,见平台文章列表"。"""
    import json

    import app.services.feishu as feishu_mod

    rows = []
    for i in range(25):  # 号A 25 篇
        rows.append(WechatArticle(user_id=1, title=f"A文{i}", author="号A",
                                  url=f"https://mp.weixin.qq.com/s/a{i}", source="listen",
                                  read_num=0))
    for i in range(3):  # 号B 3 篇
        rows.append(WechatArticle(user_id=1, title=f"B文{i}", author="号B",
                                  url=f"https://mp.weixin.qq.com/s/b{i}", source="listen",
                                  read_num=0))
    session.add_all(rows)
    session.commit()

    monkeypatch.setattr(feishu_mod, "webhook_for", lambda settings, section: "https://open.feishu.cn/hook/x")
    cards: list[dict] = []

    class _FakeFeishu:
        def __init__(self, webhook, secret="") -> None:
            pass

        def send(self, msg: str) -> bool:
            return True

        def send_card(self, card: dict) -> bool:
            cards.append(card)
            return True

    monkeypatch.setattr(feishu_client, "FeishuClient", _FakeFeishu)
    wechat_monitor._push_listen(session, 1, _settings(), rows)

    blob = json.dumps(cards, ensure_ascii=False)
    # ① 全部 28 篇都出现在卡片里(一篇都不能丢)
    for r in rows:
        assert r.title in blob, f"{r.title} 未推送"
    # ② 不再有"见平台列表"的截断提示
    assert "见平台" not in blob and "另有" not in blob
    # ③ 28 篇 > 每卡 20 篇 → 分成多张卡
    assert len(cards) >= 2
    # ④ 按账号分组:两个账号都有分组标题
    assert "📢 号A" in blob and "📢 号B" in blob


def test_push_listen_renders_my_link_with_clean_href(session, monkeypatch) -> None:
    """监听卡的网盘链接:href 必须是干净的我方转存链,提取码明文附后。

    回归:旧实现把" (提取码 xxxx)"拼进 markdown 链接目标 → URL 含空格/中文,点不开。"""
    import json

    import app.services.feishu as feishu_mod

    r = WechatArticle(user_id=1, title="资源文", author="号A", read_num=0,
                      url="https://mp.weixin.qq.com/s/x", source="listen",
                      pan_urls="https://pan.quark.cn/s/RAW")
    session.add(r)
    session.commit()
    monkeypatch.setattr(feishu_mod, "webhook_for", lambda settings, section: "https://open.feishu.cn/hook/x")
    cards: list[dict] = []

    class _FakeFeishu:
        def __init__(self, webhook, secret="") -> None:
            pass

        def send(self, msg: str) -> bool:
            return True

        def send_card(self, card: dict) -> bool:
            cards.append(card)
            return True

    monkeypatch.setattr(feishu_client, "FeishuClient", _FakeFeishu)
    wechat_monitor._push_listen(session, 1, _settings(), [r], replacements={
        r.id: [("https://pan.quark.cn/s/RAW", "https://pan.quark.cn/s/MINE", "ab12")]})
    blob = json.dumps(cards, ensure_ascii=False)
    assert "pan.quark.cn/s/MINE)" in blob   # href 是我方干净链
    assert "🔑ab12" in blob                  # 提取码明文
    assert "提取码" not in blob              # 不再被拼进 URL
    assert "s/RAW" not in blob               # 他人原始盘链不外泄


def test_push_listen_ignores_quiet_hours(session, monkeypatch) -> None:
    """免打扰时段(默认23~8点)不再丢弃普通新发文:飞书是员工查看入口,任何时段全推。

    回归:旧实现 is_quiet_hours 时只推盘链/阅读≥500 的紧急文,其余整批 return。"""
    import json

    import app.services.feishu as feishu_mod

    monkeypatch.setattr(wechat_monitor, "is_quiet_hours", lambda settings, now=None: True)
    # 一篇普通文:无盘链、阅读 0 → 旧逻辑会在夜间被丢弃
    rows = [WechatArticle(user_id=1, title="夜间普通文", author="号A",
                          url="https://mp.weixin.qq.com/s/night", source="listen", read_num=0)]
    session.add_all(rows)
    session.commit()

    monkeypatch.setattr(feishu_mod, "webhook_for", lambda settings, section: "https://open.feishu.cn/hook/x")
    cards: list[dict] = []

    class _FakeFeishu:
        def __init__(self, webhook, secret="") -> None:
            pass

        def send(self, msg: str) -> bool:
            return True

        def send_card(self, card: dict) -> bool:
            cards.append(card)
            return True

    monkeypatch.setattr(feishu_client, "FeishuClient", _FakeFeishu)
    wechat_monitor._push_listen(session, 1, _settings(), rows)
    assert cards, "免打扰时段也应推送普通新发文"
    assert "夜间普通文" in json.dumps(cards, ensure_ascii=False)


def test_parse_time_converts_utc_iso_to_local_naive() -> None:
    """带 Z/偏移的 UTC ISO 串须转"服务器本地 naive"(与全库 datetime.now() 同域),
    而非直接抹掉 tzinfo(那等于把 UTC 墙钟当本地存,发布时段/近 N 天统计偏移一整时区)。"""
    aware = datetime.fromisoformat("2026-09-22T18:00:00+00:00")
    out = wechat_monitor._parse_time("2026-09-22T18:00:00Z")
    assert out.tzinfo is None
    assert out == aware.astimezone().replace(tzinfo=None)  # 先转本地再脱 tzinfo
    # naive ISO 按本地解释,值原样不变
    assert wechat_monitor._parse_time("2026-09-22T18:00:00") == datetime(2026, 9, 22, 18, 0, 0)


# ---------------------------------------------------------------- 同步
def test_sync_pages_until_isend_and_backfills_ghid(monkeypatch: pytest.MonkeyPatch, session) -> None:
    b = WechatBenchmark(user_id=1, nickname="未命名", anchor_url="https://mp.weixin.qq.com/s/A")
    session.add(b)
    session.commit()

    def _page(items, offset, is_end):
        return {"code": 0, "data": {
            "AccountInfo": {"UserName": "gh_abc123", "NickName": "真名号"},
            "MsgList": {"Msg": [{"AppMsg": {"DetailInfo": items} } ]},
            "PagingInfo": {"Offset": offset, "IsEnd": is_end},
        }}

    fake = FakeClient(hist=[
        _page([{"Title": "历史文1 百度网盘", "ContentUrl": "https://mp.weixin.qq.com/s/h1"},
               {"Title": "历史文2 迅雷云盘", "ContentUrl": "https://mp.weixin.qq.com/s/h2"}], "OFF1", 0),
        _page([{"Title": "历史文3 夸克网盘", "ContentUrl": "https://mp.weixin.qq.com/s/h3"}], "", 1),
    ])
    out = wechat_monitor.sync_wechat_account(session, 1, b.id, settings=_settings(), client=fake)
    assert out["pages"] == 2 and out["new"] == 3
    assert b.ghid == "gh_abc123" and b.nickname == "真名号"
    urls = {r.url for r in session.scalars(select(WechatArticle)).all()}
    assert urls == {f"https://mp.weixin.qq.com/s/h{i}" for i in (1, 2, 3)}
    h2 = next(r for r in session.scalars(select(WechatArticle)).all() if r.title.startswith("历史文2"))
    assert h2.pan_types == "迅雷云盘" and h2.publish_at is None
    # 第二页 IsEnd=1 → 不再翻第三页
    assert sum(1 for c in fake.calls if c[0] == "hist") == 2


def test_sync_respects_max_pages(monkeypatch: pytest.MonkeyPatch, session) -> None:
    b = WechatBenchmark(user_id=1, nickname="号A", anchor_url="https://mp.weixin.qq.com/s/A")
    session.add(b)
    session.commit()

    def _page(offset, is_end):
        item = {"Title": f"文{offset or '0'}",
                "ContentUrl": f"https://mp.weixin.qq.com/s/p{offset or 0}"}
        return {
            "code": 0,
            "data": {
                "MsgList": {
                    "Msg": [
                        {"AppMsg": {"DetailInfo": [item]}},
                    ]
                }
            },
            "PagingInfo": {"Offset": offset or "x", "IsEnd": is_end},
        }

    fake = FakeClient(hist=[_page("o1", 0), _page("o2", 0), _page("o3", 1)])
    out = wechat_monitor.sync_wechat_account(session, 1, b.id, settings=_settings(), client=fake)
    assert out["pages"] == 2 and out["new"] == 2  # wechat_sync_max_pages=2 截断
    assert sum(1 for c in fake.calls if c[0] == "hist") == 2


# ---------------------------------------------------------------- 微信读书(免费源)
from app.services.cookie_store import set_cookie as _set_cookie
from app.services.weread_client import review_to_url


class FakeWeread:
    """假 WereadClient:latest_article/shelf/mp_content 按脚本回放。"""

    def __init__(self, cover: dict | None = None, shelf: list | None = None,
                 content: str = "", cover_items: list | None = None) -> None:
        self.cover = cover
        self.shelf_value = shelf or []
        self.content = content
        self.cover_items = cover_items or []
        self.calls: list[tuple] = []

    def latest_article(self, book_id: str) -> dict | None:
        self.calls.append(("cover", book_id))
        if self.cover is None:
            return None
        return {**self.cover}

    def mp_articles(self, book_id: str, offset: int = 0, count: int = 20) -> dict:
        self.calls.append(("articles", book_id, offset))
        items = self.cover_items
        if not items and self.cover:
            # 从 cover 生成一条(兼容单条测试)
            items = [{"title": self.cover.get("title", ""), "original_id": "test_orig",
                      "read_num": 100, "like_num": 5}]
        reviews = [{"createTime": 1788800000 + i,
                    "subReviews": [{"review": {
                        "reviewId": f"{book_id}_r{i}",
                        "mpInfo": {"title": it.get("title", ""),
                                   "originalId": it.get("original_id", ""),
                                   "readNum": it.get("read_num", 0),
                                   "likeNum": it.get("like_num", 0)},
                        "createTime": 1788800000 + i}}]} for i, it in enumerate(items)]
        return {"reviews": reviews, "synckey": 1}

    def mp_content(self, review_id: str) -> str:
        self.calls.append(("content", review_id))
        return self.content

    def shelf(self) -> list:
        self.calls.append(("shelf",))
        return self.shelf_value


def test_review_to_url_preserves_tilde() -> None:
    """reviewId → 原文短链:token 中的 `~` 必须原样保留(微信 302 坑)。"""
    rid = "MP_WXS_2_abc_4OcS7~rrtk2Lwe4P0YPiGg"
    assert review_to_url(rid, book_id="MP_WXS_2_abc") == "https://mp.weixin.qq.com/s/4OcS7~rrtk2Lwe4P0YPiGg"
    assert review_to_url(rid) == "https://mp.weixin.qq.com/s/4OcS7~rrtk2Lwe4P0YPiGg"
    assert review_to_url("") == ""


def test_listen_low_balance_still_runs_weread(session, monkeypatch: pytest.MonkeyPatch) -> None:
    """空余额只饿死 dajiala:书架号走微信读书免费源照常入库,且绝不调付费接口。"""
    _set_cookie(session, 1, "weread", "vid=1; skey=x")
    session.add(WechatBenchmark(user_id=1, nickname="书架号", weread_book_id="MP_WXS_1", anchor_url=""))
    session.add(WechatBenchmark(user_id=1, nickname="手动号", anchor_url="https://mp.weixin.qq.com/s/A"))
    session.commit()
    fake = FakeWeread(cover={"title": "夸克网盘资源", "url": "https://mp.weixin.qq.com/s/w1",
                             "review_id": "MP_WXS_1_w1", "digest": ""},
                      content="正文含 https://pan.quark.cn/s/zzz")
    monkeypatch.setattr(wechat_monitor, "WereadClient", lambda cookie: fake)
    daj = FakeClient(remain=0.5)
    out = wechat_monitor.run_wechat_listen(session, 1, settings=_settings(), client=daj, weread=fake)
    assert out["new"] >= 1 and out.get("dajiala_skipped") == "low_balance"
    assert all(c[0] != "pc" for c in daj.calls)  # 没钱也不调付费接口


def test_listen_uses_weread_first_and_detects_pan(session, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_cookie(session, 1, "weread", "vid=1; skey=x")
    b = WechatBenchmark(user_id=1, nickname="号A", weread_book_id="MP_WXS_1", anchor_url="")
    session.add(b)
    session.commit()
    fake = FakeWeread(cover={"title": "夸克网盘资源", "url": "https://mp.weixin.qq.com/s/w1",
                             "review_id": "MP_WXS_1_w1", "digest": ""},
                      content="正文含 https://pan.quark.cn/s/zzz")
    monkeypatch.setattr(wechat_monitor, "WereadClient", lambda cookie: fake)
    daj = FakeClient(remain=10.0)
    out = wechat_monitor.run_wechat_listen(session, 1, settings=_settings(), client=daj, weread=fake)
    assert out["status"] == "success" and out["new"] >= 1  # cover 1 篇 + mp_articles 列表(如可用)
    row = session.scalars(select(WechatArticle)).first()
    assert "mp.weixin.qq.com" in row.url and row.pan_types is not None
    assert ("pc", b.anchor_url) not in daj.calls  # 免费源成功时绝不调 dajiala


def test_listen_falls_back_to_dajiala_on_auth_error(session, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.weread_client import WereadAuthError

    _set_cookie(session, 1, "weread", "vid=1; skey=expired")  # 无 wr_rt → 续期不可用 → 降级 dajiala
    b = WechatBenchmark(user_id=1, nickname="号A", weread_book_id="MP_WXS_1",
                        anchor_url="https://mp.weixin.qq.com/s/A")
    session.add(b)
    session.commit()

    class _DeadWeread:
        def latest_article(self, book_id):
            raise WereadAuthError("微信读书登录态失效(-2012)")
        def latest_article(self, book_id):
            raise WereadAuthError("微信读书登录态失效(-2012)")

        def mp_articles(self, book_id, offset=0, count=20):
            raise WereadAuthError("微信读书登录态失效(-2012)")

        def mp_articles(self, book_id, offset=0, count=20):
            raise WereadAuthError("微信读书登录态失效(-2012)")

    daj = FakeClient(pc={"https://mp.weixin.qq.com/s/A": {"code": 0, "data": [
        {"title": "UC网盘资源", "url": "https://mp.weixin.qq.com/s/d1"}]}})
    monkeypatch.setattr(wechat_monitor, "WereadClient", lambda cookie: _DeadWeread())
    out = wechat_monitor.run_wechat_listen(session, 1, settings=_settings(), client=daj)
    assert out["new"] == 1  # 微信读书失效且无法续期 → dajiala 兜底照常入库
    assert ("pc", "https://mp.weixin.qq.com/s/A") in daj.calls


def test_weread_refresh_writeback_and_skips(session, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.cookie_store import get_cookie
    from app.db.models import User

    # 续期走全局 WEREAD_COOKIE 是运营者(admin)专属通道(普通用户不得回退全局续期,
    # 否则会把轮换的新 skey 落到自己行、作废 .env 全局值)→ 本测试用 admin 用户。
    session.add(User(id=1, username="op", password_hash="x", role="admin"))
    session.commit()

    # 无 Cookie → skipped
    out = wechat_monitor.refresh_weread_cookie(session, 1, settings=_settings(weread_cookie=""))
    assert out["status"] == "skipped" and out["reason"] == "no_cookie"
    # 有 Cookie 无 wr_rt → 无法续期
    out = wechat_monitor.refresh_weread_cookie(session, 1, settings=_settings(weread_cookie="wr_vid=1; wr_skey=K"))
    assert out["status"] == "skipped" and out["reason"] == "no_rt"

    # 续期成功 → 回写 Cookie 管理 + 书架验证通过
    class _OK:
        def __init__(self, cookie: str) -> None:
            self.cookie = cookie

        def refresh_skey(self, timeout: int = 20) -> str:
            return self.cookie.replace("wr_skey=OLD", "wr_skey=NEW")

        def shelf(self) -> list:
            return [{"book_id": "MP_WXS_1", "name": "号A"}]

    monkeypatch.setattr(wechat_monitor, "WereadClient", _OK)
    out = wechat_monitor.refresh_weread_cookie(
        session, 1, settings=_settings(weread_cookie="wr_vid=1; wr_rt=R; wr_skey=OLD"))
    assert out["status"] == "success" and out["verified"]
    assert "wr_skey=NEW" in get_cookie(session, 1, "weread")  # 新值已回写平台内存储

    # 续期后仍 -2012 → 登录态整体过期:报 failed 且不回写(避免把有效值覆盖成无效)
    from app.services.weread_client import WereadAuthError

    class _Expired:
        def __init__(self, cookie: str) -> None:
            self.cookie = cookie

        def refresh_skey(self, timeout: int = 20) -> str:
            return "wr_vid=1; wr_rt=R2; wr_skey=BOGUS"

        def shelf(self) -> list:
            raise WereadAuthError("登录态失效(-2012)")

    monkeypatch.setattr(wechat_monitor, "WereadClient", _Expired)
    out2 = wechat_monitor.refresh_weread_cookie(
        session, 1, settings=_settings(weread_cookie="wr_vid=1; wr_rt=R; wr_skey=OLD"))
    assert out2["status"] == "failed" and out2["reason"] == "expired"
    assert "wr_skey=NEW" in get_cookie(session, 1, "weread")  # 仍是上次成功值,未被 BOGUS 覆盖


def test_weread_shelf_and_refresh_block_global_for_non_admin(session) -> None:
    """书架/续期不得回退运营者全局 Cookie:普通用户未自配即视为无 Cookie(横向泄露防护)。
    但监听抓取通道 _weread_cookie 仍允许共享全局凭据(所有租户监听依赖它)。"""
    from app.db.models import User

    session.add(User(id=1, username="normal", password_hash="x", role="user"))
    session.commit()
    g = _settings(weread_cookie="wr_vid=1; wr_rt=R; wr_skey=G")

    # 续期:普通用户拿到全局值会作废 .env → 必须拒绝
    out = wechat_monitor.refresh_weread_cookie(session, 1, settings=g)
    assert out["status"] == "skipped" and out["reason"] == "no_cookie"
    # 书架导入同理
    assert wechat_monitor.import_benchmarks_from_shelf(session, 1, settings=g)["reason"] == "no_cookie"
    # 但监听通道的 Cookie 解析仍回退全局(不泄露书架,仅取数)
    assert wechat_monitor._weread_cookie(session, 1, g) == "wr_vid=1; wr_rt=R; wr_skey=G"


def test_listen_auto_renews_and_retries(session, monkeypatch: pytest.MonkeyPatch) -> None:
    """监听遇 -2012:自动用 wr_rt 续期回写,再以新 Cookie 重试采集。"""
    from app.services.cookie_store import get_cookie
    from app.services.weread_client import WereadAuthError

    _set_cookie(session, 1, "weread", "wr_vid=1; wr_rt=RT; wr_skey=OLD")
    b = WechatBenchmark(user_id=1, nickname="号A", weread_book_id="MP_WXS_1")
    session.add(b)
    session.commit()

    class _Flaky:
        def __init__(self, cookie: str) -> None:
            self.dead = "wr_skey=OLD" in cookie

        def refresh_skey(self, timeout: int = 20) -> str:
            return "wr_vid=1; wr_rt=RT2; wr_skey=NEW"

        def shelf(self) -> list:
            return []

        def latest_article(self, book_id: str) -> dict:
            if self.dead:
                raise WereadAuthError("登录态失效(-2012)")
            return {"title": "夸克网盘资源(cover)", "url": "https://mp.weixin.qq.com/s/c9",
                    "review_id": "MP_WXS_1_c9", "digest": ""}

        def mp_articles(self, book_id: str, offset: int = 0, count: int = 20) -> dict:
            if self.dead:
                raise WereadAuthError("登录态失效(-2012)")
            return {"reviews": [{"createTime": 1788800000, "subReviews": [{"review": {
                "mpInfo": {"title": "夸克网盘资源", "originalId": "n9",
                           "readNum": 100, "likeNum": 5},
                "reviewId": book_id + "_r0"}, "createTime": 1788800000}]}], "synckey": 1}

    monkeypatch.setattr(wechat_monitor, "WereadClient", _Flaky)
    daj = FakeClient()
    out = wechat_monitor.run_wechat_listen(session, 1, settings=_settings(), client=daj, weread=None)
    assert out["new"] >= 1  # 续期后重试成功(cover+列表)
    assert all(c[0] != "pc" for c in daj.calls)  # 全程未动付费接口
    assert "wr_skey=NEW" in get_cookie(session, 1, "weread")  # 新 Cookie 已持久化


def test_weread_refresh_skey_renewal_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """renewal 请求层:Cookie 注入 jar、POST renewal、Set-Cookie 回填(wr_rt 的 @ 重新编码、~ 保留)。"""
    from app.services import weread_client as wc_mod

    class _Cookie:
        def __init__(self, name: str, value: str) -> None:
            self.name, self.value = name, value

    class _Jar:
        def __init__(self) -> None:
            self.items: list[_Cookie] = []

        def set(self, name: str, value: str, domain: str = "", path: str = "") -> None:
            self.items.append(_Cookie(name, value))

        def __iter__(self):
            return iter(self.items)

    class _Resp:
        status_code = 200
        text = '{"succ":1}'

        @staticmethod
        def json():
            return {"succ": 1}

    class _Sess:
        def __init__(self) -> None:
            self.headers: dict = {}
            self.cookies = _Jar()

        def post(self, url: str, data=None, timeout: int = 20) -> _Resp:
            assert url.endswith("/web/login/renewal")
            assert b'"ql":true' in data.replace(b" ", b"") if isinstance(data, bytes) else '"ql":true' in data
            self.cookies.set("wr_skey", "NEWSKEY", domain="weread.qq.com", path="/")
            self.cookies.set("wr_rt", "newrt@x~t", domain="weread.qq.com", path="/")
            return _Resp()

    monkeypatch.setattr(wc_mod.requests, "Session", _Sess)
    out = wc_mod.WereadClient("wr_vid=9; wr_rt=old%40x~t; wr_skey=OLDSKEY").refresh_skey()
    assert out and "wr_skey=NEWSKEY" in out
    assert "wr_rt=newrt%40x~t" in out  # @ 重新 URL 编码;~ 属 unreserved 保留明文
    # 无 wr_rt → 不发请求直接返回 None
    assert wc_mod.WereadClient("wr_vid=9; wr_skey=K").refresh_skey() is None


def test_weread_throttle_shared_across_instances(monkeypatch) -> None:
    """限速状态类级共享:临时新建的多个实例之间仍守最小间隔(否则跨实例突发打爆风控)。"""
    from app.services import weread_client as wc

    cls = wc.WereadClient
    saved = cls._last_request
    cls._last_request = 0.0

    clock = {"now": 1000.0}
    slept: list[float] = []

    class _T:
        def time(self):
            return clock["now"]

        def sleep(self, s):
            slept.append(s)

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"errCode": 0, "data": {}}

    monkeypatch.setattr(wc, "time", _T())
    monkeypatch.setattr(wc.requests, "get", lambda *a, **k: _Resp())
    try:
        cls("c", min_gap=2.0)._get("/x")     # 首跳:_last=0,gap 巨大 → 不 sleep,last→1000
        assert slept == []
        cls("c", min_gap=2.0)._get("/y")     # 换实例、时间未推进:应共享 last=1000 → 补足间隔
        assert len(slept) == 1 and slept[0] == 2.0
    finally:
        cls._last_request = saved


def test_weread_get_decodes_nested_errcode(monkeypatch) -> None:
    """错误码包在 `data.errcode`(statusCode 499 信封)里时也必须识别,否则限流被当成功。

    旧判定只看顶层 errCode → -2014「请求频率过高」漏判,mp_cover 读成"暂无文章",
    整轮监听静默 new=0(2026-09-22 实测)。
    """
    from app.services import weread_client as wc

    def _resp(body):
        class _R:
            status_code = 200

            @staticmethod
            def json():
                return body
        return _R()

    monkeypatch.setattr(
        wc.requests, "get",
        lambda *a, **k: _resp({"statusCode": 499, "data": {"errcode": -2014, "errmsg": "请求频率过高"}}))
    with pytest.raises(wc.WereadError) as ei:
        wc.WereadClient("c", min_gap=0).mp_cover("MP_WXS_1")
    assert "-2014" in str(ei.value) and "请求频率过高" in str(ei.value)

    # 嵌套 -2012 要升级为认证失败,否则 wr_rt 自动续期永远不会被触发
    monkeypatch.setattr(wc.requests, "get", lambda *a, **k: _resp({"data": {"errcode": -2012}}))
    with pytest.raises(wc.WereadAuthError):
        wc.WereadClient("c", min_gap=0).mp_cover("MP_WXS_1")

    # 成功信封带 data.errcode=0 不得误伤
    monkeypatch.setattr(wc.requests, "get", lambda *a, **k: _resp(
        {"reviewId": "MP_WXS_1_abc", "title": "T", "data": {"errcode": 0}}))
    assert wc.WereadClient("c", min_gap=0).mp_cover("MP_WXS_1")["reviewId"] == "MP_WXS_1_abc"


def test_listen_skips_without_any_source(session) -> None:
    WechatBenchmark(user_id=1, nickname="号A", anchor_url="https://mp.weixin.qq.com/s/A")
    session.add(WechatBenchmark(user_id=1, nickname="号A", anchor_url="https://mp.weixin.qq.com/s/A"))
    session.commit()
    out = wechat_monitor.run_wechat_listen(session, 1, settings=_settings(dajiala_key=""))
    assert out["reason"] == "no_source"  # 既无微信读书 Cookie 也无 dajiala key


# ---------------------------------------------------------------- 候选对标号发现
_SOGOU_HTML = '''
<html><body>
<div class="txt-box">
<h3><a href="/link?url=xxx" uigs="article_title_0">花少2<em><!--red_beg-->人格测试<!--red_end--></em>入口</a></h3>
<p class="txt-info" id="s1">最新<em><!--red_beg-->人格测试<!--red_end--></em>在线入口</p>
<div class="s-p"><span class="all-time-y2">测试号甲</span>
<span class="s2"><script>document.write(timeConvert('1699373611'))</script></span></div>
</div>
<div class="txt-box">
<h3><a href="/link?url=yyy" uigs="article_title_1">乡镇晋升录攻略</a></h3>
<div class="s-p"><span class="all-time-y2">测试号乙</span></div>
</div>
</body></html>'''


def test_sogou_parse_extracts_name_title_time(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import sogou_weixin

    class _Resp:
        status_code = 200
        text = _SOGOU_HTML

    monkeypatch.setattr(sogou_weixin.requests, "get",
                        lambda url, params, timeout, headers: _Resp())
    out = sogou_weixin.search_articles("花少2")
    assert out["blocked"] is False
    assert [i["name"] for i in out["items"]] == ["测试号甲", "测试号乙"]
    first = out["items"][0]
    assert first["title"] == "花少2人格测试入口"  # 红高亮标签已清洗
    assert first["digest"] == "最新人格测试在线入口"
    assert first["published_at"] and first["published_at"].year == 2023

    class _Block:
        status_code = 200
        text = "antispider 请输入验证码"

    monkeypatch.setattr(sogou_weixin.requests, "get",
                        lambda url, params, timeout, headers: _Block())
    assert sogou_weixin.search_articles("任意词")["blocked"] is True


def test_mine_title_terms_extracts_content_words() -> None:
    titles = (["花少2人格测试最新入口"] * 3 + ["花少2人格测试2026版"] * 2
              + ["链接:https://pan.quark.cn/s/abc"])
    terms = wechat_monitor.mine_title_terms(titles, top=5)
    assert terms and all("入口" not in t for t in terms)  # 营销泛词剔除
    assert any("人格测试" in t for t in terms)


def test_discover_candidates_dedupe_and_skip_known(session, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.feishu as feishu_mod

    session.add(WechatBenchmark(user_id=1, nickname="测试号甲", anchor_url=""))
    session.add(WechatArticle(user_id=1, title="花少2人格测试入口", source="listen"))
    session.commit()

    def _fake_search(keyword: str, page: int = 1, timeout: int = 15) -> dict:
        assert keyword  # 实际由画像词/兜底词驱动
        return {"items": [
            {"name": "测试号甲", "title": "旧号文章", "digest": "", "published_at": None},
            {"name": "测试号乙", "title": "新候选文章", "digest": "", "published_at": None},
        ], "blocked": False}

    monkeypatch.setattr(wechat_monitor, "sogou_search_articles", _fake_search)
    monkeypatch.setattr(feishu_mod, "webhook_for", lambda settings, section: "")  # 不推飞书
    out = wechat_monitor.discover_candidates(session, 1, settings=_settings())
    assert out["new"] == 1  # 测试号甲已是对标号被跳过
    names = [c.name for c in session.scalars(select(WechatCandidate)).all()]
    assert names == ["测试号乙"]
    out2 = wechat_monitor.discover_candidates(session, 1, settings=_settings())
    assert out2["new"] == 0  # 第二轮:new 态候选去重,不重复入库


def test_import_benchmarks_from_shelf(session, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_cookie(session, 1, "weread", "vid=1; skey=x")
    session.add(WechatBenchmark(user_id=1, nickname="号C", anchor_url="https://mp.weixin.qq.com/s/C"))
    session.commit()
    fake = FakeWeread(shelf=[{"book_id": "MP_WXS_1", "name": "号A"},
                             {"book_id": "MP_WXS_2", "name": "号B"},
                             {"book_id": "MP_WXS_3", "name": "号C"}])
    monkeypatch.setattr(wechat_monitor, "WereadClient", lambda cookie: fake)
    out = wechat_monitor.import_benchmarks_from_shelf(session, 1, settings=_settings())
    assert out["created"] == 2 and out["updated"] == 1  # 号C 按昵称匹配→回填 bookId;A/B 新建
    rows = session.scalars(select(WechatBenchmark)).all()
    assert {r.weread_book_id for r in rows} == {"MP_WXS_1", "MP_WXS_2", "MP_WXS_3"}
    again = wechat_monitor.import_benchmarks_from_shelf(session, 1, settings=_settings())
    assert again["created"] == 0 and again["updated"] == 0  # 重复导入幂等


def test_sync_weread_latest_only(session, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_cookie(session, 1, "weread", "vid=1; skey=x")
    b = WechatBenchmark(user_id=1, nickname="号A", weread_book_id="MP_WXS_1", anchor_url="")
    session.add(b)
    session.commit()
    fake = FakeWeread(cover={"title": "最新一篇", "url": "https://mp.weixin.qq.com/s/latest",
                             "review_id": "MP_WXS_1_latest", "digest": ""})
    monkeypatch.setattr(wechat_monitor, "WereadClient", lambda cookie: fake)
    out = wechat_monitor.sync_wechat_account(session, 1, b.id,
                                             settings=_settings(dajiala_key=""), weread=fake)
    assert out["status"] == "partial" and out["reason"] == "weread_latest_only" and out["new"] == 1


# ---------------------------------------------------------------- 读书平台(wewe-rss v2 兼容,免费全量)
class FakePlatform:
    def __init__(self, pages: list | None = None, resolve: dict | None = None) -> None:
        self.pages = pages or []
        self.resolve_value = resolve or {}
        self.calls: list[tuple] = []

    def mp_articles(self, mp_id: str, page: int = 1, limit: int = 20) -> list:
        self.calls.append(("articles", mp_id, page, limit))
        return self.pages[page - 1] if page <= len(self.pages) else []

    def resolve_mp(self, article_url: str) -> dict:
        self.calls.append(("resolve", article_url))
        return self.resolve_value


def test_listen_prefers_platform_full_list(session, monkeypatch: pytest.MonkeyPatch) -> None:
    """平台(免费全量列表)优先于微信读书与 dajiala。"""
    _set_cookie(session, 1, "weread", "vid=1")
    session.add(WechatBenchmark(user_id=1, nickname="号A", biz="bizABC",
                                weread_book_id="MP_WXS_1"))
    session.commit()
    plat = FakePlatform(pages=[[{"id": "p1", "title": "平台文1(夸克网盘)", "url": "https://mp.weixin.qq.com/s/p1"},
                                {"id": "p2", "title": "平台文2(夸克网盘)", "url": "https://mp.weixin.qq.com/s/p2"}]])
    monkeypatch.setattr(wechat_monitor, "_platform_client", lambda settings: plat)
    daj = FakeClient(remain=10.0)
    out = wechat_monitor.run_wechat_listen(session, 1, settings=_settings(), client=daj, platform=plat)
    assert out["new"] == 2
    assert all(c[0] != "pc" for c in daj.calls)  # 平台成功 → 不走 dajiala 监听
    assert sum(1 for c in daj.calls if c[0] == "zan") == 2  # 但新文即时采样了阅读量
    urls = {r.url for r in session.scalars(select(WechatArticle)).all()}
    assert urls == {"https://mp.weixin.qq.com/s/p1", "https://mp.weixin.qq.com/s/p2"}


def test_sync_platform_paginates(session, monkeypatch: pytest.MonkeyPatch) -> None:
    """平台同步翻页拉全量,后续页全部已入库即停。"""
    b = WechatBenchmark(user_id=1, nickname="号A", biz="bizABC")
    session.add(b)
    session.commit()
    plat = FakePlatform(pages=[
        [{"id": "a", "title": "文A 夸克网盘", "url": "https://mp.weixin.qq.com/s/a"}],
        [{"id": "b", "title": "文B 百度网盘", "url": "https://mp.weixin.qq.com/s/b"}],
        [{"id": "c", "title": "文C UC网盘", "url": "https://mp.weixin.qq.com/s/c"}],
    ])
    monkeypatch.setattr(wechat_monitor, "_platform_client", lambda settings: plat)
    out = wechat_monitor.sync_wechat_account(session, 1, b.id,
                                             settings=_settings(), platform=plat)
    assert out["status"] == "success" and out["new"] == 3
    assert out["pages"] == 4  # 3 页数据 + 1 次空页确认到底


def test_add_benchmark_resolves_biz_via_platform(session, monkeypatch: pytest.MonkeyPatch) -> None:
    plat = FakePlatform(resolve={"mp_id": "bizXYZ", "name": "真名号", "article_title": "T"})
    monkeypatch.setattr(wechat_monitor, "_platform_client", lambda settings: plat)
    row = wechat_monitor.add_benchmark(session, 1, "https://mp.weixin.qq.com/s/new1", settings=_settings())
    assert row["biz"] == "bizXYZ" and row["nickname"] == "真名号"


# ---------------------------------------------------------------- 阅读量采样
def test_sample_traffic_updates_and_records(session, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.db.models import WechatTrafficSample

    b = WechatBenchmark(user_id=1, nickname="号A", anchor_url="https://mp.weixin.qq.com/s/A")
    session.add(b)
    session.commit()
    a1 = WechatArticle(user_id=1, title="文1", url="https://mp.weixin.qq.com/s/n1",
                       source="listen", benchmark_id=b.id)
    a2 = WechatArticle(user_id=1, title="文2", url="https://mp.weixin.qq.com/s/n2",
                       source="listen", benchmark_id=b.id)
    session.add_all([a1, a2])
    session.commit()

    class _TrafficClient(FakeClient):
        def read_zan_pro(self, url):
            self.calls.append(("zan", url))
            return {"read": 1234, "zan": 5, "looking": 6, "share_num": 7,
                    "collect_num": 8, "comment_count": 9}

    fake = _TrafficClient(remain=10.0)
    out = wechat_monitor.sample_traffic(session, 1, settings=_settings(), client=fake)
    assert out["status"] == "success" and out["sampled"] == 2
    assert [("zan", "https://mp.weixin.qq.com/s/n1") in fake.calls,
            ("zan", "https://mp.weixin.qq.com/s/n2") in fake.calls] == [True, True]
    assert a1.read_num == 1234 and a1.share_num == 7 and a1.traffic_at is not None
    assert session.scalar(select(WechatTrafficSample)).read_num == 1234

    # 24h 内不重复采样 → 无目标
    out2 = wechat_monitor.sample_traffic(session, 1, settings=_settings(), client=fake)
    assert out2["reason"] == "no_targets"


def test_sample_traffic_balance_trims(session, monkeypatch: pytest.MonkeyPatch) -> None:
    """余额只剩 0.07 → 只采得起 1 篇(0.06),不会打穿余额。"""
    b = WechatBenchmark(user_id=1, nickname="号A", anchor_url="https://mp.weixin.qq.com/s/A")
    session.add(b)
    session.add_all([
        WechatArticle(user_id=1, title="文1", url="https://mp.weixin.qq.com/s/n1", source="listen", benchmark_id=b.id),
        WechatArticle(user_id=1, title="文2", url="https://mp.weixin.qq.com/s/n2", source="listen", benchmark_id=b.id),
    ])
    session.commit()

    class _TrafficClient(FakeClient):
        def read_zan_pro(self, url):
            return {"read": 1, "zan": 1, "looking": 1, "share_num": 1, "collect_num": 1, "comment_count": 1}

    fake = _TrafficClient(remain=0.07)
    out = wechat_monitor.sample_traffic(session, 1, settings=_settings(), client=fake)
    assert out["sampled"] == 1
    assert out["balance_after"] >= 0


# ---------------------------------------------------------------- 盘链归一化 + 资源共振
def test_pan_links_normalized_and_resonance(session, monkeypatch: pytest.MonkeyPatch) -> None:
    """① 入库时盘链写入归一化表;② 同一盘链被 ≥2 篇推送 → 🔴资源共振卡(冷却去重);③ 旧文自动回填。"""
    b = WechatBenchmark(user_id=1, nickname="号A", anchor_url="https://mp.weixin.qq.com/s/A")
    session.add(b)
    session.commit()
    st = _settings(dajiala_key="", quark_cookie="", pan_transfer_enabled=False,
                   wechat_resonance_hours=48, focus_cooldown_hours=24,
                   feishu_webhook_wechat="https://open.feishu.cn/hook/wechat")

    items = [
        {"title": "夸克: https://pan.quark.cn/s/abc123", "url": "https://mp.weixin.qq.com/s/x1"},
        {"title": "另一号也推同一资源 https://pan.quark.cn/s/abc123", "url": "https://mp.weixin.qq.com/s/x2"},
    ]
    rows = wechat_monitor._insert_new_articles(session, 1, b, items, source="sync")
    session.commit()
    links = session.scalars(select(WechatPanLink)).all()
    assert len(links) == 2 and all(l.pan_url == "https://pan.quark.cn/s/abc123" for l in links)

    sent: list[str] = []

    class _FakeFeishu:
        def __init__(self, webhook, secret="") -> None:
            pass

        def send(self, msg: str) -> bool:
            sent.append(msg)
            return True

        def send_card(self, card: dict) -> bool:
            sent.append(str(card))
            return True

    monkeypatch.setattr(feishu_client, "FeishuClient", _FakeFeishu)
    out = wechat_monitor._enrich_new_articles(session, 1, st, rows, client=None, allow_paid=False)
    assert out == {}  # 无 dajiala 采样,仅共振
    # 转存关闭(未转出我方链)时,共振卡不再推他人原始盘链,回落公众号原文;
    # 但仍识别到"同链 ≥2 篇"并出共振卡。
    assert any("资源共振" in m for m in sent)
    assert session.scalars(select(FeishuAlert).where(FeishuAlert.section == "focus_res")).all()

    # 冷却期内不重推
    out2 = wechat_monitor._enrich_new_articles(session, 1, st, rows, client=None, allow_paid=False)
    assert out2 == {} and len(sent) == 1


def test_resonance_card_pushes_my_pan_link_not_original(session, monkeypatch: pytest.MonkeyPatch) -> None:
    """资源共振卡一律推"我方转存链"(含提取码),绝不把同行/他人的原始盘链推给员工。"""
    b = WechatBenchmark(user_id=1, nickname="号A", anchor_url="https://mp.weixin.qq.com/s/A")
    session.add(b)
    session.commit()
    st = _settings(dajiala_key="", quark_cookie="", pan_transfer_enabled=False,
                   wechat_resonance_hours=48, focus_cooldown_hours=24,
                   feishu_webhook_wechat="https://open.feishu.cn/hook/wechat")
    raw = "https://pan.quark.cn/s/aaa111"
    items = [
        {"title": f"资源甲 {raw}", "url": "https://mp.weixin.qq.com/s/x1"},
        {"title": f"资源甲另号 {raw}", "url": "https://mp.weixin.qq.com/s/x1b"},
    ]
    rows = wechat_monitor._insert_new_articles(session, 1, b, items, source="sync")
    # 模拟该盘链此前已转存为我方链(持久化在 my_pan_urls);本轮转存虽关闭,
    # 共振卡仍应回查历史、用我方链。
    rows[0].my_pan_urls = "https://pan.quark.cn/s/MINE9999 (提取码 ab12)"
    session.commit()

    sent: list[str] = []

    class _FakeFeishu:
        def __init__(self, webhook, secret="") -> None:
            pass

        def send(self, msg: str) -> bool:
            return True

        def send_card(self, card: dict) -> bool:
            sent.append(str(card))
            return True

    monkeypatch.setattr(feishu_client, "FeishuClient", _FakeFeishu)
    wechat_monitor._enrich_new_articles(session, 1, st, rows, client=None, allow_paid=False)
    blob = " ".join(sent)
    assert "资源共振" in blob
    assert "MINE9999" in blob and "ab12" in blob   # 推的是我方链 + 提取码
    assert raw not in blob                          # 原始他人盘链不外泄



def test_resonance_backlog_rotates_not_silently_cooled(session, monkeypatch: pytest.MonkeyPatch) -> None:
    """focus_max_items=1 时,第二共振资源不应在首轮被静默烧冷却:
    首轮推 A(仅烧 A 冷却),次轮 B 顶替推出,第三轮 A/B 各自已冷却 → 归零。

    回归:共振收集阶段曾对每个候选即调 feishu_alert_gate 烧冷却,卡片只渲染前 N 个,
    且 webhook 缺失时更是全量烧冷却却一条都没发——越限/无群资源永无出头之日。
    """
    b = WechatBenchmark(user_id=1, nickname="号A", anchor_url="https://mp.weixin.qq.com/s/A")
    session.add(b)
    session.commit()
    st = _settings(dajiala_key="", quark_cookie="", pan_transfer_enabled=False,
                   wechat_resonance_hours=48, focus_cooldown_hours=24, focus_max_items=1,
                   feishu_webhook_wechat="https://open.feishu.cn/hook/wechat")
    # 共振 = 同一盘链被 ≥2 篇引用;每链各插 2 篇,让 cnt 真正达阈
    # (旧用例每链只 1 篇,靠"本篇 +1"双计 bug 才凑够 cnt=2 触发共振)
    items = [
        {"title": "资源甲 https://pan.quark.cn/s/aaa111", "url": "https://mp.weixin.qq.com/s/x1"},
        {"title": "资源甲·另号 https://pan.quark.cn/s/aaa111", "url": "https://mp.weixin.qq.com/s/x1b"},
        {"title": "资源乙 https://pan.quark.cn/s/bbb222", "url": "https://mp.weixin.qq.com/s/x2"},
        {"title": "资源乙·另号 https://pan.quark.cn/s/bbb222", "url": "https://mp.weixin.qq.com/s/x2b"},
    ]
    rows = wechat_monitor._insert_new_articles(session, 1, b, items, source="sync")
    session.commit()

    sent: list[str] = []

    class _FakeFeishu:
        def __init__(self, webhook, secret="") -> None:
            pass

        def send(self, msg: str) -> bool:
            sent.append(msg)
            return True

        def send_card(self, card: dict) -> bool:
            sent.append(str(card))
            return True

    monkeypatch.setattr(feishu_client, "FeishuClient", _FakeFeishu)
    # 首轮:两个共振资源都新鲜,只推 A(前 1 个),B 不得被烧冷却
    wechat_monitor._enrich_new_articles(session, 1, st, rows, client=None, allow_paid=False)
    # 转存关闭 → 卡里用示例文章标题(资源甲/乙)区分,而非原始盘链
    assert any("资源甲" in m for m in sent) and not any("资源乙" in m for m in sent)
    cooled = {r.title for r in session.scalars(select(FeishuAlert).where(FeishuAlert.section == "focus_res")).all()}
    assert cooled and all("aaa111" in t for t in cooled)  # 仅 A 进了冷却
    # 次轮:A 在冷却里被跳过,B 顶替推出(证明首轮没把 B 静默烧进冷却)
    sent.clear()
    wechat_monitor._enrich_new_articles(session, 1, st, rows, client=None, allow_paid=False)
    assert any("资源乙" in m for m in sent)
    # 第三轮:两者各自已推送并冷却 → 无新共振卡
    sent.clear()
    wechat_monitor._enrich_new_articles(session, 1, st, rows, client=None, allow_paid=False)
    assert not any("资源共振" in m for m in sent)


def test_pan_links_backfill_legacy_articles(session, monkeypatch: pytest.MonkeyPatch) -> None:
    """归一化表建成前的旧文章(有 pan_urls 无链接行)→ 共振检查时自动回填。"""
    b = WechatBenchmark(user_id=1, nickname="号A")
    session.add(b)
    a = WechatArticle(user_id=1, title="旧文", url="https://mp.weixin.qq.com/s/old",
                      pan_urls="https://pan.quark.cn/s/legacy", source="listen",
                      benchmark_id=b.id, created_at=datetime.now() - timedelta(hours=2))
    session.add(a)
    session.commit()
    assert session.scalars(select(WechatPanLink)).all() == []  # 尚未回填

    class _FakeWeread:
        def latest_article(self, book_id):
            return {"title": "夸克网盘资源合集 https://pan.quark.cn/s/abc123", "url": "https://mp.weixin.qq.com/s/new",
                    "review_id": "MP_WXS_1_r1", "digest": "", "name": "号A"}

        def mp_articles(self, book_id, offset=0, count=20):
            return {"reviews": [{"createTime": 1788800000, "subReviews": [{"review": {
                "mpInfo": {"title": "夸克网盘资源合集 https://pan.quark.cn/s/abc123", "originalId": "new_id",
                           "readNum": 5, "likeNum": 1},
                "reviewId": book_id + "_r0"}, "createTime": 1788800000}]}], "synckey": 1}

    from config.settings import Settings as _S
    st_local = _S(_env_file=None, is_dev=True, dajiala_key="", wechat_resonance_hours=48,
                  focus_cooldown_hours=24, feishu_webhook_wechat="https://open.feishu.cn/hook/wechat")
    monkeypatch.setattr(wechat_monitor, "WereadClient", lambda cookie: _FakeWeread())
    fake_inst = _FakeWeread()
    cover_result = fake_inst.latest_article("MP_WXS_1")
    payload = fake_inst.mp_articles("MP_WXS_1")
    wechat_monitor._weread_collect(1, b, _FakeWeread(), session)
    links = session.scalars(select(WechatPanLink)).all()
    assert len(links) >= 1  # 新文入库写入归一化表


# ---------------------------------------------------------------- 文章内容交叉提取
def test_extract_account_refs_from_content() -> None:
    from app.services.content_extract import extract_account_refs

    text = "获取更多资源请关注公众号「资源君」\n搜索公众号:百宝箱分享\n更多网盘资源扫码关注 资源小站"
    refs = extract_account_refs(text)
    assert "资源君" in refs and "百宝箱分享" in refs and "资源小站" in refs
    assert "关注" not in refs and "我们" not in refs


def test_listen_cross_extracts_new_accounts(session, monkeypatch: pytest.MonkeyPatch) -> None:
    """监听到的文章正文提及新公众号 → 自动入库为候选对标号。"""
    import app.services.feishu as feishu_mod
    from app.db.models import WechatCandidate
    from app.services.content_extract import extract_account_refs

    _set_cookie(session, 1, "weread", "vid=1; skey=x")
    b = WechatBenchmark(user_id=1, nickname="号A", weread_book_id="MP_WXS_1", anchor_url="")
    session.add(b)
    session.commit()
    fake = FakeWeread(cover={"title": "某网盘资源合集 夸克网盘", "url": "https://mp.weixin.qq.com/s/w1",
            "review_id": "MP_WXS_1_w1", "digest": "", "name": "号A"},
            cover_items=[
        {"title": "某网盘资源合集 夸克网盘", "original_id": "w1", "read_num": 100, "like_num": 5},
    ])
    print("CROSS DEBUG: fake created")
    # 追踪 _insert_new_articles
    orig_insert = wechat_monitor._insert_new_articles
    def _traced_insert(session, user_id, benchmark, items, source, fetch_content=False, content_resolver=None, require_pan=True):
        print(f"INSERT DEBUG: {len(items)} items, require_pan={require_pan}")
        for it in items:
            print(f"  title={it['title'][:30]} url={it.get('url','')[:40]}")
        result = orig_insert(session, user_id, benchmark, items, source, fetch_content=fetch_content,
                             content_resolver=content_resolver, require_pan=require_pan)
        print(f"INSERT DEBUG: → {len(result)} 篇")
        return result
    orig_insert_fn = wechat_monitor._insert_new_articles
    wechat_monitor._insert_new_articles = _traced_insert
    monkeypatch.setattr(wechat_monitor, "WereadClient", lambda cookie: fake)
    monkeypatch.setattr(wechat_monitor, "fetch_article_content",
                        lambda url, timeout=15: "正文含 https://pan.quark.cn/s/zzz 更多资源请关注公众号「资源君」")
    monkeypatch.setattr(feishu_mod, "webhook_for", lambda settings, section: "")
    out = wechat_monitor.run_wechat_listen(session, 1, settings=_settings(), client=FakeClient(remain=10.0), weread=fake)
    assert out["new"] == 1
    arts = session.scalars(select(WechatArticle)).all()
    for a in arts:
        print(f"CROSS DEBUG: article = {a.title[:30]} | pan_urls = {a.pan_urls!r} | content = {(a.content or '')[:50]}")
    cands = session.scalars(select(WechatCandidate)).all()
    assert any(c.name == "资源君" for c in cands), "应从正文提取新公众号并入库为候选"


def test_listen_batch_rotation(session, monkeypatch: pytest.MonkeyPatch) -> None:
    """错峰批次:batch_index/batch_size 切片生效,号级延迟可控(防风控)。"""
    _set_cookie(session, 1, "weread", "vid=1; skey=x")
    # 造 5 个号,batch_size=2 → 每轮只查 2 个
    for i in range(5):
        session.add(WechatBenchmark(user_id=1, nickname=f"号{i}",
                                    weread_book_id=f"MP_WXS_{i}", anchor_url=""))
    session.commit()
    class _BatchFake(FakeWeread):
        """按 book_id 返回不同文章(验证批次切片真的换了号)。"""
        def latest_article(self, book_id):
            self.calls.append(("cover", book_id))
            return {"title": f"夸克网盘资源 {book_id}", "url": f"https://mp.weixin.qq.com/s/{book_id[-1]}",
                    "review_id": book_id + "_r", "digest": ""}

        def mp_articles(self, book_id, offset=0, count=20):
            self.calls.append(("articles", book_id, offset))
            return {"reviews": [{"createTime": 1788800000, "subReviews": [{"review": {
                "mpInfo": {"title": f"夸克网盘资源 {book_id}", "originalId": f"o{book_id[-1]}",
                           "readNum": 10, "likeNum": 1},
                "reviewId": book_id + "_r"}, "createTime": 1788800000}]}], "synckey": 1}
    monkeypatch.setattr(wechat_monitor, "fetch_article_content", lambda url, timeout=15: "")
    fake = _BatchFake()
    fake.calls = []  # 清掉类级继承的记录,只看本实例

    out = wechat_monitor.run_wechat_listen(session, 1, settings=_settings(dajiala_key=""),
                                           weread=fake, batch_index=0, batch_size=2)
    arts_calls = [c for c in fake.calls if c[0] == "articles"]
    assert {c[1] for c in arts_calls} == {"MP_WXS_0", "MP_WXS_1"}  # 只查了批次的 2 个号
    assert out["accounts"] == 2

    out2 = wechat_monitor.run_wechat_listen(session, 1, settings=_settings(dajiala_key=""),
                                            weread=fake, batch_index=1, batch_size=2)
    # 下一批(MP_WXS_2/3):每号 cover 1 篇 + mp_articles 1 篇 = 2 篇/号 → 4 篇
    assert out2["accounts"] == 2 and out2["new"] == 2


def test_remove_benchmark_cascades(session) -> None:
    """删除对标号级联清理其文章/盘链/采样点(防孤儿行)。"""
    from app.db.models import WechatPanLink, WechatTrafficSample
    b = WechatBenchmark(user_id=1, nickname="号A")
    session.add(b)
    session.commit()
    a = WechatArticle(user_id=1, title="文", url="https://mp.weixin.qq.com/s/x",
                      source="listen", benchmark_id=b.id, pan_urls="https://pan.quark.cn/s/z")
    session.add(a)
    session.commit()
    session.add(WechatPanLink(user_id=1, article_id=a.id, pan_url="https://pan.quark.cn/s/z"))
    session.add(WechatTrafficSample(user_id=1, article_id=a.id))
    session.commit()
    wechat_monitor.remove_benchmark(session, 1, b.id)
    assert session.scalars(select(WechatArticle)).all() == []
    assert session.scalars(select(WechatPanLink)).all() == []
    assert session.scalars(select(WechatTrafficSample)).all() == []


def test_insert_filters_empty_title_and_url_case(session) -> None:
    """空标题不入库;URL 尾部斜杠归一化去重。"""
    b = WechatBenchmark(user_id=1, nickname="号A", weread_book_id="MP_WXS_1")
    session.add(b)
    session.commit()
    items = [
        {"title": "", "url": "https://mp.weixin.qq.com/s/empty"},
        {"title": "文A 夸克网盘", "url": "https://mp.weixin.qq.com/s/dup"},
        {"title": "文B 夸克网盘", "url": "https://mp.weixin.qq.com/s/dup"},  # 同 URL 去重
    ]
    out = wechat_monitor._insert_new_articles(session, 1, b, items, source="listen", require_pan=False)
    assert len(out) == 1  # 空标题过滤 + 同 URL 去重 → 只剩文A


def test_pan_reuse_skips_duplicate_transfer(session, monkeypatch) -> None:
    """同盘链只转存一次:历史文章已有我方链接 → 新文章复用,不再调 transfer_and_share。

    (用户反馈:相同资源被多个对标号转发时每篇各存一份,浪费夸克空间+成倍风控暴露)
    """
    from app.db.models import WechatPanLink
    from app.services.quark_transfer import QuarkError, QuarkTransfer

    # 历史:文章 A 已把盘链 X 转存过
    a = WechatArticle(user_id=1, title="首发文", url="https://mp.weixin.qq.com/s/first",
                      source="listen", benchmark_id=None,
                      pan_urls="https://pan.quark.cn/s/reuseX",
                      my_pan_urls="https://pan.quark.cn/s/OLD (提取码 ab12)")
    session.add(a)
    session.commit()
    session.add(WechatPanLink(user_id=1, article_id=a.id, pan_url="https://pan.quark.cn/s/reuseX"))
    session.commit()
    # 新文 B:同一盘链
    b = WechatArticle(user_id=1, title="转载文", url="https://mp.weixin.qq.com/s/second",
                      source="listen", benchmark_id=None,
                      pan_urls="https://pan.quark.cn/s/reuseX")
    session.add(b)
    session.commit()

    def _no_transfer(self, *a, **kw):
        raise AssertionError("同盘链不应重复转存")

    monkeypatch.setattr(QuarkTransfer, "transfer_and_share", _no_transfer)
    st = _settings(quark_cookie="ck=x", pan_transfer_enabled=True,
                   wechat_listen_sample_new=False)
    reps = wechat_monitor._enrich_new_articles(session, 1, st, [b], client=None)
    assert reps[b.id] == [("https://pan.quark.cn/s/reuseX", "https://pan.quark.cn/s/OLD", "ab12")]
    assert "https://pan.quark.cn/s/OLD" in b.my_pan_urls


def test_pan_first_transfer_records_replacement(session, monkeypatch) -> None:
    """首次见到的盘链照常转存并记 replacements(转存路径未被复用逻辑破坏)。"""
    from app.services.quark_transfer import QuarkTransfer

    b = WechatArticle(user_id=1, title="新资源文", url="https://mp.weixin.qq.com/s/fresh",
                      source="listen", benchmark_id=None,
                      pan_urls="https://pan.quark.cn/s/freshY")
    session.add(b)
    session.commit()

    class _FakeQuark:
        def transfer_and_share(self, url, save_dir="", password=""):
            assert url == "https://pan.quark.cn/s/freshY"
            return {"share_url": "https://pan.quark.cn/s/NEW", "password": "zz99"}

    monkeypatch.setattr(QuarkTransfer, "__init__", lambda self, *a, **kw: None)
    monkeypatch.setattr(QuarkTransfer, "transfer_and_share", _FakeQuark.transfer_and_share)
    st = _settings(quark_cookie="ck=x", pan_transfer_enabled=True,
                   wechat_listen_sample_new=False)
    reps = wechat_monitor._enrich_new_articles(session, 1, st, [b], client=None)
    assert reps[b.id] == [("https://pan.quark.cn/s/freshY", "https://pan.quark.cn/s/NEW", "zz99")]
    assert "https://pan.quark.cn/s/NEW" in b.my_pan_urls


def test_quark_cookie_per_user_wins_over_global(session, monkeypatch) -> None:
    """转存用用户在「Cookie 管理」配的 quark,不回退全局(全局仅为用户未配时兜底)。"""
    from app.services import cookie_store
    from app.services.quark_transfer import QuarkTransfer

    b = WechatArticle(user_id=1, title="新资源文", url="https://mp.weixin.qq.com/s/own",
                      source="listen", benchmark_id=None,
                      pan_urls="https://pan.quark.cn/s/ownZ")
    session.add(b)
    session.commit()
    cookie_store.set_cookie(session, 1, "quark", "ck=mine")

    seen: list[str] = []
    monkeypatch.setattr(QuarkTransfer, "__init__",
                        lambda self, cookie, fid_store="": seen.append(cookie))
    monkeypatch.setattr(QuarkTransfer, "transfer_and_share",
                        lambda self, url, **kw: {"share_url": "https://pan.quark.cn/s/MINE",
                                                 "password": "cd01"})
    st = _settings(quark_cookie="ck=global", pan_transfer_enabled=True,
                   wechat_listen_sample_new=False)
    reps = wechat_monitor._enrich_new_articles(session, 1, st, [b], client=None)
    assert seen == ["ck=mine"]
    assert reps[b.id][0][1] == "https://pan.quark.cn/s/MINE"


def test_quark_transfer_without_any_cookie_alerts_once(session, monkeypatch) -> None:
    """识别到夸克盘链却无任何可用 Cookie:告警指明配置入口,而不是静默推"未转存"。"""
    from app.services import alert_service, cookie_store

    monkeypatch.setattr(cookie_store, "get_cookie", lambda db, uid, plat: "")
    calls: list[tuple] = []
    monkeypatch.setattr(alert_service, "notify_incident",
                        lambda *a, **kw: calls.append(a))
    b = WechatArticle(user_id=1, title="资源文", url="https://mp.weixin.qq.com/s/nocookie",
                      source="listen", benchmark_id=None,
                      pan_urls="https://pan.quark.cn/s/noc")
    session.add(b)
    session.commit()
    st = _settings(quark_cookie="", pan_transfer_enabled=True,
                   wechat_listen_sample_new=False)
    assert wechat_monitor._enrich_new_articles(session, 1, st, [b], client=None) == {}
    assert calls and "缺夸克 Cookie" in calls[0][3]

    # 百度链有自己的门控:缺百度 Cookie 同样要点名(2026-09-26 起,原先静默 break 只留 info 日志)
    calls.clear()
    c = WechatArticle(user_id=1, title="百度资源文", url="https://mp.weixin.qq.com/s/baidu",
                      source="listen", benchmark_id=None,
                      pan_urls="https://pan.baidu.com/s/baiduonly")
    session.add(c)
    session.commit()
    wechat_monitor._enrich_new_articles(session, 1, st, [c], client=None)
    assert [x[3] for x in calls] == ["百度盘链未转存(缺百度网盘 Cookie)"]


def test_pan_selfshare_41017_adopted_as_own_link(session, monkeypatch) -> None:
    """41017(转存自己的分享)=同行搬运了我们的链:原链直接收录为我方链接,不再重试。"""
    from app.services.quark_transfer import QuarkError, QuarkTransfer

    b = WechatArticle(user_id=1, title="搬运我们资源的文", url="https://mp.weixin.qq.com/s/repost",
                      source="listen", benchmark_id=None,
                      pan_urls="https://pan.quark.cn/s/ourshare")
    session.add(b)
    session.commit()

    def _fail(self, url, **kw):
        raise QuarkError("夸克接口失败(41017): 用户禁止转存自己的分享")

    monkeypatch.setattr(QuarkTransfer, "__init__", lambda self, *a, **kw: None)
    monkeypatch.setattr(QuarkTransfer, "transfer_and_share", _fail)
    st = _settings(quark_cookie="ck=x", pan_transfer_enabled=True,
                   wechat_listen_sample_new=False)
    reps = wechat_monitor._enrich_new_articles(session, 1, st, [b], client=None)
    assert reps[b.id] == [("https://pan.quark.cn/s/ourshare", "https://pan.quark.cn/s/ourshare", "")]
    assert "https://pan.quark.cn/s/ourshare" in b.my_pan_urls  # 已落值 → 补转存不再重试


# ---- 百度网盘转存/换链(门控独立 + 提取码解析 + 租户隔离) ----

def _baidu_cookie_only(monkeypatch):
    """只配百度 Cookie、不配夸克:cookie_store 对 baidupan 返回非空,其余为空。"""
    from app.services import cookie_store
    monkeypatch.setattr(cookie_store, "get_cookie",
                        lambda db, uid, plat: "BDUSS=fake" if plat == "baidupan" else "")


def test_baidu_transfer_independent_of_quark_cookie(session, monkeypatch) -> None:
    """quark_cookie 为空时,百度链仍应转存(修掉误抄的 quark_cookie 门控)。"""
    from app.services.baidupan_transfer import BaiduPanClient

    b = WechatArticle(user_id=1, title="百度新资源", url="https://mp.weixin.qq.com/s/bd1",
                      source="listen", benchmark_id=None,
                      pan_urls="https://pan.baidu.com/s/1FRESH")
    session.add(b)
    session.commit()
    _baidu_cookie_only(monkeypatch)
    monkeypatch.setattr(BaiduPanClient, "__init__", lambda self, *a, **kw: None)
    monkeypatch.setattr(BaiduPanClient, "transfer_and_share",
                        lambda self, url, password="", **kw: {
                            "share_url": "https://pan.baidu.com/s/1MINE", "password": "4321"})
    st = _settings(quark_cookie="", pan_transfer_enabled=True, wechat_listen_sample_new=False)
    reps = wechat_monitor._enrich_new_articles(session, 1, st, [b], client=None)
    assert reps[b.id] == [("https://pan.baidu.com/s/1FRESH", "https://pan.baidu.com/s/1MINE", "4321")]
    assert "https://pan.baidu.com/s/1MINE" in b.my_pan_urls
    assert "[百度]" in b.my_pan_urls  # 复用检测依赖该标记


def test_baidu_reuse_parses_extraction_code_from_history(session, monkeypatch) -> None:
    """历史复用百度链:replacements 必须带真实提取码(而非魔法 8888),且不再转存。"""
    from app.db.models import WechatPanLink
    from app.services.baidupan_transfer import BaiduPanClient

    orig = "https://pan.baidu.com/s/1SAME"
    a = WechatArticle(user_id=1, title="首发百度文", url="https://mp.weixin.qq.com/s/bd_first",
                      source="listen", benchmark_id=None, pan_urls=orig,
                      my_pan_urls="https://pan.baidu.com/s/1OLD (提取码 cd56) [百度]")
    session.add(a)
    session.commit()
    session.add(WechatPanLink(user_id=1, article_id=a.id, pan_url=orig))
    session.commit()
    b = WechatArticle(user_id=1, title="转载百度文", url="https://mp.weixin.qq.com/s/bd_second",
                      source="listen", benchmark_id=None, pan_urls=orig)
    session.add(b)
    session.commit()
    _baidu_cookie_only(monkeypatch)
    monkeypatch.setattr(BaiduPanClient, "__init__", lambda self, *a, **kw: None)

    def _no_transfer(self, *a, **kw):
        raise AssertionError("历史已转存的百度链不应重复转存")

    monkeypatch.setattr(BaiduPanClient, "transfer_and_share", _no_transfer)
    st = _settings(quark_cookie="", pan_transfer_enabled=True, wechat_listen_sample_new=False)
    reps = wechat_monitor._enrich_new_articles(session, 1, st, [b], client=None)
    assert reps[b.id] == [(orig, "https://pan.baidu.com/s/1OLD", "cd56")]


def test_baidu_reuse_does_not_leak_other_tenant_link(session, monkeypatch) -> None:
    """别的租户转存过同一盘链:本租户不复用其链接,而是各自转存(租户隔离)。"""
    from app.db.models import WechatPanLink
    from app.services.baidupan_transfer import BaiduPanClient

    orig = "https://pan.baidu.com/s/1HOT"
    other = WechatArticle(user_id=2, title="他租户首发", url="https://mp.weixin.qq.com/s/o1",
                          source="listen", benchmark_id=None, pan_urls=orig,
                          my_pan_urls="https://pan.baidu.com/s/1OTHER (提取码 zz99) [百度]")
    session.add(other)
    session.commit()
    session.add(WechatPanLink(user_id=2, article_id=other.id, pan_url=orig))
    session.commit()
    b = WechatArticle(user_id=1, title="本租户新文", url="https://mp.weixin.qq.com/s/m1",
                      source="listen", benchmark_id=None, pan_urls=orig)
    session.add(b)
    session.commit()
    _baidu_cookie_only(monkeypatch)
    monkeypatch.setattr(BaiduPanClient, "__init__", lambda self, *a, **kw: None)
    monkeypatch.setattr(BaiduPanClient, "transfer_and_share",
                        lambda self, url, password="", **kw: {
                            "share_url": "https://pan.baidu.com/s/1MYOWN", "password": "f0de"})
    st = _settings(quark_cookie="", pan_transfer_enabled=True, wechat_listen_sample_new=False)
    reps = wechat_monitor._enrich_new_articles(session, 1, st, [b], client=None)
    assert reps[b.id] == [(orig, "https://pan.baidu.com/s/1MYOWN", "f0de")]  # 自己的链,非 1OTHER/zz99


def test_baidu_dead_cookie_alerts_after_first_failure(session, monkeypatch) -> None:
    """百度 Cookie 死了时转存只报"errno 失败"(看着像对方链接失效)→ 本轮首次失败后
    探一次登录态定性,确认是 Cookie 就点名告警并停块,否则运营者只看到永久的"—"。"""
    from app.services import alert_service, cookie_store
    from app.services.baidupan_transfer import BaiduPanAuthError, BaiduPanClient, BaiduPanError

    monkeypatch.setattr(cookie_store, "get_cookie",
                        lambda db, uid, plat: "BDUSS=fake" if plat == "baidupan" else "")
    captured: list[tuple] = []
    monkeypatch.setattr(alert_service, "notify_incident",
                        lambda db, uid, kind, title, detail, settings=None, **kw:
                        captured.append((uid, kind, title, detail)) or False)

    arts = [WechatArticle(user_id=1, title=f"百度资源文{i}", url=f"https://mp.weixin.qq.com/s/b{i}",
                          source="listen", benchmark_id=None,
                          pan_urls=f"https://pan.baidu.com/s/1FAIL{i}") for i in range(3)]
    session.add_all(arts)
    session.commit()

    monkeypatch.setattr(BaiduPanClient, "__init__", lambda self, *a, **kw: None)
    probes: list[int] = []

    def _fail_transfer(self, url, password="", **kw):
        raise BaiduPanError("转存失败(errno=-6)")

    def _dead_keepalive(self):
        probes.append(1)
        raise BaiduPanAuthError("百度网盘 Cookie 已失效,请重新复制")

    monkeypatch.setattr(BaiduPanClient, "transfer_and_share", _fail_transfer)
    monkeypatch.setattr(BaiduPanClient, "keepalive", _dead_keepalive)
    st = _settings(quark_cookie="", pan_transfer_enabled=True, wechat_listen_sample_new=False)
    assert wechat_monitor._enrich_new_articles(session, 1, st, list(arts), client=None) == {}
    assert len(probes) == 1                                   # 只定性一次,不逐条撞接口
    assert [c[2] for c in captured] == ["百度网盘 Cookie 已失效,转存停用"]
    assert captured[0][1] == "wechat" and captured[0][0] == 1
    assert "BDUSS" in captured[0][3]                           # 说清去哪换、要含什么


def test_baidu_transient_failure_does_not_claim_cookie_dead(session, monkeypatch) -> None:
    """反向门:登录态仍健康(网络/对方链接问题)时不得报"Cookie 失效",
    否则运营者会白重贴一份好 Cookie。"""
    from app.services import alert_service, cookie_store
    from app.services.baidupan_transfer import BaiduPanClient, BaiduPanError

    monkeypatch.setattr(cookie_store, "get_cookie",
                        lambda db, uid, plat: "BDUSS=fake" if plat == "baidupan" else "")
    captured: list[tuple] = []
    monkeypatch.setattr(alert_service, "notify_incident",
                        lambda db, uid, kind, title, detail, settings=None, **kw:
                        captured.append(title) or False)
    b = WechatArticle(user_id=1, title="百度资源文", url="https://mp.weixin.qq.com/s/bt",
                      source="listen", benchmark_id=None, pan_urls="https://pan.baidu.com/s/1NET")
    session.add(b)
    session.commit()
    monkeypatch.setattr(BaiduPanClient, "__init__", lambda self, *a, **kw: None)
    monkeypatch.setattr(BaiduPanClient, "transfer_and_share",
                        lambda self, url, password="", **kw: (_ for _ in ()).throw(
                            BaiduPanError("转存被限制(errno=105),稍后重试")))
    monkeypatch.setattr(BaiduPanClient, "keepalive", lambda self: True)
    st = _settings(quark_cookie="", pan_transfer_enabled=True, wechat_listen_sample_new=False)
    wechat_monitor._enrich_new_articles(session, 1, st, [b], client=None)
    assert captured == []


def test_pan_cookie_keepalive_tick_covers_per_user_both_pans(session, monkeypatch) -> None:
    """每日巡检必须覆盖"按用户配的"夸克 + 百度 Cookie。

    回归:旧 `quark_keepalive_tick` 一上来 `if not settings.quark_cookie: return 0`——
    只在 Cookie 管理里配了凭据的人从未被巡检,百度盘更是完全没有保活这一问。
    """
    from app.db import models as _m
    from app.services import alert_service, cookie_store
    from app.services.baidupan_transfer import BaiduPanAuthError, BaiduPanClient
    from app.services.quark_transfer import QuarkAuthError, QuarkTransfer

    for uid, enabled in ((1, True), (2, True), (3, False)):
        session.add(_m.User(id=uid, username=f"u{uid}", email=f"u{uid}@b.c",
                            password_hash="x", enabled=enabled))
    # uid1 与 uid2 共用同一份夸克 Cookie(值相同)→ 只探一次、只告警一次
    cookies = {(1, "quark"): "QUARK-SHARED", (2, "quark"): "QUARK-SHARED",
               (1, "baidupan"): "BDUSS-dead", (3, "quark"): "QUARK-DISABLED"}
    monkeypatch.setattr(cookie_store, "get_cookie",
                        lambda db, uid, plat: cookies.get((uid, plat)) or "")
    captured: list[tuple] = []
    monkeypatch.setattr(alert_service, "notify_incident",
                        lambda db, uid, kind, title, detail, settings=None, **kw:
                        captured.append((uid, kind, title)) or False)
    monkeypatch.setattr("app.db.get_session_local", lambda: (lambda: session))
    probed: list[str] = []

    def _dead_quark_init(self, cookie, *a, **kw):
        probed.append(f"quark:{cookie}")

    def _dead_quark(self):
        raise QuarkAuthError("夸克登录态失效(__puus 过期)")

    monkeypatch.setattr(QuarkTransfer, "__init__", _dead_quark_init)
    monkeypatch.setattr(QuarkTransfer, "keepalive", _dead_quark)

    def _dead_baidu_init(self, cookie, *a, **kw):
        probed.append(f"baidupan:{cookie}")

    def _dead_baidu(self):
        raise BaiduPanAuthError("百度网盘 Cookie 已失效,请重新复制")

    monkeypatch.setattr(BaiduPanClient, "__init__", _dead_baidu_init)
    monkeypatch.setattr(BaiduPanClient, "keepalive", _dead_baidu)

    st = _settings(quark_cookie="", pan_transfer_enabled=True)
    assert wechat_monitor.pan_cookie_keepalive_tick(settings=st) == 0
    assert probed == ["quark:QUARK-SHARED", "baidupan:BDUSS-dead"]   # 同凭据不重复撞;禁用用户不探
    assert captured == [(1, "wechat", "🟠 夸克 Cookie 已失效,转存功能停用"),
                        (1, "wechat", "🟠 百度网盘 Cookie 已失效,转存功能停用")]


def test_pan_cookie_keepalive_tick_silent_on_network_wobble(session, monkeypatch) -> None:
    """保活因网络/风控异常失败时不得报"Cookie 失效"(留到下轮),否则天天误报。"""
    from app.db import models as _m
    from app.services import alert_service, cookie_store
    from app.services.quark_transfer import QuarkError, QuarkTransfer

    session.add(_m.User(id=1, username="u1", email="u1@b.c", password_hash="x", enabled=True))
    session.commit()
    monkeypatch.setattr(cookie_store, "get_cookie", lambda db, uid, plat: "ck" if plat == "quark" else "")
    captured: list[tuple] = []
    monkeypatch.setattr(alert_service, "notify_incident",
                        lambda *a, **kw: captured.append(a))
    monkeypatch.setattr("app.db.get_session_local", lambda: (lambda: session))
    monkeypatch.setattr(QuarkTransfer, "__init__", lambda self, *a, **kw: None)
    monkeypatch.setattr(QuarkTransfer, "keepalive",
                        lambda self: (_ for _ in ()).throw(QuarkError("夸克接口超时")))
    assert wechat_monitor.pan_cookie_keepalive_tick(
        settings=_settings(quark_cookie="", pan_transfer_enabled=True)) == 0
    assert captured == []



def test_renewal_cooldown_blocks_repeated_attempts(session) -> None:
    """renewal 失败后 2h 冷却:频控锁定期内不再反复撞(2026-09-16 凌晨连续失败 3h 根因)。"""
    from datetime import datetime

    from app.db.models import SystemConfig
    from app.services import wechat_monitor as wm

    # 预置:冷却期内
    session.add(SystemConfig(
        key="weread_renewal_cooldown_1",
        value=(datetime.now().replace(microsecond=0) + __import__("datetime").timedelta(hours=1)).isoformat()))
    session.commit()
    out = wm.refresh_weread_cookie(session, 1, settings=_settings())
    assert out["status"] == "skipped" and out["reason"] == "renewal_cooldown"
    assert "retry_after" in out

    # 冷却过期 → 恢复尝试(走到 no_cookie 的正常路径)
    row = session.scalar(select(SystemConfig).where(SystemConfig.key == "weread_renewal_cooldown_1"))
    row.value = (datetime.now() - __import__("datetime").timedelta(minutes=1)).isoformat()
    session.commit()
    out2 = wm.refresh_weread_cookie(session, 1, settings=_settings())
    assert out2["status"] == "skipped" and out2["reason"] == "no_cookie"  # 不再被冷却拦


def test_new_weread_cookie_clears_renewal_cooldown(session) -> None:
    """保存新微信读书 Cookie 解除续期冷却;冷却是为旧废凭据设的,不该绑住新会话。

    只清本人那行——别人的 uid 行不得被连带删除(租户隔离)。
    """
    from datetime import datetime, timedelta

    from app.db.models import SystemConfig
    from app.services import wechat_monitor as wm

    for uid in (1, 2):
        session.add(SystemConfig(key=f"weread_renewal_cooldown_{uid}",
                                 value=(datetime.now() + timedelta(hours=1)).isoformat()))
    session.commit()

    _set_cookie(session, 1, "weread", "wr_vid=1; wr_skey=NEW")
    assert session.scalar(select(SystemConfig).where(
        SystemConfig.key == "weread_renewal_cooldown_1")) is None
    assert session.scalar(select(SystemConfig).where(
        SystemConfig.key == "weread_renewal_cooldown_2")) is not None

    # 冷却已解除:续期逻辑真正被执行(不再返回 renewal_cooldown 提前退出)
    # 该 Cookie 无 wr_rt → no_rt;若是 renewal_cooldown 则说明新 Cookie 仍被旧冷却绑住
    out = wm.refresh_weread_cookie(session, 1, settings=_settings())
    assert out["status"] == "skipped" and out["reason"] == "no_rt"

    # 非 weread 平台不碰冷却行
    session.add(SystemConfig(key="weread_renewal_cooldown_1",
                             value=(datetime.now() + timedelta(hours=1)).isoformat()))
    session.commit()
    _set_cookie(session, 1, "quark", "ck=x")
    assert session.scalar(select(SystemConfig).where(
        SystemConfig.key == "weread_renewal_cooldown_1")) is not None


def test_keyword_article_all_users_skips_disabled(monkeypatch, session) -> None:
    """per-user 调度入口只处理启用用户:被管理员停用的账号不再消耗 dajiala 配额、不再推飞书。

    与 collect_tick/due_schedules 的 User.enabled 过滤同源(审计)。
    """
    from app.db.models import User
    import app.db

    session.add(User(id=1, username="a", password_hash="x"))               # 启用
    session.add(User(id=2, username="b", password_hash="x", enabled=False))  # 停用
    session.commit()
    seen: list[int] = []
    monkeypatch.setattr(app.db, "get_session_local", lambda: (lambda: session))
    monkeypatch.setattr(session, "close", lambda: None)
    monkeypatch.setattr(wechat_monitor, "keyword_article_tick",
                        lambda db, uid, settings=None: seen.append(uid))
    wechat_monitor.keyword_article_all_users(_settings())
    assert seen == [1]  # 停用用户 2 不被遍历



def test_dajiala_non_json_error_does_not_leak_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """非 JSON 响应严禁带 key:网关/门户错误页常回显含 ?key= 的请求 URL,
    而该异常会经路由 HTTPException(502, str(exc)) 原样透传到前端。响应体只入服务端日志。"""
    import app.services.dajiala_client as dc

    key = "SECRETKEY123"

    class _Resp:
        status_code = 502
        text = f"<html>502 upstream for /article_detail?key={key}&url=x</html>"

        def json(self):
            raise ValueError("not json")

    monkeypatch.setattr(dc.requests, "request", lambda *a, **k: _Resp())
    client = dc.DajialaClient(key)
    with pytest.raises(dc.DajialaError) as ei:
        client.article_detail("https://mp.weixin.qq.com/s/abc")
    assert key not in str(ei.value)


def test_keyword_article_backlog_rotates_not_silently_cooled(
        session, monkeypatch: pytest.MonkeyPatch) -> None:
    """越限的新文不应被静默烧冷却:focus_max_items=1 时首轮推 1 篇,
    次轮另一篇要能顶替推送(证明它没在首轮 gate 评估时被顺手烧掉冷却)。

    回归:keyword_article_tick 曾对全部 hits 逐条过冷却门(边查边烧)再截断推前 N,
    导致第 N+1 篇被烧冷却却从未进卡片、下轮又被自身冷却排除而永无出头之日。
    """
    import json
    from app.services import sogou_weixin, feishu_client as fc

    st = _settings(keyword_search_terms="夸克网盘资源", focus_max_items=1,
                   focus_cooldown_hours=24, feishu_webhook="https://open.feishu.cn/hook/main")

    sent: list[dict] = []

    class _FakeFeishu:
        def __init__(self, webhook, secret="") -> None:
            pass

        def send_card(self, card: dict) -> bool:
            sent.append(card)
            return True

    monkeypatch.setattr(fc, "FeishuClient", _FakeFeishu)
    monkeypatch.setattr(fc, "webhook_for", lambda settings, section: "https://open.feishu.cn/hook/wechat")
    monkeypatch.setattr(wechat_monitor, "title_hits", lambda title: True)
    titles = ["甲资源合集12345", "乙资源合集67890"]
    monkeypatch.setattr(
        sogou_weixin, "search_articles",
        lambda keyword, page=1, timeout=15: {
            "items": [{"title": t, "name": "某公众号", "digest": "", "published_at": None} for t in titles],
            "blocked": False})

    # 首轮:两篇都新鲜,但只推 1 篇
    assert wechat_monitor.keyword_article_tick(session, 1, settings=st) == 1
    assert titles[0] in json.dumps(sent[-1], ensure_ascii=False)
    # 次轮:首篇已冷却被跳过,另一篇顶替推出(证明它没在首轮被静默烧冷却)
    assert wechat_monitor.keyword_article_tick(session, 1, settings=st) == 1
    assert titles[1] in json.dumps(sent[-1], ensure_ascii=False)
    # 第三轮:两篇各自已推送并冷却 → 无新推送
    assert wechat_monitor.keyword_article_tick(session, 1, settings=st) == 0


def test_weread_refresh_tick_names_failure_reason(session, monkeypatch) -> None:
    """续期失败的飞书提醒必须点名原因:旧文案一律写"wr_rt 已整体失效",
    而"换出新 skey 但书架验证仍 -2012"(需重新扫码)与"renewal 被频控"处理动作不同。"""
    from app.db import models as _m
    from app.services import alert_service

    session.add(_m.User(id=1, username="u1", email="u1@b.c", password_hash="x", enabled=True))
    session.commit()

    captured: list[tuple] = []
    monkeypatch.setattr(alert_service, "notify_incident",
                        lambda db, uid, kind, title, detail, settings=None, **kw:
                        captured.append((uid, kind, title, detail)))
    monkeypatch.setattr("app.db.get_session_local", lambda: (lambda: session))
    monkeypatch.setattr("app.services.cookie_store.get_cookie", lambda db, uid, p: "wr_vid=1; wr_skey=x")
    monkeypatch.setattr(wechat_monitor, "refresh_weread_cookie",
                        lambda db, uid, settings=None: {"status": "failed", "reason": "expired"})

    assert wechat_monitor.weread_refresh_tick(settings=Settings(_env_file=None)) == 0
    uid, kind, title, detail = captured[0]
    assert (uid, kind) == (1, "wechat")
    assert "书架验证仍报登录失效" in detail          # 说清是"登录态整体过期",不是频控
    assert "wr_rt 已整体失效" not in detail
    assert "wr_rt=" in detail                        # 顺带提醒粘贴前先确认含 wr_rt


def test_weread_refresh_tick_alerts_on_missing_wr_rt(session, monkeypatch) -> None:
    """Cookie 缺 wr_rt(续期根本没起跑)必须提醒:旧逻辑当 skipped 静默放过,
    运营者以为自动续期在守着,实际这份 Cookie 十几小时必死、监听随后断源。"""
    from app.db import models as _m
    from app.services import alert_service

    session.add(_m.User(id=1, username="u1", email="u1@b.c", password_hash="x", enabled=True))
    session.commit()

    captured: list[tuple] = []
    monkeypatch.setattr(alert_service, "notify_incident",
                        lambda db, uid, kind, title, detail, settings=None, **kw:
                        captured.append((uid, title, detail)))
    monkeypatch.setattr("app.db.get_session_local", lambda: (lambda: session))
    monkeypatch.setattr("app.services.cookie_store.get_cookie", lambda db, uid, p: "wr_vid=1; wr_skey=x")
    monkeypatch.setattr(wechat_monitor, "refresh_weread_cookie",
                        lambda db, uid, settings=None: {"status": "skipped", "reason": "no_rt"})

    assert wechat_monitor.weread_refresh_tick(settings=Settings(_env_file=None)) == 0
    assert len(captured) == 1
    assert "根本没有 wr_rt" in captured[0][2]


def test_weread_refresh_tick_stays_quiet_for_cooldown(session, monkeypatch) -> None:
    """冷却期内的 skipped 不重复提醒(前一轮已推过,否则每 6h 撞一次刷屏)。"""
    from app.db import models as _m
    from app.services import alert_service

    session.add(_m.User(id=1, username="u1", email="u1@b.c", password_hash="x", enabled=True))
    session.commit()

    captured: list[tuple] = []
    monkeypatch.setattr(alert_service, "notify_incident",
                        lambda *a, **kw: captured.append(a))
    monkeypatch.setattr("app.db.get_session_local", lambda: (lambda: session))
    monkeypatch.setattr(wechat_monitor, "refresh_weread_cookie",
                        lambda db, uid, settings=None: {"status": "skipped", "reason": "renewal_cooldown"})

    assert wechat_monitor.weread_refresh_tick(settings=Settings(_env_file=None)) == 0
    assert captured == []


def test_sync_endpoint_translates_weread_auth_error(session, monkeypatch) -> None:
    """「同步文章」在 Cookie 失效时必须是 502+可执行文案,不是裸 500。

    实测(2026-09-22):无 dajiala key 的同步走微信读书最新一篇,wr_skey 一过期就抛
    WereadAuthError,而 sync 路由只接 KeyError/DajialaError → 500 → 前端统一显示
    "服务器开小差了,请稍后重试",用户完全不知道要去换 Cookie。
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import wechat as wechat_api
    from app.auth import get_current_user
    from app.db import get_db
    from app.db.models import User
    from app.services.weread_client import WereadAuthError

    user = User(username="t", password_hash="x")
    session.expire_on_commit = False   # 请求跑在线程池里,惰性属性会回查内存 sqlite(跨线程报错)
    session.add(user)
    session.commit()
    b = WechatBenchmark(user_id=user.id, nickname="某号", weread_book_id="MP_WXS_1", active=True)
    session.add(b)
    session.commit()

    def _boom(*a, **kw):
        raise WereadAuthError("微信读书登录态失效(-2012):Cookie 过期?")

    monkeypatch.setattr(wechat_api.wechat_monitor, "sync_wechat_account", _boom)
    app = FastAPI()
    app.include_router(wechat_api.router)
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_user] = lambda: user
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.post(f"/api/wechat/benchmarks/{b.id}/sync")
    assert r.status_code == 502, r.text
    assert "Cookie 管理" in r.json()["detail"] and "wr_rt" in r.json()["detail"]


class _SyncWeread:
    """假微信读书客户端:旧 skey 报登录失效,续期后的新 skey 能拿到最新一篇。"""

    def __init__(self, cookie: str) -> None:
        self.cookie = cookie

    def latest_article(self, book_id: str) -> dict | None:
        if self.cookie.endswith("old"):
            raise wechat_monitor.WereadAuthError("微信读书登录态失效(-2012):Cookie 过期?")
        return {"url": "https://mp.weixin.qq.com/s/new1", "title": "新文章 夸克网盘",
                "review_id": "rid", "publish_at": None}

    def mp_content(self, review_id: str) -> str:
        return "正文 https://pan.quark.cn/s/abc123"


def _sync_setup(session, monkeypatch) -> WechatBenchmark:
    from app.services.cookie_store import set_cookie as _set

    _set(session, 1, "weread", "wr_vid=1; wr_skey=old")
    b = WechatBenchmark(user_id=1, nickname="某号", weread_book_id="MP_WXS_1", active=True)
    session.add(b)
    session.commit()
    monkeypatch.setattr(wechat_monitor, "WereadClient", _SyncWeread)
    monkeypatch.setattr(wechat_monitor, "_dajiala_key", lambda s, u, st: "")
    return b


def test_sync_renews_weread_cookie_before_giving_up(session, monkeypatch) -> None:
    """无 dajiala 的同步遇到 skey 过期,应先自动续期一次再重试(监听早已这么做)。"""
    b = _sync_setup(session, monkeypatch)
    monkeypatch.setattr(wechat_monitor, "refresh_weread_cookie",
                        lambda s, u, settings=None: {"status": "success", "cookie": "wr_vid=1; wr_skey=new"})

    out = wechat_monitor.sync_wechat_account(session, 1, b.id, settings=Settings(_env_file=None))
    assert out["status"] == "partial" and out["new"] == 1
    assert session.scalar(select(WechatArticle).where(WechatArticle.user_id == 1)) is not None


def test_sync_reraises_when_renewal_also_fails(session, monkeypatch) -> None:
    """续期也救不回来(wr_rt 死了)时原样上抛,交给路由出 502 文案——不能假装同步成功。"""
    import pytest

    b = _sync_setup(session, monkeypatch)
    monkeypatch.setattr(wechat_monitor, "refresh_weread_cookie",
                        lambda s, u, settings=None: {"status": "failed", "reason": "renewal_failed"})
    with pytest.raises(wechat_monitor.WereadAuthError):
        wechat_monitor.sync_wechat_account(session, 1, b.id, settings=Settings(_env_file=None))


def test_quark_dead_source_leaves_backfill_queue(session, monkeypatch) -> None:
    """源分享已被封(41031)的历史文章必须出队:补转存按 id 升序取队头,
    永久失败的死链若不落值会年年霸占配额,后面能转的永远轮不到
    (2026-09-22 本机库实测 24 篇 09-15 的文章卡在一周没动)。"""
    from app.services.quark_transfer import QuarkError, QuarkTransfer

    DEAD = "https://pan.quark.cn/s/deadshare"
    FRESH = "https://pan.quark.cn/s/freshshare"
    old = WechatArticle(user_id=1, title="死链文", url="https://mp.weixin.qq.com/s/old",
                        source="listen", pan_urls=DEAD)
    new = WechatArticle(user_id=1, title="新文", url="https://mp.weixin.qq.com/s/new",
                        source="listen", pan_urls=FRESH)
    session.add_all([old, new])
    session.commit()

    tried: list[str] = []

    def _transfer(self, url, **kw):
        tried.append(url)
        if url == DEAD:
            raise QuarkError("夸克接口失败(41031): 分享已被取消")
        return {"share_url": "https://pan.quark.cn/s/MINE", "password": ""}

    monkeypatch.setattr(QuarkTransfer, "__init__", lambda self, *a, **kw: None)
    monkeypatch.setattr(QuarkTransfer, "transfer_and_share", _transfer)
    st = _settings(quark_cookie="ck=x", pan_transfer_enabled=True, wechat_listen_sample_new=False)

    wechat_monitor._enrich_new_articles(session, 1, st, [new], client=None)
    session.commit()
    assert "41031" in (old.my_pan_urls or "")        # 落标记 → 不再算"待转存"
    assert "pan.quark.cn/s/MINE" in new.my_pan_urls

    tried.clear()
    wechat_monitor._enrich_new_articles(session, 1, st, [new], client=None)
    assert DEAD not in tried                          # 第二轮起不再撞死链


def test_push_listen_marks_transfer_status(session, monkeypatch) -> None:
    """卡片要当场说清点标题会去哪:🔴=我方转存链,⏳=有源链还没转好(点进去是原文),
    ⛔=对方分享已封永远转不了。"""
    import json

    import app.services.feishu as feishu_mod

    mine = WechatArticle(user_id=1, title="已转存文", author="号A", read_num=0,
                         url="https://mp.weixin.qq.com/s/m", source="listen",
                         pan_urls="https://pan.quark.cn/s/M", pan_types="夸克",
                         my_pan_urls="https://pan.quark.cn/s/MINE")
    pending = WechatArticle(user_id=1, title="待转存文", author="号A", read_num=0,
                            url="https://mp.weixin.qq.com/s/p", source="listen",
                            pan_urls="https://pan.quark.cn/s/P", pan_types="夸克")
    dead = WechatArticle(user_id=1, title="死链文", author="号A", read_num=0,
                         url="https://mp.weixin.qq.com/s/d", source="listen",
                         pan_urls="https://pan.quark.cn/s/D", pan_types="夸克",
                         my_pan_urls="⚠️源分享已被封(41031),未转存")
    session.add_all([mine, pending, dead])
    session.commit()

    monkeypatch.setattr(feishu_mod, "webhook_for", lambda settings, section: "https://open.feishu.cn/hook/x")
    cards: list[dict] = []

    class _FakeFeishu:
        def __init__(self, webhook, secret="") -> None:
            pass

        def send(self, msg: str) -> bool:
            return True

        def send_card(self, card: dict) -> bool:
            cards.append(card)
            return True

    monkeypatch.setattr(feishu_client, "FeishuClient", _FakeFeishu)
    # mine 走"本轮转存链";另外两篇无 replacements → 回落历史我方链/原文
    wechat_monitor._push_listen(session, 1, _settings(), [mine, pending, dead],
                                replacements={mine.id: [("https://pan.quark.cn/s/M",
                                                         "https://pan.quark.cn/s/MINE", "")]})
    blob = json.dumps(cards, ensure_ascii=False)
    assert "⏳待转存" in blob and "⛔源失效" in blob
    assert "mp.weixin.qq.com/s/p" in blob            # 待转存的仍指原文(有标记说明,不骗人)


def test_admin_health_reports_transfer_coverage(session) -> None:
    """运维页要看得见转存覆盖率:多少篇换成我方链、多少还在队列里、几个用户配了夸克 Cookie。"""
    from datetime import datetime

    from app.admin import collection_health
    from app.db.models import UserCookie

    session.add(WechatArticle(user_id=1, title="有链已转", url="u1", source="listen",
                              pan_urls="https://pan.quark.cn/s/a",
                              my_pan_urls="https://pan.quark.cn/s/mine", created_at=datetime.now()))
    session.add(WechatArticle(user_id=1, title="有链未转", url="u2", source="listen",
                              pan_urls="https://pan.quark.cn/s/b",
                              my_pan_urls="", created_at=datetime.now()))
    session.add(WechatArticle(user_id=1, title="无链", url="u3", source="listen",
                              pan_urls="", created_at=datetime.now()))
    session.add(UserCookie(user_id=1, platform="quark", cookie="x"))
    session.commit()

    h = collection_health(session)
    m = h["wechat_monitor"]
    assert m["pan_30d"] == 2 and m["transferred_30d"] == 1
    assert m["pending_30d"] == 1 and m["quark_cookie_users"] == 1


# ---------------------------------------------------------------- 同步 → 转存 → 推送
def _fake_feishu(monkeypatch, cards: list) -> None:
    import app.services.feishu as feishu_mod

    monkeypatch.setattr(feishu_mod, "webhook_for",
                        lambda settings, section: "https://open.feishu.cn/hook/x")

    class _FakeFeishu:
        def __init__(self, webhook, secret="") -> None:
            pass

        def send(self, msg: str) -> bool:
            return True

        def send_card(self, card: dict) -> bool:
            cards.append(card)
            return True

    monkeypatch.setattr(feishu_client, "FeishuClient", _FakeFeishu)


def _fake_quark(monkeypatch, out_map: dict[str, str], calls: list) -> None:
    from app.services.quark_transfer import QuarkTransfer

    monkeypatch.setattr(QuarkTransfer, "__init__", lambda self, *a, **kw: None)

    def _transfer(self, url, save_dir="", password=""):
        calls.append(url)
        return {"share_url": out_map[url], "password": "ab12"}

    monkeypatch.setattr(QuarkTransfer, "transfer_and_share", _transfer)


def test_sync_transfers_then_pushes_my_link(session, monkeypatch) -> None:
    """同步入库 → 先夸克转存 → 再推飞书;相同盘链只保存一次、只推一篇。

    回归:旧实现同步只入库,卡片永远等监听的下一轮才带上我的链,
    且同资源被搬运两次就会推两张卡、转存两次。"""
    import json

    from app.services.feishu import _md_safe

    b = WechatBenchmark(user_id=1, nickname="号A", biz="bizABC")
    session.add(b)
    session.commit()
    plat = FakePlatform(pages=[[
        {"id": "r1", "title": "资源文一 https://pan.quark.cn/s/RAW1",
         "url": "https://mp.weixin.qq.com/s/r1"},
        {"id": "r2", "title": "资源文二 https://pan.quark.cn/s/RAW1",
         "url": "https://mp.weixin.qq.com/s/r2"},
    ]])
    monkeypatch.setattr(wechat_monitor, "_platform_client", lambda settings: plat)
    calls: list[str] = []
    _fake_quark(monkeypatch, {"https://pan.quark.cn/s/RAW1": "https://pan.quark.cn/s/MINE1"}, calls)
    cards: list[dict] = []
    _fake_feishu(monkeypatch, cards)

    st = _settings(quark_cookie="ck=x", pan_transfer_enabled=True, wechat_sync_push_limit=20)
    out = wechat_monitor.sync_wechat_account(session, 1, b.id, settings=st, platform=plat)
    assert out["new"] == 2 and out["deduped"] == 1 and out["pushed"] == 1
    assert calls == ["https://pan.quark.cn/s/RAW1"]      # 相同链接不再重复转存
    blob = json.dumps(cards, ensure_ascii=False)
    assert "pan.quark.cn/s/MINE1" in blob and "🔑ab12" in blob   # 点文章名进我的盘
    assert f"]({_md_safe('https://pan.quark.cn/s/RAW1')}" not in blob  # 不把他人原链做成链接


def test_sync_push_skips_resource_already_announced(session, monkeypatch) -> None:
    """更早入库的文章已经带着这条盘链进过飞书群 → 同步到的搬运文不再重推。"""
    b = WechatBenchmark(user_id=1, nickname="号A", biz="bizABC")
    session.add(b)
    session.commit()
    old = WechatArticle(user_id=1, title="首发文", url="https://mp.weixin.qq.com/s/old",
                        source="listen", pan_urls="https://pan.quark.cn/s/RAW1")
    session.add(old)
    session.commit()
    session.add(WechatPanLink(user_id=1, article_id=old.id, pan_url="https://pan.quark.cn/s/RAW1"))
    session.commit()

    plat = FakePlatform(pages=[[{"id": "r1", "title": "搬运文 https://pan.quark.cn/s/RAW1",
                                 "url": "https://mp.weixin.qq.com/s/r1"}]])
    monkeypatch.setattr(wechat_monitor, "_platform_client", lambda settings: plat)
    calls: list[str] = []
    _fake_quark(monkeypatch, {}, calls)
    cards: list[dict] = []
    _fake_feishu(monkeypatch, cards)

    st = _settings(quark_cookie="ck=x", pan_transfer_enabled=True, wechat_sync_push_limit=20)
    out = wechat_monitor.sync_wechat_account(session, 1, b.id, settings=st, platform=plat)
    assert out["new"] == 1 and out["deduped"] == 1 and out["pushed"] == 0
    assert calls == [] and cards == []   # 已推过的资源连转存都不必再做


def test_sync_push_window_caps_transfer_calls(session, monkeypatch) -> None:
    """一次同步入库很多资源文时按 `wechat_sync_push_limit` 截断:转存调用同步受限,
    同步请求不会被几十次夸克调用拖成十几分钟;截断数量写进返回值。"""
    b = WechatBenchmark(user_id=1, nickname="号A", biz="bizABC")
    session.add(b)
    session.commit()
    items = [{"id": f"i{n}", "title": f"资源文{n} https://pan.quark.cn/s/raw{n}",
              "url": f"https://mp.weixin.qq.com/s/i{n}"} for n in range(5)]
    plat = FakePlatform(pages=[items])
    monkeypatch.setattr(wechat_monitor, "_platform_client", lambda settings: plat)
    calls: list[str] = []
    _fake_quark(monkeypatch, {f"https://pan.quark.cn/s/raw{n}": f"https://pan.quark.cn/s/m{n}"
                              for n in range(5)}, calls)
    _fake_feishu(monkeypatch, [])

    st = _settings(quark_cookie="ck=x", pan_transfer_enabled=True, wechat_sync_push_limit=2)
    out = wechat_monitor.sync_wechat_account(session, 1, b.id, settings=st, platform=plat)
    assert out["pushed"] == 2 and out["truncated"] == 3 and out["deduped"] == 0
    assert len(calls) == 2


def test_sync_still_pushes_when_transfer_blows_up(session, monkeypatch) -> None:
    """转存环节抛异常不能把同步整个搞失败:照旧推卡,标题回落公众号原文。"""
    import json

    from app.services.quark_transfer import QuarkTransfer

    b = WechatBenchmark(user_id=1, nickname="号A", biz="bizABC")
    session.add(b)
    session.commit()
    plat = FakePlatform(pages=[[{"id": "r1", "title": "资源文 https://pan.quark.cn/s/RAW9",
                                 "url": "https://mp.weixin.qq.com/s/r1"}]])
    monkeypatch.setattr(wechat_monitor, "_platform_client", lambda settings: plat)
    monkeypatch.setattr(QuarkTransfer, "__init__", lambda self, *a, **kw: None)

    def _boom(self, url, **kw):
        raise RuntimeError("夸克接口 500")

    monkeypatch.setattr(QuarkTransfer, "transfer_and_share", _boom)
    cards: list[dict] = []
    _fake_feishu(monkeypatch, cards)

    st = _settings(quark_cookie="ck=x", pan_transfer_enabled=True, wechat_sync_push_limit=20)
    out = wechat_monitor.sync_wechat_account(session, 1, b.id, settings=st, platform=plat)
    assert out["pushed"] == 1
    assert "mp.weixin.qq.com/s/r1" in json.dumps(cards, ensure_ascii=False)
