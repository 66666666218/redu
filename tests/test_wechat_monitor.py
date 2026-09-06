"""公众号监听/同步单测(dajiala 全 mock,零花费):盘链识别/加号/监听去重/翻页同步/余额保护。"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db import models  # noqa: F401
from app.db.models import RunRecord, WechatArticle, WechatBenchmark
from config.settings import Settings
from app.services import wechat_monitor
from app.services.dajiala_client import DajialaClient


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
                        lambda url, timeout, headers: _Resp(200, html_ok))
    out = wechat_monitor.fetch_article_content("https://mp.weixin.qq.com/s/x")
    assert "pan.quark.cn/s/aa" in out and "<p>" not in out
    assert wechat_monitor.detect_pan_types(out) == ["夸克网盘"]

    monkeypatch.setattr(wechat_monitor.requests, "get",
                        lambda url, timeout, headers: _Resp(200, "环境异常 请完成验证"))
    assert wechat_monitor.fetch_article_content("https://mp.weixin.qq.com/s/x") == ""


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
        {"data": [{"title": "普通文", "content_url": "https://mp.weixin.qq.com/s/n2"}]},
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
    # 无对标号 → skipped,不写 RunRecord
    out = wechat_monitor.run_wechat_listen(session, 1, settings=_settings(), client=FakeClient())
    assert out["reason"] == "no_benchmarks"
    assert session.scalars(select(RunRecord)).all() == []

    # 当天没有发文 → miss_count 累积
    b = WechatBenchmark(user_id=1, nickname="号A", anchor_url="https://mp.weixin.qq.com/s/A")
    session.add(b)
    session.commit()
    fake = FakeClient(pc={"https://mp.weixin.qq.com/s/A": {"code": 0, "msg": "当天没有发文!", "data": []}})
    out = wechat_monitor.run_wechat_listen(session, 1, settings=_settings(), client=fake)
    assert out["new"] == 0 and b.miss_count == 1

    # 余额低于阈值 → skipped + RunRecord
    monkeypatch_ = pytest.MonkeyPatch()
    monkeypatch_.setattr(wechat_monitor, "fetch_article_content", lambda url, timeout=15: "")
    out = wechat_monitor.run_wechat_listen(session, 1, settings=_settings(), client=FakeClient(remain=0.5))
    monkeypatch_.undo()
    assert out["reason"] == "low_balance"
    run = session.scalars(select(RunRecord).order_by(RunRecord.id.desc())).first()
    assert run.kind == "wechat_listen" and run.status == "skipped" and "low_balance" in run.detail


def test_listen_pushes_pan_articles_to_feishu(monkeypatch: pytest.MonkeyPatch, session) -> None:
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
    sent: list[str] = []

    class _FakeFeishu:
        def __init__(self, webhook, secret="") -> None:
            pass

        def send(self, msg: str) -> bool:
            sent.append(msg)
            return True

    monkeypatch.setattr(fc_mod, "FeishuClient", _FakeFeishu)
    out = wechat_monitor.run_wechat_listen(session, 1, settings=_settings(), client=fake)
    assert out["new"] == 1 and sent, "应推公众号专属群"
    row = session.scalar(select(WechatArticle))
    assert row.pan_types == "夸克网盘" and "夸克网盘" in sent[0] and row.content


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
        _page([{"Title": "历史文1", "ContentUrl": "https://mp.weixin.qq.com/s/h1"},
               {"Title": "历史文2 迅雷云盘", "ContentUrl": "https://mp.weixin.qq.com/s/h2"}], "OFF1", 0),
        _page([{"Title": "历史文3", "ContentUrl": "https://mp.weixin.qq.com/s/h3"}], "", 1),
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

    def __init__(self, cover: dict | None = None, shelf: list | None = None, content: str = "") -> None:
        self.cover = cover
        self.shelf_value = shelf or []
        self.content = content
        self.calls: list[tuple] = []

    def latest_article(self, book_id: str) -> dict | None:
        self.calls.append(("cover", book_id))
        if self.cover is None:
            return None
        return {**self.cover}

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
    assert out["status"] == "success" and out["new"] == 1
    row = session.scalar(select(WechatArticle))
    assert row.url == "https://mp.weixin.qq.com/s/w1" and row.pan_types == "夸克网盘"
    assert ("pc", b.anchor_url) not in daj.calls  # 免费源成功时绝不调 dajiala


def test_listen_falls_back_to_dajiala_on_auth_error(session, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.weread_client import WereadAuthError

    _set_cookie(session, 1, "weread", "vid=1; skey=expired")
    b = WechatBenchmark(user_id=1, nickname="号A", weread_book_id="MP_WXS_1",
                        anchor_url="https://mp.weixin.qq.com/s/A")
    session.add(b)
    session.commit()

    class _DeadWeread:
        def latest_article(self, book_id):
            raise WereadAuthError("微信读书登录态失效(-2012)")

    daj = FakeClient(pc={"https://mp.weixin.qq.com/s/A": {"code": 0, "data": [
        {"title": "UC网盘资源", "url": "https://mp.weixin.qq.com/s/d1"}]}})
    monkeypatch.setattr(wechat_monitor, "WereadClient", lambda cookie: _DeadWeread())
    out = wechat_monitor.run_wechat_listen(session, 1, settings=_settings(), client=daj)
    assert out["new"] == 1  # 微信读书失效 → dajiala 兜底照常入库
    assert ("pc", "https://mp.weixin.qq.com/s/A") in daj.calls


def test_listen_skips_without_any_source(session) -> None:
    WechatBenchmark(user_id=1, nickname="号A", anchor_url="https://mp.weixin.qq.com/s/A")
    session.add(WechatBenchmark(user_id=1, nickname="号A", anchor_url="https://mp.weixin.qq.com/s/A"))
    session.commit()
    out = wechat_monitor.run_wechat_listen(session, 1, settings=_settings(dajiala_key=""))
    assert out["reason"] == "no_source"  # 既无微信读书 Cookie 也无 dajiala key


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
    plat = FakePlatform(pages=[[{"id": "p1", "title": "平台文1", "url": "https://mp.weixin.qq.com/s/p1"},
                                {"id": "p2", "title": "平台文2", "url": "https://mp.weixin.qq.com/s/p2"}]])
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
        [{"id": "a", "title": "A", "url": "https://mp.weixin.qq.com/s/a"}],
        [{"id": "b", "title": "B", "url": "https://mp.weixin.qq.com/s/b"}],
        [{"id": "c", "title": "C", "url": "https://mp.weixin.qq.com/s/c"}],
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
