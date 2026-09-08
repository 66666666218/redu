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
    assert out["status"] == "success" and out["new"] == 1
    row = session.scalar(select(WechatArticle))
    assert "mp.weixin.qq.com" in row.url and row.pan_types == "夸克网盘"
    assert ("pc", b.anchor_url) not in daj.calls  # 免费源成功时绝不调 dajiala


def test_listen_falls_back_to_dajiala_on_auth_error(session, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.weread_client import WereadAuthError

    _set_cookie(session, 1, "weread", "vid=1; skey=expired")  # 无 wr_rt → 续期不可用 → 降级 dajiala
    b = WechatBenchmark(user_id=1, nickname="号A", weread_book_id="MP_WXS_1",
                        anchor_url="https://mp.weixin.qq.com/s/A")
    session.add(b)
    session.commit()

    class _DeadWeread:
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
    assert out["new"] == 1  # 续期后重试成功
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
    assert any("资源共振" in m and "abc123" in m for m in sent)
    assert session.scalars(select(FeishuAlert).where(FeishuAlert.section == "focus_res")).all()

    # 冷却期内不重推
    out2 = wechat_monitor._enrich_new_articles(session, 1, st, rows, client=None, allow_paid=False)
    assert out2 == {} and len(sent) == 1


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
        def mp_articles(self, book_id, offset=0, count=20):
            return {"reviews": [{"createTime": 1788800000, "subReviews": [{"review": {
                "mpInfo": {"title": "夸克网盘资源合集 https://pan.quark.cn/s/abc123", "originalId": "new_id",
                           "readNum": 5, "likeNum": 1},
                "reviewId": book_id + "_r0"}, "createTime": 1788800000}]}], "synckey": 1}

    from config.settings import Settings as _S
    st_local = _S(_env_file=None, is_dev=True, dajiala_key="", wechat_resonance_hours=48,
                  focus_cooldown_hours=24, feishu_webhook_wechat="https://open.feishu.cn/hook/wechat")
    monkeypatch.setattr(wechat_monitor, "WereadClient", lambda cookie: _FakeWeread())
    wechat_monitor._weread_collect(1, b, _FakeWeread(), session)
    links = session.scalars(select(WechatPanLink)).all()
    assert len(links) == 1  # 新文入库写入归一化表


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
    fake = FakeWeread(cover_items=[
        {"title": "某网盘资源合集 夸克网盘", "original_id": "w1", "read_num": 100, "like_num": 5},
    ])
    monkeypatch.setattr(wechat_monitor, "WereadClient", lambda cookie: fake)
    monkeypatch.setattr(wechat_monitor, "fetch_article_content",
                        lambda url, timeout=15: "正文含 https://pan.quark.cn/s/zzz 更多资源请关注公众号「资源君」")
    monkeypatch.setattr(feishu_mod, "webhook_for", lambda settings, section: "")
    out = wechat_monitor.run_wechat_listen(session, 1, settings=_settings(), client=FakeClient(remain=10.0), weread=fake)
    assert out["new"] == 1
    cands = session.scalars(select(WechatCandidate)).all()
    assert any(c.name == "资源君" for c in cands), "应从正文提取新公众号并入库为候选"
