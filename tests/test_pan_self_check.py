"""网盘链路自检的判据(2026-10-19,用户口径「增加一道自检功能」)。

这里钉住的是**"空转与污染要被看见"**:
  · 作业超时 / 最近一轮有空转标记 → 必须报警;
  · 百度清单被测试数据覆盖 → 必须报警(10-18 的真实事故);
  · 配置还是 dry-run / 预算偏小 → 必须提示,别让"设了但没生效"装成正常。
"""
import os
import time
from pathlib import Path

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

from app.services import pan_self_check as psc  # noqa: E402


class _S:
    pan_auto_clean_enabled = True
    pan_auto_clean_dry_run = False
    pan_auto_clean_max_deletes = 300
    xunlei_dedupe_budget = 4000
    pan_self_check_reuse_days = 7
    pan_auto_clean_baidu_report_path = ""


class _Run:
    def __init__(self, status="success", detail="", started_at=None):
        from datetime import datetime

        self.status = status
        self.detail = detail
        self.started_at = started_at or datetime.now()


class TestBaiduReport:
    def test_缺清单要报警(self, tmp_path: Path) -> None:
        out = psc._check_baidu_report(tmp_path / "nope.json")
        assert out["level"] == "warn" and "不存在" in out["detail"]

    def test_测试造的数据要认出来(self, tmp_path: Path) -> None:
        """★ 10-18 的真实事故:`dup0.apk / 100 字节` 把生产清单覆盖了。"""
        p = tmp_path / "report.json"
        p.write_text(
            '{"root": "/", "n_groups": 1, "n_dup": 1, "bytes": 100,'
            ' "dup_files": [{"path": "/a/dup0.apk", "size": 100}]}',
            encoding="utf-8")
        out = psc._check_baidu_report(p)
        assert out["level"] == "warn" and "疑似测试数据" in out["detail"]

    def test_正常清单要放过(self, tmp_path: Path) -> None:
        p = tmp_path / "report.json"
        p.write_text(
            '{"root": "/来自资源批量转存", "n_groups": 287, "n_dup": 1548,'
            ' "bytes": 142000000000, "dup_files": [{"path": "/a/老舅.apk", "size": 99999}]}',
            encoding="utf-8")
        out = psc._check_baidu_report(p)
        assert out["level"] == "ok" and "287" in out["detail"]

    def test_过期清单要报警(self, tmp_path: Path) -> None:
        p = tmp_path / "report.json"
        p.write_text('{"root": "/x", "n_groups": 0, "n_dup": 0, "bytes": 0,'
                     ' "dup_files": []}', encoding="utf-8")
        old = time.time() - 72 * 3600
        os.utime(p, (old, old))
        out = psc._check_baidu_report(p)
        assert out["level"] == "warn" and "未更新" in out["detail"]


class TestRunDetail:
    def test_空转标记要报警(self, monkeypatch) -> None:
        monkeypatch.setattr(psc, "_last_run", lambda *a, **k: _Run(
            detail="夸克跳过(没扫完:已扫19个家,还差9个;计划3条不可信)"))
        out = psc._check_run_detail(None, 1, "pan_auto_clean")
        assert out["level"] == "warn" and "没扫完" in out["detail"]

    def test_撞预算要报警(self, monkeypatch) -> None:
        monkeypatch.setattr(psc, "_last_run", lambda *a, **k: _Run(
            status="failed", detail="扫2581目录 800次调用 —— 撞 API 预算,本轮不删"))
        out = psc._check_run_detail(None, 1, "xunlei_dedupe")
        assert out["level"] == "error" and "撞 API 预算" in out["detail"]

    def test_正常一轮不报警(self, monkeypatch) -> None:
        monkeypatch.setattr(psc, "_last_run", lambda *a, **k: _Run(
            detail="上限300 | 夸克 扫19 计划264 删264 省50.00GiB | 迅雷 计划4 删4"))
        out = psc._check_run_detail(None, 1, "pan_auto_clean")
        assert out["level"] == "ok"


