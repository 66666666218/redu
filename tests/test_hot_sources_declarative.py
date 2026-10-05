"""声明式热榜源的单测(2026-10-05)。

**不联网** —— 全部喂构造好的 payload,钉住三种"差一点点就静默返回空"的分支:
  · 点路径标题(掘金 `data[].content.title`)
  · JS 赋值剥壳(`var newest = [...]`,金十)
  · 平台自有成功码(华尔街见闻 **20000** 才是 OK)

⚠️ 这三种的共同点是:**出错时不报错、只是"没有热点"** —— 本仓的老毛病。
"""
from __future__ import annotations

import pytest

from app.services.hot_sources import (
    HotSourceError, JsonListSource, RssSource, _clean_title, _pick)


class _FakeResp:
    def __init__(self, body: str) -> None:
        self.text = body
        self.content = body.encode("utf-8")


def _patch(monkeypatch, body: str):
    """把 `creq.get` 换成固定响应(不联网)。"""
    import app.services.hot_sources as hs

    monkeypatch.setattr(hs.creq, "get",
                        lambda *a, **k: _FakeResp(body))


# ---------------- _pick ----------------

def test_pick_支持点路径():
    assert _pick({"a": {"b": "x"}}, "a.b") == "x"
    assert _pick({"a": {"b": "x"}}, "a") == {"b": "x"}


def test_pick_路径断掉返回_None_而不是抛错():
    assert _pick({"a": 1}, "a.b") is None
    assert _pick({}, "a.b") is None
    assert _pick("不是字典", "a") is None
    assert _pick({"a": None}, "a.b") is None


def test_pick_空键返回_None_不会把整个字典当成值():
    # ⚠️ 这条是防"没配 url_key 却把整条记录当链接串进 url"的那类脏数据
    assert _pick({"a": 1}, "") is None


# ---------------- _clean_title ----------------

def test_标题里的_html_标签要被洗掉():
    """实测 `wallstreetcn-quick` 把正文片段当标题给:`<p>菲律宾股指…</p>`。

    不洗就会**原样进推送卡片** —— 而且是"不报错"的那种脏(不洗也能入库)。
    """
    assert _clean_title("<p>菲律宾股指日内涨幅扩大至2%。</p>") == "菲律宾股指日内涨幅扩大至2%。"


def test_标题里的_实体与多余空白要被清掉():
    assert _clean_title("A &amp; B") == "A & B"
    assert _clean_title("  多   空白\n换行  ") == "多 空白 换行"


def test_长标签也要能洗掉_上限别卡太小():
    """⚠️ **回归测试**:第一版上限写 `{0,80}`,金十的 `<a href="https://cdn.jin10.com/…">`
    **单标签就 130 字符**,上线后实测仍有 18 行漏网。上限是**量出来的**。
    """
    tag = '<a href="https://cdn.jin10.com/vip_column/index.html#/detail/' + "x" * 60 + '">看</a>'
    assert len(tag) > 80
    assert _clean_title(tag) == "看"


def test_清洗不改写内容():
    """⚠️ 只去标签/解实体/压空白,**不许**截断或改写 —— 改了就成内容事故了。"""
    assert _clean_title("标题里有 < 和 > 但没标签") == "标题里有 < 和 > 但没标签"
    assert _clean_title("") == ""
    assert _clean_title(None) == ""


def test_jsonlist_标题走清洗(monkeypatch):
    _patch(monkeypatch, '{"code":0,"data":[{"title":"<p>一</p>"},{"title":"<p>二</p>"},'
                        '{"title":"<p>三</p>"},{"title":"<p>四</p>"},{"title":"<p>五</p>"}]}')
    rows = JsonListSource("t", "http://x", title_keys=("title",),
                          list_path="data").fetch()
    assert [r["title"] for r in rows] == ["一", "二", "三", "四", "五"]


