"""抖音 CDP 路单测(2026-10-25)。全部离线 —— 一个真请求都不发。

守的是三件事:
1. **页面行 → 上层记录**的映射(去重、`/video/<id>` 解析、URL 拼装);
2. **登录态只靠注入**:库里有 cookie 才搜,没有就明确报错(别拿未登录页当"没人推");
3. **连不上 / 读不到就抛**,绝不静默返回空列表(与 `mediacrawler_source.crawl` 同一条纪律)。
"""
import os
from pathlib import Path

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest

from app.services import douyin_cdp_source as dc


def test_行映射_去重并拼出作品链接():
    raw = {"rows": [
        {"vid": "7380308675841297704", "text": "《白泽的梦》教程", "author": "某号"},
        {"vid": "7380308675841297704", "text": "重复", "author": ""},
        {"vid": "", "text": "没有 id 的行要丢掉", "author": "x"},
    ]}
    rows = dc._rows_of(raw, "白泽")
    assert len(rows) == 1, rows
    r = rows[0]
    assert r["url"] == "https://www.douyin.com/video/7380308675841297704"
    assert "《白泽的梦》" in r["snippet"] and r["name"] == "某号"
    assert r["keyword"] == "白泽" and r["pan_link"] == ""


def test_搜索地址要做URL编码():
    u = dc._search_url("白泽 的梦")
    assert "type=video" in u and "%20" in u and "/search/" in u


# ---------------------------------------------------------------------------
# 发布时间:卡片上是**相对写法**,缺了它整条会被新鲜度闸丢掉(2026-10-28)
# ---------------------------------------------------------------------------

#: 实测的卡片原文(`innerText`,5 行:时长 / 点赞 / 标题 / @作者 / 时间)。
CARD = "06:22\n13.4万\n童哥冬眠前安排顿丰盛的 #美食\n@猪猪狠无敌\n1小时前"


def test_从卡片取发布时间_不是拿时长():
    """第一行 `06:22` 是**时长**;把它当发布时间会全错(所以不认 HH:MM)。"""
    ts = dc._publish_at(CARD)
    assert ts > 0
    from datetime import datetime

    assert datetime.fromtimestamp(ts).date() == datetime.now().date()


def test_相对时间各档都能认():
    import time as _t
    from datetime import datetime

    from app.services.hot_time import parse_published_coarse as p

    now = _t.time()
    assert now - 5 * 86400 < p("5天前").timestamp() < now - 4 * 86400
    assert now - 8 * 86400 < p("1周前").timestamp() < now - 6 * 86400
    assert now - 100 * 86400 < p("3月前").timestamp() < now - 80 * 86400
    assert datetime.now().year - 6 < p("5年前").year < datetime.now().year - 4
    #: 卡片首行是**时长**、作者行是 `@昵称` —— 都不许被当成时间
    assert p("06:22") is None and p("@某某") is None and p("合集") is None


def test__rows_of_带上发布时间_否则会被新鲜度闸丢光():
    """★ 回归:CDP 的行缺 `publish_at` ⇒ `collect_leads` 判成"判不出时间"整条丢弃,
    运行记录却还写 success(0 条) —— 静默失败。"""
    rows = dc._rows_of({"rows": [{"vid": "1", "text": CARD, "author": "猪猪狠无敌"}]}, "美食")
    assert rows and rows[0]["publish_at"] > 0
    assert rows[0]["name"] == "猪猪狠无敌"
    assert "@猪猪狠无敌" not in rows[0]["snippet"]


def test_判不出时间就给0_不编():
    rows = dc._rows_of({"rows": [{"vid": "2", "text": "合集\n03:10\n7.4万\n标题", "author": ""}]}, "词")
    assert rows[0]["publish_at"] == 0


class _Db:
    def __init__(self, ck):
        self.ck = ck


def test_没有_cookie_就明确报错_不许拿未登录页当结果(monkeypatch):
    monkeypatch.setattr("app.services.cookie_store.get_cookie", lambda *a, **k: "")
    with pytest.raises(dc.DouyinCdpError) as ei:
        dc._cookies_of(_Db(""), 1)
    assert "没有抖音 cookie" in str(ei.value)


def test_cookie_摊成_playwright_结构(monkeypatch):
    from app.services import cookie_store

    monkeypatch.setattr(cookie_store, "get_cookie",
                        lambda *a, **k: "sessionid=abc; ttwid=def; 坏片段")
    got = dc._cookies_of(_Db(""), 1)
    assert {c["name"] for c in got} == {"sessionid", "ttwid"}
    assert all(c["domain"] == ".douyin.com" for c in got)


