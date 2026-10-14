"""公众号**阅读数**这条链的守卫(2026-10-14)。

用户当天的原话是「**阅读还是没修好**」——卡片上 37 篇新文的"阅读"列**整列 `—`**。
查下来这条链上有**三个各自静默**的坏点,而它们**没有一个会报错**:

  ① `WempClient` 解析 `appmsgpublish` 时**把 `read_num`/`like_num` 直接丢掉**
     (归一化只留 id/title/url/summary/时间);
  ② 监听轮 ⓪ 分支的 `norm` 投影**又丢一次**(只取 title/url/时间);
  ③ **结构性**:`MultiSourceClient.mp_articles` 一见到"有内容"的源就 `return`,
     而**只有 wemp(后台 `appmsgpublish`)那一路带阅读数** —— WeRSS 的返回结构里
     根本没有这个字段。⇒ 只要 WeRSS 活着,wemp **永远问不到**。

本仓反复出现的不是"某个 bug 没修",而是**同一条数据在 A 处带了、B 处又丢了**。
所以这个文件除了单测,最后还有两条**源码级守卫**:⓪ 分支必须用"带阅读数"的那条路、
必须把两个字段投影下去 —— 免得下次有人顺手改回去(那种改动**不会有任何测试变红**)。
"""
from __future__ import annotations

import json
import re

import pytest

from app.services.reader_platform_client import PlatformError


# ---------------------------------------------------------------- 计数解析

@pytest.mark.parametrize("raw, want", [
    (12345, 12345),
    ("12345", 12345),
    ("12,345", 12345),
    ("10万+", 100000),
    ("1.2万", 12000),
    (0, 0),
    ("", 0),
    ("—", 0),
    (None, 0),
    (True, 0),          # bool 不是数:后台给 true 时**不能**当成 1
    (-5, 0),            # 负数无意义 ⇒ 0(=不知道),绝不回退
])
def test_阅读数取值要认各种形态(raw, want) -> None:
    """后台是网页抓来的数,形态很杂(`"12345"` / `"10万+"` / 空串)。

    ⚠️ **取不出必须是 0**,因为 0 在这条链上的语义是"**不知道**"而不是"没有" ——
    `_insert_new_articles` 只在「旧值 0 且新值 >0」时回填,**从不写 0**
    (见 `_enrich` 里的长注释)。所以取不出只会"保持原样",不会把已知的读数抹成 0。
    """
    from app.services.wechat.wemp_client import _count

    assert _count(raw) == want


def test_候选字段名要挨个试() -> None:
    from app.services.wechat.wemp_client import _pick

    keys = ("read_num", "readNum", "read_count")
    assert _pick({"readNum": "888"}, keys) == 888       # 第一个名字没有,第二个有
    assert _pick({"read_num": 0, "read_count": 7}, keys) == 7   # 0 等于"不知道",继续往下找
    assert _pick({"title": "x"}, keys) == 0             # 一个都没有 ⇒ 0


# ---------------------------------------------------------------- WempClient

def _resp(payload):
    class _R:
        def json(self):
            return payload
    return _R()


def _publish_page(appmsgex: list[dict]) -> str:
    return json.dumps({"publish_list": [
        {"publish_info": json.dumps({"appmsgex": appmsgex})}]})


