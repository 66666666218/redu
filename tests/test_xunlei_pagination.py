"""迅雷列目录**翻页**单测(2026-10-07)。

## 为什么它是个大事
原来的 `list_files` **只发一页请求**,而迅雷响应里**明明带着 `next_page_token`** 却从没用过
⇒ **任何条目数超过一页(约 200 条)的目录,后面的内容我们永远看不到**。
实测代价:盘上真实占用 **24.17 TiB**,而本地索引表 `xunlei_resources` **只有 8 行**。

⇒ `list_all_files` 就是那个补丁:**跟着 token 走到底**。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest  # noqa: E402

from app.services import xunlei_transfer as xt  # noqa: E402


class _Resp:
    def __init__(self, payload, status=200):
        self._p, self.status_code = payload, status

    def json(self):
        return self._p


class TestListAllFiles:
    def _patch(self, monkeypatch, pages):
        """pages: [{"files":[...], "next_page_token":"t1"}, ...] 按 token 顺序发。"""
        monkeypatch.setattr(xt, "_credentials", lambda *a, **k: {"access_token": "x"})
        monkeypatch.setattr(xt, "_drive_headers", lambda c: {})
        monkeypatch.setattr(xt, "_fresh_cred", lambda c: c)
        monkeypatch.setattr(xt, "_with_captcha_retry", lambda fn: fn())
        seen: list[str] = []

        def _get(url, headers=None, timeout=None, params=None):
            seen.append(str((params or {}).get("page_token") or ""))
            i = len(seen) - 1
            return _Resp(pages[i] if i < len(pages) else {"files": [], "next_page_token": ""})
        monkeypatch.setattr(xt.requests, "get", _get)
        return seen

    def test_跟着token把所有页都拿到(self, monkeypatch) -> None:
        """★ 核心:三页要合成一份完整列表 —— 而不是只有第一页。"""
        seen = self._patch(monkeypatch, [
            {"files": [{"id": "1", "name": "a"}], "next_page_token": "T1"},
            {"files": [{"id": "2", "name": "b"}], "next_page_token": "T2"},
            {"files": [{"id": "3", "name": "c"}], "next_page_token": ""},
        ])
        out = xt.list_all_files("")
        assert [f["id"] for f in out] == ["1", "2", "3"], out
        assert seen == ["", "T1", "T2"], f"token 要一页页传下去,实际:{seen}"

    def test_没有token就停(self, monkeypatch) -> None:
        seen = self._patch(monkeypatch, [{"files": [{"id": "1", "name": "a"}],
                                          "next_page_token": ""}])
        assert len(xt.list_all_files("")) == 1
        assert len(seen) == 1, "没有下一页就不该再发请求"

    def test_空页也要停_别死循环(self, monkeypatch) -> None:
        """⚠️ **有 token 但 files 为空**时必须停 —— 否则就是无限翻页(实测把脚本挂死过)。"""
        seen = self._patch(monkeypatch, [{"files": [], "next_page_token": "T1"}])
        assert xt.list_all_files("") == []
        assert len(seen) == 1, f"空页还继续翻 = 死循环,实际发了 {len(seen)} 次"

    def test_撞翻页上限要留痕(self, monkeypatch, caplog) -> None:
        """⚠️ **绝不能让"没列完"和"列完了"长得一样** —— 撞上限必须 warning。"""
        import logging
        monkeypatch.setattr(xt, "_credentials", lambda *a, **k: {"access_token": "x"})
        monkeypatch.setattr(xt, "_drive_headers", lambda c: {})
        monkeypatch.setattr(xt, "_fresh_cred", lambda c: c)
        monkeypatch.setattr(xt, "_with_captcha_retry", lambda fn: fn())
        monkeypatch.setattr(xt.requests, "get", lambda *a, **k: _Resp(
            {"files": [{"id": "x", "name": "n"}], "next_page_token": "永远有下一页"}))
        with caplog.at_level(logging.WARNING):
            out = xt.list_all_files("", max_pages=3)
        assert len(out) == 3
        assert any("没列完" in r.message or "上限" in r.message for r in caplog.records), \
            "撞上限必须说出来,否则读的人会以为列完了"
