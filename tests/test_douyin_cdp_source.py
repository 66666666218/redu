"""抖音 CDP 路单测(2026-10-25)。全部离线 —— 一个真请求都不发。

守的是三件事:
1. **页面行 → 上层记录**的映射(去重、`/video/<id>` 解析、URL 拼装);
2. **登录态只靠注入**:库里有 cookie 才搜,没有就明确报错(别拿未登录页当"没人推");
3. **连不上 / 读不到就抛**,绝不静默返回空列表(与 `mediacrawler_source.crawl` 同一条纪律)。
"""
import os

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
