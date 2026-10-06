"""夸克**删除**能力单测(2026-10-07)。

⚠️ 这是**破坏性能力**,所以单测里**一个真请求都不发**(全打桩)。
它的安全验证另有一份:`scripts/quark_delete_probe.py` —— **建一个临时文件夹再删它**,
绝不拿真资源共享当小白鼠(已实测通过)。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest  # noqa: E402

from app.services.quark_transfer import (  # noqa: E402
    QuarkAuthError, QuarkError, QuarkTransfer,
)


@pytest.fixture
def qt(monkeypatch):
    c = QuarkTransfer("ck=x")
    calls: list[dict] = []

    def _req(method, path, *, api=None, params=None, json=None, timeout=None):
        calls.append({"method": method, "path": path, "api": api, "json": json})
        return {"code": 0, "data": {"task_id": "t1"}}
    monkeypatch.setattr(c, "_request", _req)
    c._calls = calls
    return c


class TestDeleteFiles:
    def test_默认进回收站而不是彻底删(self, qt) -> None:
        """★ **默认必须是可恢复的那条路**(action_type=2 = 回收站)。

        删除不可逆这件事上,默认值比实现更要紧:调用方忘了传参时,
        落到"能捞回来"那一档才是安全的。
        """
        out = qt.delete_files(["fid1"])
        assert out["ok"] is True and out["count"] == 1
        body = qt._calls[0]["json"]
        assert body["filelist"] == ["fid1"]
        assert body["action_type"] == 2, f"默认应进回收站(2),实际 {body['action_type']}"
        assert qt._calls[0]["path"] == "/1/clouddrive/file/delete"

    def test_显式要彻底删才用1(self, qt) -> None:
        qt.delete_files(["fid1"], to_recycle=False)
        assert qt._calls[0]["json"]["action_type"] == 1

    def test_空列表不发请求(self, qt) -> None:
        """⚠️ 反向:**空 filelist 绝不能发出去** —— 那正是"手滑删错"最容易发生的形态。"""
        out = qt.delete_files([])
        assert out["ok"] is False and qt._calls == []

    def test_cookie失效返回失败而不是抛(self, qt, monkeypatch) -> None:
        """删不动时**如实返回失败**,别让调用方以为删掉了。"""
        def _boom(*a, **k):
            raise QuarkAuthError("夸克 Cookie 已失效")
        monkeypatch.setattr(qt, "_request", _boom)
        out = qt.delete_files(["fid1"])
        assert out["ok"] is False and "失效" in out["message"]

    def test_接口报错也不抛(self, qt, monkeypatch) -> None:
        def _boom(*a, **k):
            raise QuarkError("夸克接口失败(123)")
        monkeypatch.setattr(qt, "_request", _boom)
        assert qt.delete_files(["fid1"])["ok"] is False
