"""llm_client 单测:请求构造/响应解析/失败降级/成本追踪(全 mock,零成本)。"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest

from app.services import llm_client
from app.services.llm_client import narrate_articles, rank_candidates, rewrite_article

KEY = "sk-test"
BASE = "https://api.test.com"
MODEL = "test-model"


class _Resp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _ok_content(content="AI 回复"):
    return {"choices": [{"message": {"content": content}}], "usage": {"total_tokens": 100}}


@pytest.fixture(autouse=True)
def _reset_usage():
    llm_client._llm_usage.update(calls=0, failures=0, total_tokens=0)


def test_narrate_parses_and_tracks_usage(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return _Resp(200, _ok_content("AI 解读文本"))

    monkeypatch.setattr(llm_client.requests, "post", fake_post)
    out = narrate_articles(BASE, KEY, MODEL, [{"title": "文A", "summary": "摘要"}])
    assert out == "AI 解读文本"
    assert captured["url"] == BASE + "/chat/completions"
    assert captured["headers"]["Authorization"] == f"Bearer {KEY}"
    assert captured["json"]["model"] == MODEL
    assert "文A" in captured["json"]["messages"][1]["content"]
    snap = llm_client.llm_usage_snapshot()
    assert snap["calls"] == 1 and snap["total_tokens"] == 100 and snap["failures"] == 0


def test_narrate_failure_returns_none(monkeypatch):
    monkeypatch.setattr(llm_client.requests, "post",
                        lambda *a, **kw: _Resp(500, text="server error"))
    out = narrate_articles(BASE, KEY, MODEL, [{"title": "文A"}])
    assert out is None
    snap = llm_client.llm_usage_snapshot()
    assert snap["failures"] == 1


def test_narrate_empty_key_returns_none():
    assert narrate_articles("", KEY, MODEL, [{"title": "文A"}]) is None


def test_rank_candidates_parses_sequential_format(monkeypatch):
    llm_output = "1|资源号|高|专注网盘资源分享\n2|营销号|低|广告为主\n3|无关|低|情感类"
    monkeypatch.setattr(llm_client.requests, "post",
                        lambda *a, **kw: _Resp(200, _ok_content(llm_output)))
    cands = [{"name": "号A", "title": "T1"}, {"name": "号B", "title": "T2"}, {"name": "号C", "title": "T3"}]
    out = rank_candidates(BASE, KEY, MODEL, cands)
    assert out is not None and len(out) == 3
    assert out[0]["verdict"] == "资源号" and out[0]["priority"] == "高"
    assert out[1]["verdict"] == "营销号"


def test_rewrite_outputs_title_and_content(monkeypatch):
    llm_output = "新标题在这里\n\n这是改写后的正文第一段。\n\n第二段。"
    monkeypatch.setattr(llm_client.requests, "post",
                        lambda *a, **kw: _Resp(200, _ok_content(llm_output)))
    out = rewrite_article(BASE, KEY, MODEL, "原标题", "原正文" * 50, my_link="https://pan.quark.cn/s/xyz")
    assert out is not None
    assert out["title"] == "新标题在这里"
    assert "第一段" in out["content"]
    # my_link 被包含在 prompt 中
    # (LLM 自行决定保留)


def test_rewrite_short_content_returns_none():
    assert rewrite_article(BASE, KEY, MODEL, "T", "短", my_link="") is None


def test_usage_snapshot_reset(monkeypatch):
    monkeypatch.setattr(llm_client.requests, "post",
                        lambda *a, **kw: _Resp(200, _ok_content("x")))
    narrate_articles(BASE, KEY, MODEL, [{"title": "T"}])
    snap = llm_client.llm_usage_snapshot(reset=True)
    assert snap["total_tokens"] > 0
    snap2 = llm_client.llm_usage_snapshot()
    assert snap2["total_tokens"] == 0