def test_端点连不上就抛_不许返回空列表(monkeypatch):
    monkeypatch.setattr(dc, "ensure_running", lambda *a, **k: (False, "CDP 不可达"))
    with pytest.raises(dc.DouyinCdpError) as ei:
        dc.search(["词"], session=_Db("x"))
    assert "CDP 不可达" in str(ei.value)


def test_词间休眠有硬下限():
    class _S:
        douyin_cdp_between_kw_ms = 1        #: 配成 1ms 也不许生效

    assert dc.between_kw_ms(_S()) >= 5000


def test_没有监听就返回没关掉():
    """`_close_browser` 在没有监听时应如实返回 False(别假装关了)。"""
    assert dc._close_browser(59998) is False


def test_端口从端点解析():
    class _S:
        douyin_cdp_url = "http://127.0.0.1:9444"

    assert dc._port(_S()) == 9444


def test_抖音档案也是绝对路径():
    """同一条教训:相对 `--user-data-dir` 会让 Edge 退回默认档案(动到用户的浏览器)。"""
    assert dc.profile_dir().is_absolute()


# ---------------------------------------------------------------------------
# 档案被标记 ⇒ 隔离 + 换全新档案重试一次(2026-10-28)
#
# 起因:CDP 路 10-26 起 15/15 轮全失败,报「验证码中间页」。受控三步实测:
#   ① 旧档案连 `https://www.douyin.com/` 首页都是验证码页(body 4 字节);
#   ② 全新空白档案(不注入 cookie)正常;
#   ③ 全新空白档案 + 注入库里那 66 条 cookie ⇒ **正常且已登录**;
#   ④ 隔离旧档案后跑 `search(["美食"])` ⇒ 16 条。
# ⇒ 不是 IP、不是 cookie、不是账号,就是**那份档案的身份**。登录靠注入,换档案不丢登录。
# ---------------------------------------------------------------------------


class _Settings:
    douyin_cdp_url = "http://127.0.0.1:59999"
    douyin_cdp_profile = "data/_douyin_profile"
    douyin_cdp_between_kw_ms = 5000
    douyin_cdp_quarantine_cooldown_hours = 12


def test_验证码中间页被标成档案可疑():
    """只有它才会触发"换档案"这个动作 —— 别的失败(未登录/连不上)都不许。"""
    e = dc.DouyinCdpError("验证码中间页", profile_suspect=True)
    assert e.profile_suspect is True
    assert dc.DouyinCdpError("连不上").profile_suspect is False


def test_首页预热遇到验证码页也要标成档案可疑():
    class _Page:
        def goto(self, *a, **k):
            return None

        def evaluate(self, *a, **k):
            return {"title": "验证码中间页", "captcha": True}

    with pytest.raises(dc.DouyinCdpError) as ei:
        dc._warmup(_Page())
    assert ei.value.profile_suspect is True
    assert "连首页" in str(ei.value)


def test_首页预热失败不抛(monkeypatch):
    """预热是"改善形状"的一步,不该变成新的失败点。"""

    class _Page:
        def goto(self, *a, **k):
            raise RuntimeError("net::ERR_TIMED_OUT")

    dc._warmup(_Page())          # 不抛就是通过


def test_隔离档案_会新建空档案并只留最近一份(monkeypatch, tmp_path):
    monkeypatch.setattr(dc, "_close_browser", lambda *a, **k: True)
    prof = tmp_path / "_dy_prof"
    prof.mkdir()
    (prof / "Default").mkdir()
    #: 更早的两份隔离档案(每份几百 MB,攒着会把盘吃满)
    for tag in ("20260101-000000", "20260102-000000"):
        (tmp_path / f"_dy_prof{dc._QUARANTINE_TAG}{tag}").mkdir()

    class _S(_Settings):
        douyin_cdp_profile = str(prof)

    moved = dc.quarantine_profile(_S())

    assert moved and "flagged" in moved
    assert not prof.exists()                     #: 原路径必须腾空,下次启动才建新档案
    left = sorted(p.name for p in tmp_path.glob(f"_dy_prof{dc._QUARANTINE_TAG}*"))
    assert left == [Path(moved).name], left