def test_rss_标题也走清洗(monkeypatch):
    _patch(monkeypatch, '<?xml version="1.0"?><rss version="2.0"><channel>'
                        '<item><title><![CDATA[<b>甲</b>]]></title></item>'
                        '<item><title>乙 &amp; 丙</title></item>'
                        '</channel></rss>')
    rows = RssSource("r", "http://x").fetch()
    assert [r["title"] for r in rows] == ["甲", "乙 & 丙"]


# ---------------- JsonListSource ----------------

def test_点路径标题被正确取到_掘金形状(monkeypatch):
    body = ('{"err_no":0,"data":[{"content":{"title":"标题一"}},'
            '{"content":{"title":"标题二"}}]}')
    _patch(monkeypatch, body)
    rows = JsonListSource("t", "http://x", title_keys=("content.title",),
                          list_path="data").fetch()
    assert [r["title"] for r in rows] == ["标题一", "标题二"]


def test_js_赋值剥壳_金十形状(monkeypatch):
    # ⚠️ 给 6 条:自动找层那条启发式**要求 ≥5 条**(防"找错层"),只给 2 条会被判成
    # "解析不到列表" —— 那是测试数据的问题,不是被测代码的问题。
    items = ",".join('{"data":{"content":"快讯%d"}}' % i for i in range(6))
    _patch(monkeypatch, "var newest = [%s];" % items)
    rows = JsonListSource("t", "http://x", title_keys=("data.content",),
                          strip_js=True).fetch()
    assert [r["title"] for r in rows] == ["快讯%d" % i for i in range(6)]


def test_不剥壳时吃不了_js_赋值_反向验证(monkeypatch):
    """`strip_js=False` 时必须失败 —— 否则上面那条测不出东西。"""
    items = ",".join('{"title":"t%d"}' % i for i in range(6))
    _patch(monkeypatch, "var newest = [%s];" % items)
    with pytest.raises(HotSourceError):
        JsonListSource("t", "http://x", title_keys=("title",)).fetch()


def test_平台自有成功码_20000_要被当成功(monkeypatch):
    body = ('{"code":20000,"message":"OK","data":{"day_items":'
            '[{"title":"一","uri":"u1"},{"title":"二","uri":"u2"}]}}')
    _patch(monkeypatch, body)
    rows = JsonListSource("wscn", "http://x", title_keys=("title",),
                          list_path="data.day_items", url_key="uri",
                          ok_codes=(20000, 0, None)).fetch()
    assert [r["title"] for r in rows] == ["一", "二"]
    assert rows[0]["url"] == "u1"


def test_默认成功码下_20000_会被判成失败_反向验证(monkeypatch):
    """不传 `ok_codes` 时必须报错 —— 说明那个参数真的在起作用。"""
    _patch(monkeypatch, '{"code":20000,"data":{"day_items":[{"title":"一"}]}}')
    with pytest.raises(HotSourceError):
        JsonListSource("wscn", "http://x", title_keys=("title",),
                       list_path="data.day_items").fetch()


def test_业务码非零要当失败_不能回空(monkeypatch):
    """HTTP 200 + 业务码报错是最典型的"假成功" —— 必须抛,不能返回 []。"""
    _patch(monkeypatch, '{"code":10012,"message":"签名错误"}')
    with pytest.raises(HotSourceError):
        JsonListSource("cls", "http://x", title_keys=("title",)).fetch()


def test_解析不到列表要抛错而不是返回空(monkeypatch):
    _patch(monkeypatch, '{"code":0,"data":[]}')
    with pytest.raises(HotSourceError):
        JsonListSource("t", "http://x", title_keys=("title",), list_path="data").fetch()


def test_标题键不在记录里要抛错(monkeypatch):
    """字段名变了要**立刻响**,而不是安安静静入库 0 条。"""
    _patch(monkeypatch, '{"code":0,"data":[{"name":"a"},{"name":"b"}]}')
    with pytest.raises(HotSourceError):
        JsonListSource("t", "http://x", title_keys=("title",), list_path="data").fetch()


