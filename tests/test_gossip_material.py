"""素材自动采集单测(2026-10-25)。**全部离线** —— 微博/抖音都被换掉。"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest

from app.services import gossip_material as gm


class _Db:
    def close(self):
        pass


def test_两个来源都成功时_每条都带来源标签(monkeypatch):
    from app.services import douyin_cdp_source, weibo_search

    monkeypatch.setattr(weibo_search, "search", lambda *a, **k: [
        {"name": "某博主", "snippet": "廖碧儿承认恋情,对象是吕礼章"},
        {"name": "另一人", "snippet": "廖碧儿承认恋情,对象是吕礼章"},   # 重复要去掉
        {"name": "第三人", "snippet": "她说感情与年龄无关"},
    ])
    monkeypatch.setattr(douyin_cdp_source, "search", lambda *a, **k: [
        {"name": "某号", "snippet": "两人因红酒结缘"},
    ])
    got = gm.gather("廖碧儿承认恋情", session=_Db(), use_baidu=False)
    assert got["counts"] == {"微博": 2, "抖音": 1}, got["counts"]
    assert "【微博】@某博主:" in got["text"]
    assert "【抖音】某号:" in got["text"]
    assert got["text"].count("廖碧儿承认恋情,对象是吕礼章") == 1, "重复素材要去掉"
    assert got["fails"] == {}


def test_一个平台挂了另一个还能用_且失败要说出来(monkeypatch):
    """⚠️ 「微博今天没内容」与「微博抓取挂了」长得一样 —— 必须带 `fails` 说出来。"""
    from app.services import douyin_cdp_source, weibo_search

    monkeypatch.setattr(weibo_search, "search",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("微博 401")))
    monkeypatch.setattr(douyin_cdp_source, "search", lambda *a, **k: [
        {"name": "某号", "snippet": "抖音那边的内容"}])
    got = gm.gather("某热点", session=_Db(), use_baidu=False)
    assert got["counts"]["抖音"] == 1 and "微博" in got["fails"]
    assert "微博 401" in got["fails"]["微博"]
    assert "抖音那边的内容" in got["text"]


def test_抖音撞验证码要如实报_不许当成没内容(monkeypatch):
    """★ 2026-10-25 实测:`/search/<词>` 有时返回「验证码中间页」(HTTP 200、几乎空白),
    那时 `rows=0`,与"这个词没人发"完全同形 —— 必须把验证码这层说出来。"""
    from app.services import douyin_cdp_source, weibo_search

    monkeypatch.setattr(weibo_search, "search", lambda *a, **k: [
        {"name": "甲", "snippet": "微博素材"}])

    def _captcha(*a, **k):
        raise douyin_cdp_source.DouyinCdpError("抖音返回的是**「验证码中间页」**")

    monkeypatch.setattr(douyin_cdp_source, "search", _captcha)
    got = gm.gather("某热点", session=_Db(), use_baidu=False)
    assert "验证码" in got["fails"]["抖音"]
    assert got["text"].strip(), "别因为抖音失败就把微博素材也丢了"


def test_两个平台都空就返回空素材_调用方据此拒绝生成(monkeypatch):
    from app.services import douyin_cdp_source, weibo_search

    monkeypatch.setattr(weibo_search, "search", lambda *a, **k: [])
    monkeypatch.setattr(douyin_cdp_source, "search", lambda *a, **k: [])
    got = gm.gather("某热点", session=_Db(), use_baidu=False)
    assert got["text"] == ""


def test_百度资讯也要带标签_且排在微博前面(monkeypatch):
    """报道类素材放最前 —— 模型写「事件概览」时应先看到报道,而不是网友发言。"""
    from app.services import douyin_cdp_source, weibo_search

    monkeypatch.setattr(gm, "_baidu_news",
                        lambda title, limit: (["【百度资讯】某媒体报道正文"], ""))
    monkeypatch.setattr(weibo_search, "search", lambda *a, **k: [
        {"name": "甲", "snippet": "微博网友说法"}])
    monkeypatch.setattr(douyin_cdp_source, "search", lambda *a, **k: [])
    got = gm.gather("某热点", session=_Db())
    assert got["counts"]["百度资讯"] == 1 and got["counts"]["微博"] == 1
    first = [ln for ln in got["text"].splitlines() if ln.strip()][0]
    assert first.startswith("【百度资讯】"), got["text"][:120]


def test_百度抓不到要记进_fails(monkeypatch):
    from app.services import douyin_cdp_source, weibo_search

    monkeypatch.setattr(gm, "_baidu_news", lambda title, limit: ([], "页面里没提取到可用正文"))
    monkeypatch.setattr(weibo_search, "search", lambda *a, **k: [
        {"name": "甲", "snippet": "微博素材"}])
    monkeypatch.setattr(douyin_cdp_source, "search", lambda *a, **k: [])
    got = gm.gather("某热点", session=_Db())
    assert "百度资讯" in got["fails"] and got["text"].strip()


def test_空标题直接拒(monkeypatch):
    got = gm.gather("   ", session=_Db(), use_baidu=False)
    assert got["text"] == "" and "标题为空" in got["fails"]["输入"]


def test_素材有上限():
    assert gm.MAX_CHARS <= 12000, "要跟 compose_gossip_doc 的截断保持一致,否则白抓"