class TestJobAge:
    def test_从未运行是错误(self, monkeypatch) -> None:
        monkeypatch.setattr(psc, "_last_run", lambda *a, **k: None)
        monkeypatch.setattr(psc, "_last_heartbeat", lambda *a, **k: None)
        out = psc._check_job_age(None, 1, "xunlei_dedupe", 8 * 24)
        assert out["level"] == "error" and "从未运行" in out["detail"]

    def test_只写心跳的作业不算从未运行(self, monkeypatch) -> None:
        """★ `xunlei_cleanup` 不写 `runs`,只写 `job_heartbeats` —— 别误报成"没跑过"。"""
        from datetime import datetime

        class _HB:
            last_run_at = datetime.now()
            run_count = 14
            error_count = 0
            last_error = ""

        monkeypatch.setattr(psc, "_last_run", lambda *a, **k: None)
        monkeypatch.setattr(psc, "_last_heartbeat", lambda *a, **k: _HB())
        out = psc._check_job_age(None, 1, "xunlei_cleanup", 26)
        assert out["level"] == "ok" and "心跳" in out["detail"]

    def test_超过阈值是错误(self, monkeypatch) -> None:
        from datetime import datetime, timedelta

        monkeypatch.setattr(psc, "_last_run", lambda *a, **k: _Run(
            started_at=datetime.now() - timedelta(hours=30)))
        out = psc._check_job_age(None, 1, "pan_auto_clean", 26)
        assert out["level"] == "error"


class TestConfig:
    def test_dry_run要提示(self) -> None:
        class _D(_S):
            pan_auto_clean_dry_run = True

        out = psc._check_config(_D())
        assert out["level"] == "warn" and "不会真删" in out["detail"]

    def test_预算偏小要提示(self) -> None:
        class _B(_S):
            xunlei_dedupe_budget = 800

        out = psc._check_config(_B())
        assert out["level"] == "warn" and "预算" in out["detail"]

    def test_配置齐全就正常(self) -> None:
        out = psc._check_config(_S())
        assert out["level"] == "ok" and "300" in out["detail"]


class TestRunChecks:
    def test_有_error时结论是error(self, monkeypatch) -> None:
        """`status` 必须能被上层区分:error 写 `runs=failed`,warn 只推告警。"""
        monkeypatch.setattr(psc, "_check_job_age", lambda *a, **k: psc._item("a", "ok", ""))
        monkeypatch.setattr(psc, "_check_run_detail", lambda *a, **k: psc._item("b", "warn", ""))
        monkeypatch.setattr(psc, "_check_quota", lambda *a, **k: psc._item("c", "error", ""))
        monkeypatch.setattr(psc, "_check_reuse", lambda *a, **k: psc._item("d", "ok", ""))
        monkeypatch.setattr(psc, "_check_baidu_report", lambda *a, **k: psc._item("e", "ok", ""))
        monkeypatch.setattr(psc, "_check_config", lambda *a, **k: psc._item("f", "ok", ""))
        out = psc.run_checks(None, 1, _S())
        # `_check_run_detail` 对 3 个作业各调一次(3 个 warn)+ 1 个 error = 4 项
        assert out["status"] == "error" and len(out["warnings"]) == 4


class TestQuotaPool:
    """盘容量判据:**看池里最空的号**,不是全池最满的那个(2026-10-21)。"""

    def _patch(self, monkeypatch, by_platform: dict) -> None:
        from app.services import pan_accounts as pa

        def fake_accounts(db, platform, *, user_id=1):
            return by_platform.get(platform, [])

        monkeypatch.setattr(pa, "accounts", fake_accounts)
        monkeypatch.setattr(psc, "_probe_ratio", lambda *a, **k: None)

    def test_有空号时不该报盘满(self, monkeypatch) -> None:
        from app.services.pan_accounts import AccountRef

        self._patch(monkeypatch, {"xunlei": [
            AccountRef("xunlei#2", "xunlei", "xunlei#2", last_ratio=102.5343),
            AccountRef("xunlei#3", "xunlei", "xunlei#3", last_ratio=0.097),
            AccountRef("xunlei#4", "xunlei", "xunlei#4", last_ratio=0.048),
        ]})
        out = psc._check_quota(None, 1)
        assert out["level"] == "ok"
        assert "xunlei#4" in out["detail"] and "停摆" not in out["detail"]
        #: 免费小号仍要留痕,但它只是附注、不是告警
        assert "xunlei#2" in out["detail"] and "不可用" in out["detail"]

    def test_全满才报错(self, monkeypatch) -> None:
        from app.services.pan_accounts import AccountRef

        self._patch(monkeypatch, {"baidupan": [
            AccountRef("百度2", "baidupan", "baidupan", last_ratio=0.9999),
            AccountRef("百度3", "baidupan", "baidupan#2", last_ratio=0.995),
        ]})
        out = psc._check_quota(None, 1)
        assert out["level"] == "error" and "所有号" in out["detail"]

    def test_全在95以上但没到99算警告(self, monkeypatch) -> None:
        from app.services.pan_accounts import AccountRef

        self._patch(monkeypatch, {"baidupan": [
            AccountRef("百度2", "baidupan", "baidupan", last_ratio=0.999),
            AccountRef("百度3", "baidupan", "baidupan#2", last_ratio=0.97),
        ]})
        out = psc._check_quota(None, 1)
        assert out["level"] == "warn" and "所有号" in out["detail"]
