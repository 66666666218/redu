"""quark_transfer 单测:分享链解析/价格提取/错误分型(全 mock,零网络)。"""
import pytest

from app.services.quark_transfer import (
    QuarkAuthError,
    QuarkError,
    QuarkTransfer,
    extract_quark_urls,
)


class TestExtractQuarkUrls:
    def test_extracts_single(self):
        assert extract_quark_urls("下载: https://pan.quark.cn/s/abc123") == [
            "https://pan.quark.cn/s/abc123"]

    def test_extracts_multiple_dedupes(self):
        text = "链接1 https://pan.quark.cn/s/aaa 链接2 https://pan.quark.cn/s/bbb 链接1重复 https://pan.quark.cn/s/aaa"
        assert extract_quark_urls(text) == [
            "https://pan.quark.cn/s/aaa", "https://pan.quark.cn/s/bbb"]

    def test_no_urls(self):
        assert extract_quark_urls("没有链接的文本") == []

    def test_ignores_non_quark(self):
        assert extract_quark_urls("https://pan.baidu.com/s/abc") == []


class TestQuarkTransferParsing:
    def test_parse_share_url(self):
        qt = QuarkTransfer("cookie=test")
        share_id, pwd = qt._parse_share("https://pan.quark.cn/s/abc123")
        assert share_id == "abc123" and pwd == ""

    def test_parse_share_with_password(self):
        qt = QuarkTransfer("cookie=test")
        share_id, pwd = qt._parse_share("https://pan.quark.cn/s/abc123 提取码:x9y8")
        assert share_id == "abc123" and pwd == "x9y8"

    def test_parse_invalid_raises(self):
        qt = QuarkTransfer("cookie=test")
        with pytest.raises(QuarkError):
            qt._parse_share("https://pan.baidu.com/s/abc")


class TestQuarkAuthError:
    def test_401_raises_auth_error(self, monkeypatch):
        qt = QuarkTransfer("cookie=test")

        class _Resp:
            status_code = 401
            text = '{"code": 401}'

            def json(self):
                return {"code": 401}

        monkeypatch.setattr("app.services.quark_transfer.requests.request",
                            lambda *a, **kw: _Resp())
        with pytest.raises(QuarkAuthError):
            qt._request("GET", "/test")

    def test_capacity_limit_message(self, monkeypatch):
        qt = QuarkTransfer("cookie=test")

        class _Resp:
            status_code = 200
            text = ""

            def json(self):
                return {"code": 31001, "message": "capacity limit exceeded"}

        monkeypatch.setattr("app.services.quark_transfer.requests.request",
                            lambda *a, **kw: _Resp())
        with pytest.raises(QuarkError, match="容量不足"):
            qt._request("GET", "/test")


class TestQuarkKeepalive:
    def test_keepalive_calls_list_dir(self, monkeypatch):
        qt = QuarkTransfer("cookie=test")
        called = {"list_dir": False}

        def fake_list_dir(fid):
            called["list_dir"] = True
            return []

        qt._list_dir = fake_list_dir
        assert qt.keepalive() is True
        assert called["list_dir"] is True


class TestDirCache:
    def test_dir_cache_reuses_fid(self, monkeypatch):
        """路径缓存:同一目录只解析一次,后续命中缓存。"""
        qt = QuarkTransfer("cookie=test")
        qt._dir_cache = {"/redian监听": "fid123"}
        assert qt._ensure_dir("/redian监听") == "fid123"

    def test_dir_cache_normalizes_trailing_slash(self):
        qt = QuarkTransfer("cookie=test")
        qt._dir_cache = {"redian监听": "fid123"}
        assert qt._ensure_dir("/redian监听/") == "fid123"
