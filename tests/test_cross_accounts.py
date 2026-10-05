"""跨平台同类资源号发现单测(2026-10-01):知乎解析、盘链筛选、去重。"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db import models  # noqa: F401
from app.db.models import CrossPlatformAccount, User
from app.services import cross_accounts as cp


@pytest.fixture(autouse=True)
def _no_rate_limit(monkeypatch):
    """限速(_REQ_GAP)是给线上防封用的,测试里不该真等——每个请求省 4 秒。"""
    monkeypatch.setattr(cp, "_REQ_GAP", 0)


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    # autoflush=False **与生产一致**(app/db/database.py 就是这么配的)。默认 True 会让
    # `select` 前先 flush pending,从而**掩盖"同一轮里重复 add 撞唯一键"这类 bug**——
    # 2026-10-02 实跑就在生产配置上撞到了(cross_platform_accounts 唯一键冲突)。
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    db.add(User(id=1, username="admin", email="a@b.c", password_hash="x", enabled=True))
    db.commit()
    yield db
    db.close()


def test_search_zhihu_skips_non_account_items_and_reads_pan_link(monkeypatch) -> None:
    """知乎返回里混着 hot_timing/ring_box 这类非账号条目,要跳过;
    盘链从标题+正文里判(用户口径:确认是推广网盘的才收)。"""
    class _R:
        status_code = 200                       # 现在非 200 会被判成硬失败(见 SearchSourceError)

        def json(self):
            return {"data": [
                {"type": "hot_timing", "object": {}},                       # 非账号条目
                {"type": "search_result", "object": {
                    "author": {"name": "亚楼", "url_token": "ya-lou-1"},
                    "title": "资源整理", "content": "夸克 https://pan.quark.cn/s/abc 自取"}},
                {"type": "search_result", "object": {
                    "author": {"name": "路人", "url_token": "lu-ren"},
                    "title": "聊聊资源", "content": "大家平时用什么网盘?"}},   # 无盘链
            ]}

    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: _R())
    hits = cp._search_zhihu("ck", "网盘资源")
    assert len(hits) == 2                       # 非账号条目被跳过
    assert hits[0]["uid"] == "ya-lou-1" and hits[0]["name"] == "亚楼"
    assert hits[0]["pan_link"].startswith("https://pan.quark.cn")
    assert hits[1]["pan_link"] == ""            # 光聊资源的没有链


def test_discover_keeps_only_accounts_with_pan_link(session, monkeypatch) -> None:
    """核心口径:只收录**内容里真含网盘链**的号,光聊资源的不算。"""
    monkeypatch.setattr(cp, "SEARCHERS", {"zhihu": lambda ck, kw, limit=20: [
        {"uid": "u1", "name": "资源号", "url": "https://www.zhihu.com/people/u1",
         "snippet": "x", "pan_link": "https://pan.quark.cn/s/real"},
        {"uid": "u2", "name": "路人", "url": "", "snippet": "y", "pan_link": ""},
    ]})
    monkeypatch.setattr("app.services.cookie_store.get_cookies",
                        lambda s, u: {"zhihu": "ck"})
    out = cp.discover_cross_accounts(session, 1, keywords=["测试词"])
    assert out["found"] == 1 and out["new"] == 1     # found 统计的是"带链"的命中(2 条里 1 条)
    rows = session.scalars(select(CrossPlatformAccount)).all()
    assert [r.name for r in rows] == ["资源号"]
    assert rows[0].hit_keyword == "测试词"


def test_discover_dedupes_by_platform_uid(session, monkeypatch) -> None:
    """同一账号重复发现不重复入库(唯一键 user+platform+uid)。"""
    monkeypatch.setattr(cp, "SEARCHERS", {"zhihu": lambda ck, kw, limit=20: [
        {"uid": "u1", "name": "资源号", "url": "", "snippet": "", "pan_link": "https://pan.quark.cn/s/a"},
    ]})
    monkeypatch.setattr("app.services.cookie_store.get_cookies", lambda s, u: {"zhihu": "ck"})
    assert cp.discover_cross_accounts(session, 1, keywords=["词A"])["new"] == 1
    assert cp.discover_cross_accounts(session, 1, keywords=["词B"])["new"] == 0
    assert len(session.scalars(select(CrossPlatformAccount)).all()) == 1


def test_search_bilibili_reads_user_search_and_flags_pan_account(monkeypatch) -> None:
    """B站走**搜用户**:拿 mid/uname/usign;号名或签名含网盘词 → looks_like_pan 置位。

    走搜用户而非搜视频的原因(2026-10-02 实测):视频搜索的 `description` 是空的
    (接口不返回视频简介),捞不到链;而搜用户能把**名字里就写着"网盘资源"**的号直接捞出。
    """
    class _Nav:
        def json(self):
            return {"code": -101, "data": {"wbi_img": {      # 未登录也照给 wbi_img
                "img_url": "https://i0.hdslb.com/bfs/wbi/" + "a" * 32 + ".png",
                "sub_url": "https://i0.hdslb.com/bfs/wbi/" + "b" * 32 + ".png"}}}

    class _Search:
        def json(self):
            return {"code": 0, "data": {"result": [
                {"mid": 123, "uname": "网盘资源商行", "usign": "持续更新，加我QQ:12345"},
                {"mid": 456, "uname": "老王的日常", "usign": "记录生活"},        # 与网盘无关
                {"mid": 789, "uname": "小站", "usign": "夸克 https://pan.quark.cn/s/xyz"},
            ]}}

    import requests
    monkeypatch.setattr(requests, "get", lambda url, **k: _Nav() if "nav" in url else _Search())
    monkeypatch.setattr(cp, "_bili_mixin_cache", {"key": "", "ts": 0.0})   # 清缓存以强制走 nav

    hits = cp._search_bilibili("", "网盘资源")
    assert len(hits) == 3
    assert hits[0]["uid"] == "123" and hits[0]["url"] == "https://space.bilibili.com/123"
    assert hits[0]["looks_like_pan"] is True                # 号名明写网盘
    assert hits[1]["looks_like_pan"] is False               # 生活号,不受影响
    assert hits[2]["pan_link"].startswith("https://pan.quark.cn")   # 签名里有真链


def test_pan_account_hints_ignore_generic_word() -> None:
    """"资源"是泛词**不算**(会收进一大堆无关号);须命中"网盘"或具体网盘品牌。"""
    assert not any(h in "资源分享家" for h in cp._PAN_ACCOUNT_HINTS)
    assert any(h in "夸克网盘资源" for h in cp._PAN_ACCOUNT_HINTS)


def test_discover_keeps_bili_account_flagged_by_name(session, monkeypatch) -> None:
    """B站口径:号名/签名明写网盘就收(该平台搜索层给不出链),泛词不算。"""
    monkeypatch.setattr(cp, "SEARCHERS", {"bilibili": lambda ck, kw, limit=20: [
        {"uid": "m1", "name": "网盘资源商行", "url": "", "snippet": "",
         "pan_link": "", "looks_like_pan": True},
        {"uid": "m2", "name": "资源分享家", "url": "", "snippet": "",
         "pan_link": "", "looks_like_pan": False},      # 只含"资源"不算
    ]})
    monkeypatch.setattr("app.services.cookie_store.get_cookies", lambda s, u: {})
    out = cp.discover_cross_accounts(session, 1, keywords=["测试词"])
    assert out["new"] == 1
    rows = session.scalars(select(CrossPlatformAccount)).all()
    assert [r.name for r in rows] == ["网盘资源商行"]


def test_bili_mixin_is_cached(monkeypatch) -> None:
    """wbi mixin 要缓存:一轮里多次搜索不该反复打 nav(访问频率是红线)。"""
    calls = []

    class _Nav:
        def json(self):
            return {"data": {"wbi_img": {
                "img_url": "https://x/" + "a" * 32 + ".png",
                "sub_url": "https://x/" + "b" * 32 + ".png"}}}

    import requests
    monkeypatch.setattr(requests, "get", lambda url, **k: (calls.append(url), _Nav())[1])
    monkeypatch.setattr(cp, "_bili_mixin_cache", {"key": "", "ts": 0.0})
    first = cp._bili_mixin()
    second = cp._bili_mixin()
    assert first and first == second
    assert len(calls) == 1                                  # 第二次命中缓存,没有再请求


def test_discover_without_cookie_still_runs_anon_platforms(session, monkeypatch) -> None:
    """没配 Cookie 时:免 Cookie 的平台(B站)照跑,需要登录态的平台(知乎)跳过。"""
    called = []

    def _zhihu(ck, kw, limit=20):
        called.append("zhihu")          # 不该被调用
        return []

    monkeypatch.setattr(cp, "SEARCHERS", {
        "bilibili": lambda ck, kw, limit=20: [
            {"uid": "m1", "name": "UP主", "url": "", "snippet": "",
             "pan_link": "https://pan.quark.cn/s/b"}],
        "zhihu": _zhihu,
    })
    monkeypatch.setattr("app.services.cookie_store.get_cookies", lambda s, u: {})
    out = cp.discover_cross_accounts(session, 1, keywords=["测试词"])
    assert out["platforms"] == ["bilibili"] and out["new"] == 1
    assert called == []                 # 没 Cookie 就不去撞知乎的风控


def test_discover_dedupes_across_keywords_in_one_round(session, monkeypatch) -> None:
    """同一轮内**多个搜索词命中同一个号**不能撞唯一键。

    这是 2026-10-02 真实跑出来的 bug:三个行业词各搜一次,"夸克网盘资源"这类号会在
    "网盘资源"和"夸克网盘"两次搜索里**都出现**;`_save` 查重时前一次 add 还在 pending
    (生产 session 是 `autoflush=False`),于是重复 add → commit 时 IntegrityError →
    异常冒泡使**整轮白跑**。所以 fixture 特意配成 autoflush=False 才抓得住。
    """
    monkeypatch.setattr(cp, "SEARCHERS", {"bilibili": lambda ck, kw, limit=20: [
        {"uid": "same", "name": "网盘资源商行", "url": "", "snippet": "",
         "pan_link": "", "looks_like_pan": True},
    ]})
    monkeypatch.setattr(cp, "_account_keywords", lambda *a, **k: ["词A", "词B", "词C"])
    monkeypatch.setattr("app.services.cookie_store.get_cookies", lambda s, u: {})
    out = cp.discover_cross_accounts(session, 1, keywords=["占位"])
    assert out["found"] == 3                # 三次搜索都命中
    assert out["new"] == 1                  # 但入库只一次
    assert len(session.scalars(select(CrossPlatformAccount)).all()) == 1


def test_discover_noop_when_no_usable_platform(session, monkeypatch) -> None:
    """连免 Cookie 的平台都没有时才 no_cookie。"""
    monkeypatch.setattr(cp, "SEARCHERS", {})
    monkeypatch.setattr("app.services.cookie_store.get_cookies", lambda s, u: {})
    out = cp.discover_cross_accounts(session, 1, keywords=["测试词"])
    assert out["status"] == "no_cookie" and out["new"] == 0


# ---------------------------------------------------------------- 资源库搜索词清洗

def test_library_search_word_strips_parenthetical_before_budget() -> None:
    """括号里的补充说明**先剥掉**,否则它会吃掉预算、把资源名切半。

    实测(2026-10-03):`霸王茶姬杯贴自定义入口链接直达（附教程）0919` 硬切 12 字只到
    `…入口链`(资源名断在半截),剥括号后整名才进得来。
    """
    assert cp.library_search_word("霸王茶姬杯贴自定义入口链接直达（附教程）0919") == \
        "霸王茶姬杯贴自定义入口链接直达"


def test_library_search_word_cuts_at_separator_not_mid_phrase() -> None:
    """分隔符之后多是补充说明 → 从第一个(位置 ≥6 的)分隔符切断,别留一条竖线在词里。"""
    assert cp.library_search_word("花少2人格测试直达入口｜最新测试（附链接）") == "花少2人格测试直达入口"
    assert cp.library_search_word("𝐢𝐩𝐚𝐝平板高清动态壁纸｜200张+8k横屏动漫壁纸") == "𝐢𝐩𝐚𝐝平板高清动态壁纸"


def test_library_search_word_keeps_late_separator_phrase_intact() -> None:
    """⚠️ 阈值 6 的由来:分隔符**太靠前**时不能切 —— 会把「七宗罪、七美德…」砍成「七宗罪」(3 字)。

    这正是它**不能**复用 `douyin_leads._to_search_word` 的原因(那条按分隔符取首段),
    实测库里 8 条资源照搬会丢 3 条。
    """
    assert cp.library_search_word("七宗罪、七美德测试免费入口直达（附最新链接）") == \
        "七宗罪、七美德测试免费入口直达"


def test_library_search_word_strips_leading_noise_and_version() -> None:
    """开头口水词(紧跟标点时)与版本尾串都要剥;`亲测！` 留着搜不出东西。"""
    assert cp.library_search_word("亲测！苹果ios共享id，测试可用 10月1最新免费入口") == "苹果ios共享id"
    assert cp.library_search_word("手机警报器（警笛模拟器）2.0版") == "手机警报器"


def test_library_search_word_does_not_cut_mid_ascii_word() -> None:
    """硬切时别切在英文词中间(`pdf` 被切成 `pd`)。"""
    assert cp.library_search_word("github高性价比人生指南pdf") == "github高性价比人生指南"


def test_library_search_word_drops_generic_and_short() -> None:
    """泛化大包名(与群那条路**共用**同一份词表)与过短的都丢掉 —— 搜出来全是噪音。"""
    assert cp.library_search_word("最全文件") == ""
    assert cp.library_search_word("短的") == ""
    assert cp.library_search_word("") == ""


def test_search_zhihu_raises_on_rate_limit_instead_of_empty(monkeypatch) -> None:
    """⚠️ 被限流必须**抛**,不能返回空列表冒充"没有结果"。

    否则下游 `pan_discovery.sync()` 会把"被挡住"记成 `success(候选0)`,链路看着健康、
    其实早停了(与闲鱼那次"假成功"同一类)。实测踩过:同一批词单跑能捞到链,整轮 sync 却是 0。
    """
    class _R:
        status_code = 403
        text = ""

        def json(self):
            return {}

    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: _R())
    with pytest.raises(cp.SearchSourceError) as ei:
        cp._search_zhihu("ck", "网盘资源")
    assert "403" in str(ei.value) and "限流" in str(ei.value)


def test_search_zhihu_raises_when_payload_has_no_data(monkeypatch) -> None:
    """知乎限流时回的是 `{"error": …}` —— 那不是"没有结果",同样是硬失败。"""
    class _R:
        status_code = 200

        def json(self):
            return {"error": {"code": 403, "message": "请求过于频繁"}}

    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: _R())
    with pytest.raises(cp.SearchSourceError):
        cp._search_zhihu("ck", "网盘资源")


def test_search_zhihu_empty_data_is_not_an_error(monkeypatch) -> None:
    """真的搜到 0 条(`{"data": []}`)是**正常**结果,不能抛 —— 否则每轮都误报失败。"""
    class _R:
        status_code = 200

        def json(self):
            return {"data": []}

    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: _R())
    assert cp._search_zhihu("ck", "网盘资源") == []


# ---------------------------------------------------------------- B站搜视频(名字型用)

def test_search_bilibili_videos_strips_em_and_keeps_keyword(monkeypatch) -> None:
    """B站**搜视频**给名字型用:标题要剥掉 `<em>` 高亮,且**必须带 `keyword`**。

    少了 `keyword`,`resource_presence.probe` 就归不了组 → **永远是 0 条**(静默归零,
    2026-10-03 单测抓到过)。
    """
    class _R:
        def json(self):
            return {"code": 0, "data": {"result": [
                {"bvid": "BV1", "title": '<em class="keyword">网盘资源</em>火影忍者720集',
                 "author": "4K超清臻享版"}]}}

    import requests
    monkeypatch.setattr(cp, "_bili_signed_get", lambda *a, **k: _R().json())
    out = cp.search_bilibili_videos("网盘资源")
    assert len(out) == 1
    assert "<em" not in out[0]["snippet"] and "火影忍者" in out[0]["snippet"]
    assert out[0]["keyword"] == "网盘资源"
    assert out[0]["url"] == "https://www.bilibili.com/video/BV1"


def test_search_bilibili_videos_raises_on_risk_control(monkeypatch) -> None:
    """风控 `code=-412` 必须**抛**,不能返回空列表 —— 否则"被拦"会被当成"这资源没人推"。"""
    monkeypatch.setattr(cp, "_bili_signed_get",
                        lambda *a, **k: {"code": -412, "message": "请求被拦截"})
    with pytest.raises(cp.SearchSourceError) as ei:
        cp.search_bilibili_videos("网盘资源")
    assert "-412" in str(ei.value)


# ---------------------------------------------------------------- 静默失败修复(2026-10-03)

def test_search_bilibili_raises_on_risk_control(monkeypatch) -> None:
    """B站 `code=-412`(风控)必须**抛**,不能返回空列表。

    否则"被拦"与"真没新号"在运行记录里长得一模一样 —— 与已修的 `_search_zhihu` 对齐。
    """
    monkeypatch.setattr(cp, "_bili_signed_get",
                        lambda *a, **k: {"code": -412, "message": "请求被拦截"})
    with pytest.raises(cp.SearchSourceError) as ei:
        cp._search_bilibili("", "网盘资源")
    assert "-412" in str(ei.value)


def test_discover_raises_when_every_search_fails(session, monkeypatch) -> None:
    """⚠️ **全部搜索都失败 → 抛**,不能返回 `ok(新增0)`(2026-10-03 修)。

    被限流/登录失效时,`ok(新增0)` 与"真的没有新号"完全无法区分 —— 账号发现会静默停摆。
    """
    def _boom(ck, kw, limit=20):
        raise cp.SearchSourceError("B站返回 code=-412 请求被拦截")

    monkeypatch.setattr(cp, "SEARCHERS", {"bilibili": _boom})
    monkeypatch.setattr(cp, "_account_keywords", lambda *a, **k: ["词A", "词B"])
    monkeypatch.setattr("app.services.cookie_store.get_cookies", lambda s, u: {})
    with pytest.raises(cp.SearchSourceError) as ei:
        cp.discover_cross_accounts(session, 1, keywords=["占位"])
    assert "全部失败" in str(ei.value)


def test_discover_keeps_going_when_some_searches_fail(session, monkeypatch) -> None:
    """**部分**失败要保住其余产出(限速是常态,不能一失败就整轮白跑),并把失败数报出来。"""
    calls = {"n": 0}

    def _flaky(ck, kw, limit=20):
        calls["n"] += 1
        if calls["n"] == 1:
            raise cp.SearchSourceError("超时")
        return [{"uid": f"u{calls['n']}", "name": "网盘资源商行", "url": "",
                 "snippet": "", "pan_link": "", "looks_like_pan": True}]

    monkeypatch.setattr(cp, "SEARCHERS", {"bilibili": _flaky})
    monkeypatch.setattr(cp, "_account_keywords", lambda *a, **k: ["词A", "词B", "词C"])
    monkeypatch.setattr("app.services.cookie_store.get_cookies", lambda s, u: {})
    out = cp.discover_cross_accounts(session, 1, keywords=["占位"])
    assert out["status"] == "ok" and out["failed"] == 1 and out["new"] == 2


def test_cross_account_tick_records_run(session, monkeypatch) -> None:
    """⚠️ 这个作业**此前根本不写运行记录** —— 于是"它到底跑没跑"在系统里查不到。

    与 `xunlei_sync` 同型的可见性缺口(2026-10-03 一起补)。
    """
    from sqlalchemy import select
    from app.db.models import RunRecord
    import app.db as db_mod

    monkeypatch.setattr(db_mod, "get_session_local", lambda: (lambda: session))
    monkeypatch.setattr(cp, "discover_cross_accounts",
                        lambda *a, **k: {"status": "ok", "found": 3, "new": 2, "failed": 0})
    cp.cross_account_tick()
    runs = session.scalars(select(RunRecord).where(RunRecord.kind == "cross_account_discover")).all()
    assert len(runs) == 1 and runs[0].status == "success" and "新增2" in runs[0].detail


def test_cross_account_tick_records_failure(session, monkeypatch) -> None:
    """全失败(现在会抛 `SearchSourceError`)必须在运行记录里体现为 `failed`。"""
    from sqlalchemy import select
    from app.db.models import RunRecord
    import app.db as db_mod

    monkeypatch.setattr(db_mod, "get_session_local", lambda: (lambda: session))

    def _boom(*a, **k):
        raise cp.SearchSourceError("2 次搜索全部失败:B站 code=-412")

    monkeypatch.setattr(cp, "discover_cross_accounts", _boom)
    cp.cross_account_tick()
    runs = session.scalars(select(RunRecord).where(RunRecord.kind == "cross_account_discover")).all()
    assert len(runs) == 1 and runs[0].status == "failed" and "-412" in runs[0].detail


def test_兜底词表必须与_settings_默认一致() -> None:
    """★ **防漂移守卫**(2026-10-05)。

    `discover_cross_accounts` 的 `settings` 参数**没有默认值**,而调用方(含大量测试)
    常常不传 ⇒ `_account_keywords` 里那份 `_DEFAULT_BILI_KEYWORDS` **必须有,且得对**。
    ⚠️ 这条链原来就靠这个兜底工作 —— 2026-10-05 我改成轮转时**顺手去掉了它**,
    当场被 5 条测试打红(`bili_keywords` 变空 ⇒ 一个平台都没搜)。
    两处默认值飘了就会重现那种"静默什么都不做"。
    """
    from config.settings import Settings

    from app.services import cross_accounts as cp

    assert cp._DEFAULT_BILI_KEYWORDS == Settings.model_fields["cross_bili_keywords"].default, (
        "两处默认词表飘了 —— 改一处必须改另一处")
    # 窗口必须**严格小于**池子,否则没有轮转可言(每轮都用全部词 ⇒ 发现饱和)
    pool = [k.strip() for k in cp._DEFAULT_BILI_KEYWORDS.split(",") if k.strip()]
    assert cp._BILI_WINDOW < len(pool), "词池必须大于窗口,否则'轮转'是假的"


def test_settings_为_None_时也要出词() -> None:
    """反向:把兜底再去掉一次,这条必须红。"""
    from app.services import cross_accounts as cp

    assert cp._account_keywords(None, None, 3) == ["网盘资源", "夸克网盘", "百度网盘"]
