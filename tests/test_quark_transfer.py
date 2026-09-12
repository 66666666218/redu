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


class TestEnsureDirFallback:
    """幽灵 23008 场景(线上案例:目录不存在却报同名,旧任务残留)的候选名梯子。"""

    def _mk_qt(self, tmp_path):
        return QuarkTransfer("cookie=test", fid_store=str(tmp_path / "fids.json"))

    def test_first_candidate_wins(self, tmp_path):
        qt = self._mk_qt(tmp_path)
        calls = []
        qt._mk_dir = lambda p, n: calls.append(n) or "fidA"
        assert qt._ensure_dir("/redian监听") == "fidA"
        assert calls == ["redian监听"]  # 正常路径只建一次,零扫描

    def test_ghost_conflict_falls_to_date_suffix(self, tmp_path):
        from datetime import datetime
        qt = self._mk_qt(tmp_path)
        calls = []
        suffix = f"{datetime.now():%m%d}"

        def fake_mk(parent, name):
            calls.append(name)
            if name == "redian监听":
                raise QuarkError("夸克接口失败(23008): file is doloading[同名冲突]")
            return "fidB"

        qt._mk_dir = fake_mk
        assert qt._ensure_dir("/redian监听") == "fidB"
        assert calls == ["redian监听", f"redian监听_{suffix}"]

    def test_all_candidates_conflict_raises(self, tmp_path):
        qt = self._mk_qt(tmp_path)
        qt._mk_dir = lambda p, n: (_ for _ in ()).throw(QuarkError("23008 同名冲突"))
        with pytest.raises(QuarkError, match="含后缀候选均撞名"):
            qt._ensure_dir("/redian监听")

    def test_fid_store_persists_across_instances(self, tmp_path):
        """幽灵场景跨运行复用:第二轮直接用持久化 fid,不再撞名新堆目录。"""
        store = tmp_path / "fids.json"
        qt = QuarkTransfer("cookie=test", fid_store=str(store))
        qt._mk_dir = lambda p, n: "fidC"
        assert qt._ensure_dir("/redian监听") == "fidC"
        assert store.exists()
        qt2 = QuarkTransfer("cookie=test", fid_store=str(store))
        qt2._mk_dir = lambda p, n: (_ for _ in ()).throw(AssertionError("复用时不应再创建"))
        assert qt2._ensure_dir("/redian监听") == "fidC"
        assert "redian监听" in qt2._used_store  # 标记来自缓存,保存失败可自愈

    def test_invalidate_clears_store_and_recreates(self, tmp_path):
        store = tmp_path / "fids.json"
        qt = QuarkTransfer("cookie=test", fid_store=str(store))
        qt._mk_dir = lambda p, n: "fidD"
        qt._ensure_dir("/redian监听")
        qt.invalidate_dir("/redian监听")
        qt2 = QuarkTransfer("cookie=test", fid_store=str(store))
        calls = []
        qt2._mk_dir = lambda p, n: calls.append(n) or "fidE"
        assert qt2._ensure_dir("/redian监听") == "fidE"
        assert calls  # 失效后重新走了创建