def test_wemp_必须把阅读数和点赞数带出来(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 坏点①:原来这两项被直接丢掉 ⇒ 即便后台活着,卡片阅读列也是全 `—`。"""
    from app.services.wechat import wemp_client as wc

    page = _publish_page([
        {"aid": "1_1", "title": "文一", "link": "https://mp.weixin.qq.com/s/a1",
         "create_time": 1788800000, "read_num": "6666", "like_num": 12},
        {"aid": "1_2", "title": "文二", "link": "https://mp.weixin.qq.com/s/a2",
         "create_time": 1788800001, "readNum": 888},      # 换一个候选字段名也得认
    ])
    monkeypatch.setattr(wc, "_shape_logged", False)
    monkeypatch.setattr(wc.requests, "get", lambda *a, **kw: _resp(
        {"base_resp": {"ret": 0}, "publish_page": page}))

    items = wc.WempClient("ck=x", "t").mp_articles("MP_WXS_1")
    assert [it["read_num"] for it in items] == [6666, 888]
    assert items[0]["like_num"] == 12
    assert items[1]["like_num"] == 0        # 没给 ⇒ 0(=不知道),不编


def test_第一次解析要把后台真实字段名打进日志(monkeypatch, caplog) -> None:
    """后台的字段名**没有验证过**(实测时凭据已死)。

    所以名字不猜死:`_READ_KEYS` 只是候选,而**第一次真解析成功时把真实字段名打出来**
    —— 让第一次真会话自己回答"它到底叫什么"。没有这条日志,名字猜错了就是
    "阅读数一直是 0",而**没有任何地方会说出原因**。
    """
    from app.services.wechat import wemp_client as wc

    monkeypatch.setattr(wc, "_shape_logged", False)
    page = _publish_page([{"aid": "1", "title": "文", "link": "https://mp.weixin.qq.com/s/a",
                           "read_num": 5, "whatever_new_field": 1}])
    monkeypatch.setattr(wc.requests, "get", lambda *a, **kw: _resp(
        {"base_resp": {"ret": 0}, "publish_page": page}))
    with caplog.at_level("INFO"):
        wc.WempClient("ck=x", "t").mp_articles("MP_WXS_1")
        wc.WempClient("ck=x", "t").mp_articles("MP_WXS_1")   # 第二次不该再打
    hits = [r for r in caplog.records if "appmsgpublish 文章字段" in r.getMessage()]
    assert len(hits) == 1, "字段名日志必须**恰好一次**(每进程一次,不是每篇一次)"
    assert "whatever_new_field" in hits[0].getMessage(), "真实字段名没打出来就白留了"


# ---------------------------------------------------------------- 跨源合并

class _BE:
    """假列表源:`mp_articles` 返回预置数据或抛预置异常,记录被调用次数。"""

    def __init__(self, items, exc=None) -> None:
        self.items, self.exc, self.calls = items, exc, 0

    def mp_articles(self, mp_id, page=1, limit=20):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return self.items


def _art(title, url, read=0, like=0, ts=1788800000):
    return {"id": url, "title": title, "url": url, "summary": "",
            "publish_at_raw": ts, "read_num": read, "like_num": like}


# 实测形态:库里存的是**短链**(见 `wechat_articles` 最近 12 行,清一色这种)
_SHORT = "https://mp.weixin.qq.com/s/pnzKiYr4-bbVY9qpuqBxcw"
# 后台 `appmsgpublish` 给的是**长链**(带分享噪声尾巴)
_LONG_SAME = ("http://mp.weixin.qq.com/s?__biz=MjM5NzI1MTY0MQ==&mid=2655483036&idx=1"
              "&sn=fd5c5b4a2c34d648e6989f0d9014ff59&chksm=abc&scene=27#wechat_redirect")


def test_两级链接形态不同也必须能补上阅读数() -> None:
    """★ **最可能真实发生的形态**:WeRSS 给短链、后台给长链,**同一篇文章**。

    直接比 url 会判成两条 ⇒ 阅读数**永远补不上、而且全程静默**。
    这里就按真实形态摆一次:短链 + 长链 + 同一个标题 ⇒ 必须补上。
    """
    from app.services.wechat._source import MultiSourceClient

    werss = _BE([_art("某资源合集", _SHORT)])
    wemp = _BE([_art("某资源合集", _LONG_SAME, read=4321, like=88)])
    stats: dict = {}
    m = MultiSourceClient([("werss", werss), ("wemp", wemp)])
    out = m.mp_articles_with_reads("MP_WXS_1", stats=stats)

    assert len(out) == 1
    assert out[0]["read_num"] == 4321 and out[0]["like_num"] == 88
    # ★ url/title **一个字符都不许动**(入库去重就是按 url 做的)
    assert out[0]["url"] == _SHORT
    assert out[0]["title"] == "某资源合集"
    assert stats["reads_src"] == 1 and stats["reads_from"] == 1


def test_两源都给长链时按biz_mid_idx对上_不靠标题() -> None:
    from app.services.wechat._source import MultiSourceClient

    long_a = _LONG_SAME
    long_b = _LONG_SAME.replace("chksm=abc", "chksm=zzz").replace("scene=27", "scene=99")
    werss = _BE([_art("文 A", long_a)])
    wemp = _BE([_art("文 A", long_b, read=999)])
    stats: dict = {}
    out = MultiSourceClient([("werss", werss), ("wemp", wemp)]).mp_articles_with_reads(
        "MP_WXS_1", stats=stats)
    assert out[0]["read_num"] == 999
    assert stats.get("reads_via_title", 0) == 0, "精确键能对上就不该走标题兜底"


def test_标题重名时宁可补不上也不补错() -> None:
    """⚠️ 兜底的红线:同一号 20 篇里可能有同名(系列文"日报"),一旦重名就**放弃**。

    宁可这轮补不上(`unmatched` 计数会把它暴露出来),也不给**错行**安**错读数** ——
    后者会让运维看着一个数字以为"修好了",比空着更坏。
    """
    from app.services.wechat._source import MultiSourceClient

    werss = _BE([_art("日报", "https://mp.weixin.qq.com/s/aaa111"),
                 _art("日报", "https://mp.weixin.qq.com/s/bbb222")])
    wemp = _BE([_art("日报", "http://mp.weixin.qq.com/s?__biz=B&mid=1&idx=1", read=777)])
    stats: dict = {}
    out = MultiSourceClient([("werss", werss), ("wemp", wemp)]).mp_articles_with_reads(
        "MP_WXS_1", stats=stats)
    assert [it["read_num"] for it in out] == [0, 0], "重名还敢兜底 = 给错行安错读数"
    assert stats["reads_unmatched"] == 1, "对不上必须计数,否则'没补上'是静默的"


def test_读数只许往高走_更小的旧快照不许覆盖() -> None:
    """阅读数是**单调量**。源抖动给出更小的旧快照时**不许回退** ——
    与 `_enrich._insert_new_articles` 用的是同一条纪律(那里被测试当场挡过一次)。"""
    from app.services.wechat._source import MultiSourceClient

    werss = _BE([_art("文", _SHORT, read=500)])
    wemp = _BE([_art("文", _LONG_SAME, read=7, like=3)])
    out = MultiSourceClient([("werss", werss), ("wemp", wemp)]).mp_articles_with_reads("MP_WXS_1")
    assert out[0]["read_num"] == 500, "更小的读数把 500 覆盖掉了"
    assert out[0]["like_num"] == 3, "点赞数原来没有(0)⇒ 该补上"


def test_源没带数时什么都不改() -> None:
    """`src>0 而 from=0` 正是**"问到了但它没带数"**的指纹(大概率字段名不对)——
    这时绝不能把 base 的字段改坏。"""
    from app.services.wechat._source import MultiSourceClient

    werss = _BE([_art("文", _SHORT, read=42)])
    wemp = _BE([_art("文", _LONG_SAME, read=0, like=0)])   # 问到了,但一个数都没带
    stats: dict = {}
    out = MultiSourceClient([("werss", werss), ("wemp", wemp)]).mp_articles_with_reads(
        "MP_WXS_1", stats=stats)
    assert out[0]["read_num"] == 42
    assert stats["reads_src"] == 1 and stats["reads_from"] == 0, "src/from 要分得开"


def test_横向源死了会被熔断_不能每号都去撞() -> None:
    """wemp 凭据现在就是死的(`200003`)。如果每号每轮都去撞一遍,
    这轮 81 个号就是 81 次白撞 —— 与"在已风控的号上重试把冷却推后"是同一类自伤。"""
    from app.services.wechat._source import MultiSourceClient
    from app.services.wechat.wemp_client import WempAuthError

    werss = _BE([_art("文", _SHORT)])
    wemp = _BE([], exc=WempAuthError("会话失效(200003)"))
    m = MultiSourceClient([("werss", werss), ("wemp", wemp)])

    assert m.mp_articles_with_reads("MP_WXS_1")[0]["read_num"] == 0
    assert wemp.calls == 1
    # 第二个号:wemp 已熔断(600s)⇒ **一次都不该再问**
    m.mp_articles_with_reads("MP_WXS_2")
    assert wemp.calls == 1, "熔断没生效 = 每号都会去撞一次死源"
    assert werss.calls == 2


def test_取不到数时与mp_articles逐字一致() -> None:
    """**兼容性红线**:没有横向源可补时(单源/全冷却/都不带数),
    `mp_articles_with_reads` 的结果必须与 `mp_articles` **完全一样** ——
    否则这条改动会顺手改掉 ⓪ 分支的既有行为。"""
    from app.services.wechat._source import MultiSourceClient

    items = [_art("文", _SHORT)]
    m = MultiSourceClient([("werss", _BE(list(items)))])
    assert m.mp_articles_with_reads("MP_WXS_1") == list(items)

    # 全失败:抛**最后**一个异常(与 mp_articles 同一语义)
    m2 = MultiSourceClient([("x", _BE([], exc=PlatformError("x 限流"))),
                            ("y", _BE([], exc=PlatformError("y 会话失效")))])
    with pytest.raises(PlatformError):
        m2.mp_articles_with_reads("MP_WXS_1")

    # 全空:返回空列表
    m3 = MultiSourceClient([("b", _BE([])), ("c", _BE([]))])
    assert m3.mp_articles_with_reads("MP_WXS_1") == []


def test_空列表的源不算横向源() -> None:
    """空列表 = 该源正常答"没有",不是"答上了"。若把它当横向源,
    `stats["reads_src"]` 会虚高,运维就看不出"横向源其实一次都没答上话"。"""
    from app.services.wechat._source import MultiSourceClient

    stats: dict = {}
    out = MultiSourceClient([("werss", _BE([_art("文", _SHORT)])),
                             ("wemp", _BE([]))]).mp_articles_with_reads("MP_WXS_1", stats=stats)
    assert len(out) == 1 and "reads_src" not in stats


# ---------------------------------------------------------------- 键

def test_候选键的三级形态() -> None:
    from app.services.wechat._source import article_keys

    # 长链:原样 + biz|mid|idx + host+path 三级
    keys = article_keys(_LONG_SAME)
    assert keys[0].startswith("http://mp.weixin.qq.com/s?__biz=")
    assert "MjM5NzI1MTY0MQ==|2655483036|1" in keys
    assert "mp.weixin.qq.com/s" in keys
    # 短链:没有 __biz,只有两级
    assert article_keys(_SHORT) == (_SHORT, "mp.weixin.qq.com/s/pnzKiYr4-bbVY9qpuqBxcw")
    # 尾巴里的分享噪声不影响第③级
    assert article_keys(_LONG_SAME.replace("#wechat_redirect", ""))[2] == keys[2]


@pytest.mark.parametrize("bad", ["", "   ", "not a url", "javascript:void(0)"])
def test_取不出键就返回空_不许拿空串当键(bad: str) -> None:
    """⚠️ 空串当键会把**所有**取不出键的条目并成一条 —— 那是静默串号。"""
    from app.services.wechat._source import article_keys

    assert article_keys(bad) == ()


def test_App路的投影也不许丢阅读数() -> None:
    """★ 坏点①②的**第二道门**:`_append_listed`(App 兜底路的投影)。

    它和 ⓪ 分支的 `norm` 是**同一个病、两个地方** —— 本仓最常吃的亏就是"一处修了、另一处没修"。
    这条守卫不是凭空写的:**本次改动过程中它真的被弄坏过一次**,而且是**静默的** ——
    端到端那条(`test_监听轮的横向源阅读数要一路落进库`)走的是 ⓪ 分支,**看不见 App 路**;
    唯一抓得住的是 `test_网页路被拦时_App_兜底要把阅读数接回来`。
    所以这里把"两个投影都要带上这两个字段"钉死。
    """
    src = _read("app/services/wechat/_listen.py")
    m = re.search(r"def _append_listed\(.*?\n(.*?)\n\n\n", src, re.S)
    assert m, "没找到 _append_listed(代码结构变了?守卫要跟着改)"
    body = m.group(1)
    assert '"read_num": it.get("read_num")' in body, "App 路的投影把阅读数丢了"
    assert '"like_num": it.get("like_num")' in body, "App 路的投影把点赞数丢了"


def test_两个投影的读数类字段必须一致() -> None:
    """⓪ 分支与 App 路**必须把同一批"读数类"字段送下去**。

    ⚠️ 这条是"结构性"守卫,不是重复:两处各写一遍投影本身就是**隐患的根源**
    (本仓"同一坑在两条链上只修一条"的记录已经很多)。这里只比**读数类**字段 ——
    其余字段两处**本就不同**(App 路的 url 是 `build_mp_url(it["original_id"])` 拼的,
    不是原样取),硬比全字段只会变成一条**随时误报**的脆测试。
    """
    src = _read("app/services/wechat/_listen.py")
    n = re.search(r"norm = \[(.*?)for it in raw_items\]", src, re.S)
    a = re.search(r"def _append_listed\(.*?\n(.*?)\n\n\n", src, re.S)
    assert n and a, "代码结构变了,守卫要跟着改"
    for name, block in (("⓪ 分支 norm", n.group(1)), ("App 路 _append_listed", a.group(1))):
        assert re.search(r'"read_num":\s*it', block), f"{name} 没了 read_num"
        assert re.search(r'"like_num":\s*it', block), f"{name} 没了 like_num"
        assert re.search(r'"title":\s*it', block), f"{name} 没了 title"


# ---------------------------------------------------------------- 源码级守卫

def _read(rel: str) -> str:
    import pathlib
    return (pathlib.Path(__file__).resolve().parent.parent / rel).read_text(encoding="utf-8")


def test_0号分支必须走带阅读数的那条路() -> None:
    """★ 坏点③的守卫:⓪ 分支若改回 `plat.mp_articles(...)`,
    **wemp 的阅读数就永远进不来**(WeRSS 结构里没有这个字段),而**不会有任何测试变红** ——
    因为它照样返回一列文章,只是阅读数全是 0。这种"静默退回去"必须由源码守卫挡住。
    """
    src = _read("app/services/wechat/_listen.py")
    assert "mp_articles_with_reads" in src, (
        "⓪ 分支改回 mp_articles 了:阅读数会**静默**全变 `—`(WeRSS 不带这个字段)")
    # 而且必须传 stats —— 不然 `runs.detail` 里没有 reads(...) 那一截,
    # "补没补上 / 为什么没补上"又只能靠猜。
    assert re.search(r"_with_reads\([^)]*stats=wr_stats", src), \
        "没把 stats 传进去:运维记录里看不到补阅读数的账"
    # 单源客户端 / 测试替身没有这个方法 ⇒ 必须留着 `mp_articles` 的退回路径
    assert re.search(r"else plat\.mp_articles\(feed_id", src), \
        "把退回路径删了:单源配置(生产 .env 里 `wechat_reader_*` 为空时就是这样)会直接 AttributeError"


def test_0号分支的投影不许再把阅读数丢掉() -> None:
    """★ 坏点②的守卫:`norm` 是通往 `_insert_new_articles` 的**唯一**一道门。

    这道门一漏,后面 `_enrich` 的 `preset_read` 回填逻辑再完整也**拿不到数**
    (`it.get("read_num")` 恒为 None)。这一处曾让整条链看起来"到处都对"。
    """
    src = _read("app/services/wechat/_listen.py")
    m = re.search(r"norm = \[(.*?)for it in raw_items\]", src, re.S)
    assert m, "没找到 0 号分支的 norm 投影(代码结构变了?守卫要跟着改)"
    block = m.group(1)
    assert '"read_num": it.get("read_num")' in block, "阅读数又在这道门上被丢掉了"
    assert '"like_num": it.get("like_num")' in block, "点赞数又在这道门上被丢掉了"


# ---------------------- 卡片按「账号+标题」收敛(2026-10-14:用户报"显示也有问题")
#
# 现象:有些号**每天重发同名文章**(`xx风味笔记`/`现在就出发` 这类系列)⇒ 卡片上同一标题
# 连着出现 2~3 次,读的人第一反应是"卡片重复了/去重坏了"。⚠️ **入库去重没问题**
# (那些行的 URL 与 `article_keys()` 三级候选键各不相同 ⇒ 是不同文章)⇒ 收的是**显示**。

def _mk_article(author: str, title: str, *, day: int, pan: str = ""):
    from datetime import datetime, timedelta

    from app.db.models import WechatArticle

    return WechatArticle(user_id=1, author=author, title=title, url=f"https://x/{title}/{day}",
                         pan_types=pan, created_at=datetime.now() - timedelta(days=3),
                         publish_at=datetime.now() - timedelta(days=day))


class TestCollapseSameTitle:
    def test_同号同标题只留最新_并带上次数(self) -> None:
        from app.services.wechat._listen import _collapse_repeats

        rows = [_mk_article("某号", "黄山风味笔记：虾、鱼", day=4),
                _mk_article("某号", "黄山风味笔记：虾、鱼", day=1),   # 最新
                _mk_article("某号", "黄山风味笔记：虾、鱼", day=2),
                _mk_article("某号", "另一种标题", day=1)]
        out = _collapse_repeats(rows)
        assert len(out) == 2, f"同标题应收成一行:{[(a.title, n) for a, n in out]}"
        (first, n1), (second, n2) = out
        assert (first.title, n1) == ("黄山风味笔记：虾、鱼", 3)
        assert first.publish_at == max(r.publish_at for r in rows[:3]), "要留最新的那条"
        assert (second.title, n2) == ("另一种标题", 1)

    def test_标题差一个后缀的不许并(self) -> None:
        """⚠️ 别过度合并:`xx最新版` 是**另一篇**,并掉就丢信息了。"""
        from app.services.wechat._listen import _collapse_repeats

        out = _collapse_repeats([_mk_article("某号", "最新可用书源", day=2),
                                 _mk_article("某号", "最新可用书源最新版", day=1)])
        assert len(out) == 2 and all(n == 1 for _, n in out)

    def test_不同号同名文章不许并(self) -> None:
        from app.services.wechat._listen import _collapse_repeats

        out = _collapse_repeats([_mk_article("甲号", "同名资源", day=2), _mk_article("乙号", "同名资源", day=1)])
        assert len(out) == 2, "跨账号不合并(按账号分组是卡片的核心口径)"
