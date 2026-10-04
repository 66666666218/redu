"""微博盘链源(2026-10-05)。

它是目前**质量最好**的盘链源(实测「资源 合集」19 条里 5 条带链;完整链路跑下来
21 个候选里 **17 条来自微博**)。这里钉住的是**踩过的三个坑**。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest  # noqa: E402

from app.services import cross_accounts as ca  # noqa: E402


class _R:
    def __init__(self, payload=None, text="", status=200, ctype="application/json"):
        self._p, self.text, self.status_code = payload, text, status
        self.headers = {"Content-Type": ctype}

    def json(self):
        return self._p


def _post(mblogid="Abc123", text="小说合集 链接：http://t.cn/xxxx",
          urls=("https://pan.quark.cn/s/abc123def456",), ads=False):
    return {
        "mblogid": mblogid, "isAd": ads,
        "text_raw": text,
        "user": {"id": 7654321, "screen_name": "资源铺阿甲"},
        "reposts_count": 12, "comments_count": 3, "attitudes_count": 45,
        "url_struct": [{"long_url": u, "ori_url": u} for u in urls],
    }


def test_search_weibo_reads_statuses_from_the_top_level(monkeypatch) -> None:
    """★ **坑一**:`statuses` 在**顶层**,**不是** `data.statuses`。

    我第一版按 `data.statuses` 读,得到"0 条"—— 而响应里其实有数据。
    """
    payload = {"ok": 1, "statuses": [_post()]}          # 顶层
    monkeypatch.setattr("requests.get", lambda *a, **k: _R(payload))
    rows = ca._search_weibo("ck", "小说")
    assert len(rows) == 1, "顶层 statuses 没被读到"


def test_search_weibo_keeps_all_links_and_dedupes_repeats(monkeypatch) -> None:
    """★ **一条微博挂多条链时要全收**(2026-10-05 改)。

    实测「资源 合集」里有帖子**同时挂百度 + 夸克** —— 只取第一条会漏。
    同时:同一条链会在 `long_url` / `ori_url` 里**重复出现**,必须去重,
    否则一条链会被当成两条(那正是我先前那个"66 条"虚报的来源,真实 ~22)。
    """
    u1 = "https://pan.baidu.com/s/1FnQLSdlKEr8pjDdNXZvk6A"
    u2 = "https://pan.quark.cn/s/494a0b616761"
    p = _post(urls=(u1, u2))
    p["url_struct"][0]["ori_url"] = u1                   # 同一链重复出现 → 要去重
    monkeypatch.setattr("requests.get", lambda *a, **k: _R({"statuses": [p]}))

    rows = ca._search_weibo("ck", "小说")
    assert len(rows) == 1                                 # 一条微博 = 一个"源记录"
    assert rows[0]["pan_links"] == [u1, u2], "两条链都要在,且按出现顺序"
    assert rows[0]["pan_link"] == u1                      # 兼容字段仍是第一条

    # 调用方对**每条链**各产一个候选
    from app.services.pan_discovery import _candidates_from_weibo

    cands = _candidates_from_weibo("ck", ["小说"], limit=20)
    assert [c["origin_url"] for c in cands] == [u1, u2], "两条链都该成为候选"


def test_search_weibo_skips_ads_and_keeps_panless_posts_with_empty_link(monkeypatch) -> None:
    """★ **两层职责要分清**(这正是设计意图):
    `_search_weibo` 是**源函数** —— 返回**全部**有效帖子(带 metrics,名字型/热度那条链也要用),
    没有盘链的帖子 `pan_link` 为空;**由调用方**(`_candidates_from_weibo`)筛出带链的。
    ⚠️ 两者混成一件事的话,"微博当盘链源"和"微博当热度源"就没法共用一个解析器了。
    """
    monkeypatch.setattr("requests.get", lambda *a, **k: _R({"statuses": [
        _post(mblogid="Ad", ads=True),                    # 广告:源函数就该丢掉
        _post(mblogid="NoPan", urls=()),                  # 没链:保留,但 pan_link 为空
        _post(mblogid="Good", urls=("https://pan.xunlei.com/s/XYZ789",)),
    ]}))
    rows = ca._search_weibo("ck", "小说")
    assert len(rows) == 2, "广告位不算内容,但要保留没链的帖子(热度那条链还要用)"
    by_mid = {r["url"].rsplit("/", 1)[-1]: r for r in rows}
    assert "Ad" not in by_mid
    assert by_mid["NoPan"]["pan_link"] == ""
    assert by_mid["Good"]["pan_link"] == "https://pan.xunlei.com/s/XYZ789"

    # 调用方负责筛:只有带链的才成为盘链候选
    from app.services.pan_discovery import _candidates_from_weibo

    cands = _candidates_from_weibo("ck", ["小说"], limit=20)
    assert [c["origin_url"] for c in cands] == ["https://pan.xunlei.com/s/XYZ789"]


def test_search_weibo_raises_on_expired_cookie(monkeypatch) -> None:
    """★★ **登录态失效要抛错,不能返回空列表** —— 空会被读成"今天没人发资源",
    而事实是"我们没凭据"。微博失效的特征是跳新浪通行证页(`retcode=6102`)。"""
    monkeypatch.setattr("requests.get", lambda *a, **k: _R(
        None, text="<html>...retcode=6102...</html>", ctype="text/html"))
    with pytest.raises(ca.SearchSourceError) as ei:
        ca._search_weibo("ck", "小说")
    assert "6102" in str(ei.value)


def test_search_weibo_raises_when_body_has_no_statuses(monkeypatch) -> None:
    """返回体结构变了也要报错,不能静默返回空。"""
    monkeypatch.setattr("requests.get", lambda *a, **k: _R({"unexpected": []}))
    with pytest.raises(ca.SearchSourceError):
        ca._search_weibo("ck", "小说")


def test_weibo_metrics_drop_zero_and_map_field_names() -> None:
    """微博给的是 **转发/评论/赞**;⚠️ 0 与缺失都不放进去(让 `conversion` 走降级,
    填 0 会被读成"没人看")。"""
    assert ca._weibo_metrics({"reposts_count": 12, "comments_count": 3,
                              "attitudes_count": 45}) == {
        "share_count": 12, "comment_count": 3, "liked_count": 45}
    assert ca._weibo_metrics({"reposts_count": 0, "attitudes_count": 0}) == {}
    assert ca._weibo_metrics({}) == {}


def test_weibo_is_wired_into_pan_discovery() -> None:
    """★ 接进了 `find_candidates`(与知乎/贴吧并列,一个源失败不影响另一个)。"""
    import inspect

    from app.services import pan_discovery as pd
    src = inspect.getsource(pd.find_candidates)
    assert "_candidates_from_weibo" in src
    assert 'get_cookie(session, user_id, "weibo")' in src