def test_自动找层_要多看几个元素(monkeypatch):
    """`_dig_titled` 的样本要跨元素 —— 只看 `node[0]` 会漏掉"首条无标题"的接口。"""
    body = ('{"code":0,"x":{"y":[{"ad":1},{"title":"真标题一"},{"title":"真标题二"},'
            '{"title":"真标题三"},{"title":"真标题四"},{"title":"真标题五"}]}}')
    _patch(monkeypatch, body)
    rows = JsonListSource("t", "http://x", title_keys=("title",)).fetch()
    assert len(rows) == 5
    assert rows[0]["title"] == "真标题一"


def test_limit_生效(monkeypatch):
    items = ",".join('{"title":"t%d"}' % i for i in range(10))
    _patch(monkeypatch, '{"code":0,"data":[%s]}' % items)
    rows = JsonListSource("t", "http://x", title_keys=("title",),
                          list_path="data").fetch(limit=3)
    assert len(rows) == 3


# ---------------- RssSource ----------------

_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>源</title>
<item><title><![CDATA[条目一]]></title><link>http://a/1</link></item>
<item><title>条目二</title><link>http://a/2</link></item>
</channel></rss>"""


def test_rss_能解析出标题与链接(monkeypatch):
    _patch(monkeypatch, _RSS)
    rows = RssSource("r", "http://x", "测试源").fetch()
    assert [r["title"] for r in rows] == ["条目一", "条目二"]
    assert rows[0]["url"] == "http://a/1"
    assert rows[0]["extra"] == "测试源"


def test_rss_空频道要抛错而不是返回空(monkeypatch):
    _patch(monkeypatch, '<?xml version="1.0"?><rss version="2.0"><channel/></rss>')
    with pytest.raises(HotSourceError):
        RssSource("r", "http://x").fetch()


def test_rss_响应不是_xml_要抛_HotSourceError(monkeypatch):
    """ⓐ 有的端点会把 HTML 错误页当 200 回来(如 hupu)—— 必须转成我们自己的异常。"""
    _patch(monkeypatch, "<html><body>404 not found</body></html>")
    with pytest.raises(HotSourceError):
        RssSource("r", "http://x").fetch()


# ---------------- RssSource 多镜像 ----------------

def _patch_urls(monkeypatch, mapping: dict):
    """按 URL 给不同响应(测多镜像用)。"""
    import app.services.hot_sources as hs

    def fake(url, *a, **k):
        if url not in mapping:
            raise ConnectionError("no route")
        return _FakeResp(mapping[url])

    monkeypatch.setattr(hs.creq, "get", fake)


_OK = '<?xml version="1.0"?><rss version="2.0"><channel><item><title>甲</title></item></channel></rss>'


def test_多镜像_第一个挂了自动用第二个(monkeypatch):
    """**这不是过度设计**:本仓跨两个网络部署,实测同一站两边可达性相反。"""
    _patch_urls(monkeypatch, {"http://b/feed": _OK})
    rows = RssSource("r", ("http://a/feed", "http://b/feed")).fetch()
    assert [r["title"] for r in rows] == ["甲"]


def test_多镜像_第一个通就不碰第二个(monkeypatch):
    calls: list[str] = []
    import app.services.hot_sources as hs

    def fake(url, *a, **k):
        calls.append(url)
        return _FakeResp(_OK)

    monkeypatch.setattr(hs.creq, "get", fake)
    RssSource("r", ("http://a/feed", "http://b/feed")).fetch()
    assert calls == ["http://a/feed"], "第一个通了还去打第二个就是浪费 + 多一份被风控的机会"


def test_多镜像_全挂时要把每个镜像的错都带出来(monkeypatch):
    """⚠️ 只报最后一个错会掩盖"哪几个镜像坏了" —— 排障时正需要这个。"""
    _patch_urls(monkeypatch, {})
    with pytest.raises(HotSourceError) as ei:
        RssSource("r", ("http://a/feed", "http://b/feed")).fetch()
    msg = str(ei.value)
    assert "a" in msg and "b" in msg


def test_单_url_仍然是合法的(monkeypatch):
    """兼容老写法:给一个字符串等价于单元素元组。"""
    _patch_urls(monkeypatch, {"http://a/feed": _OK})
    assert len(RssSource("r", "http://a/feed").fetch()) == 1