def test_隔离_换新档案仍被挡_才记冷却(monkeypatch):
    """判据是"**换全新档案也没用**"这个事实,不是"又失败了"。"""
    state = {"n": 0}

    def fake_round(kws, cookies, settings, pre_up):
        state["n"] += 1
        raise dc.DouyinCdpError("验证码中间页", profile_suspect=True)

    monkeypatch.setattr(dc, "available", lambda *a, **k: (False, ""))
    monkeypatch.setattr(dc, "ensure_running", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(dc, "_cookies_of", lambda *a, **k: [{"name": "a", "value": "b"}])
    monkeypatch.setattr(dc, "_one_round", fake_round)
    monkeypatch.setattr(dc, "quarantine_profile", lambda *a, **k: "moved")
    marked = []
    monkeypatch.setattr(dc, "_mark_quarantine_failed",
                        lambda *a, **k: marked.append(1))

    with pytest.raises(dc.DouyinCdpError) as ei:
        dc.search(["词"], session=_Db("x"), settings=_Settings())

    assert state["n"] == 2, "只许重试一次,别无限隔离"
    assert marked == [1], "换新档案也没用 ⇒ 要记冷却"
    assert "不是档案的问题" in str(ei.value)


def test_隔离后重试成功就返回(monkeypatch):
    calls = []

    def fake_round(kws, cookies, settings, pre_up):
        calls.append(pre_up)
        if len(calls) == 1:
            raise dc.DouyinCdpError("验证码中间页", profile_suspect=True)
        return [{"uid": "1", "name": "", "url": "u", "snippet": "", "pan_link": ""}]

    monkeypatch.setattr(dc, "available", lambda *a, **k: (False, ""))
    monkeypatch.setattr(dc, "ensure_running", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(dc, "_cookies_of", lambda *a, **k: [{"name": "a", "value": "b"}])
    monkeypatch.setattr(dc, "_one_round", fake_round)
    monkeypatch.setattr(dc, "quarantine_profile", lambda *a, **k: "moved")
    marked = []
    monkeypatch.setattr(dc, "_mark_quarantine_failed",
                        lambda *a, **k: marked.append(1))

    rows = dc.search(["词"], session=_Db("x"), settings=_Settings())

    assert len(rows) == 1
    assert calls == [False, False], "起的是我们自己的浏览器 ⇒ 两次都要由我们收尾"
    assert marked == [], "重试成功就不该记冷却"


def test_隔离失败要说隔离失败_不许说成换了新档案也没用(monkeypatch):
    """两个结论的动作不同:前者要人去腾目录,后者是等风控过去。"""
    monkeypatch.setattr(dc, "available", lambda *a, **k: (False, ""))
    monkeypatch.setattr(dc, "ensure_running", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(dc, "_cookies_of", lambda *a, **k: [{"name": "a", "value": "b"}])

    def fake_round(*a, **k):
        raise dc.DouyinCdpError("验证码中间页", profile_suspect=True)

    monkeypatch.setattr(dc, "_one_round", fake_round)
    monkeypatch.setattr(dc, "quarantine_profile", lambda *a, **k: "")

    with pytest.raises(dc.DouyinCdpError) as ei:
        dc.search(["词"], session=_Db("x"), settings=_Settings())
    msg = str(ei.value)
    assert "隔离失败" in msg
    assert "一样被挡" not in msg


def test_非档案原因的失败不触发隔离(monkeypatch):
    monkeypatch.setattr(dc, "available", lambda *a, **k: (False, ""))
    monkeypatch.setattr(dc, "ensure_running", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(dc, "_cookies_of", lambda *a, **k: [{"name": "a", "value": "b"}])

    def fake_round(*a, **k):
        raise dc.DouyinCdpError("显示未登录")

    monkeypatch.setattr(dc, "_one_round", fake_round)
    q = []
    monkeypatch.setattr(dc, "quarantine_profile", lambda *a, **k: q.append(1) or "")

    with pytest.raises(dc.DouyinCdpError) as ei:
        dc.search(["词"], session=_Db("x"), settings=_Settings())
    assert "未登录" in str(ei.value)
    assert q == [], "登录过期不是档案问题,不许丢档案"


def test_token_on_the_author_line_is_kept() -> None:
    """★ 2026-10-29:实测有一批单机游戏号把夸克令牌写在作者同一行 ——
    `@未来C /~7fda3YJwIo~:/`。原来整行剔掉 ⇒ `collect_leads` 看不到口令,
    这批**带网盘的帖子一条都进不来**。现在剔作者名,但**行内令牌/链接要留下**。"""
    from app.services.douyin_cdp_source import _rows_of

    raw = {"rows": [{"vid": "1", "author": "未来C",
                     "text": "00:33 17 杀手3合集 免费下载\n@未来C /~7fda3YJwIo~:/"}]}
    rows = _rows_of(raw, "kw")
    assert "/~7fda3YJwIo~" in rows[0]["snippet"]
    assert "@未来C" not in rows[0]["snippet"]
