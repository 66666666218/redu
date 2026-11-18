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


class TestQuotaCollapsed:
    """★★ 2026-11-18:整池 >100% 时**不许给「清理」这个动作**。

    实测四个迅雷号的 `limit` **逐字节相同**(`10737418240` = 10 GiB 免费档兜底值),
    而用量 121 GiB ~ **16.03 TiB** ⇒ ratio 12.17 / **1640.99** / 102.50 / 75.56。
    真实免费档装不下 16 TiB ⇒ 这个 limit 不是真配额(主号 `upgrade_type` 当时已空)。
    「清一下就好」在这种情况下是**清多少都不会恢复**的错动作。
    """

    def _patch(self, monkeypatch, refs, limits: dict[str, int]) -> None:
        from app.services import pan_accounts as pa
        from app.services import xunlei_transfer as xt

        monkeypatch.setattr(pa, "accounts",
                            lambda db, platform, *, user_id=1:
                            refs if platform == "xunlei" else [])
        monkeypatch.setattr(psc, "_probe_ratio", lambda *a, **k: None)
        monkeypatch.setattr(xt, "_credentials", lambda settings=None, *, cookie_key="xunlei": {
            "access_token": "a", "_cookie_key": cookie_key})
        monkeypatch.setattr(xt, "quota_info", lambda cred=None, **k: {
            "usage": 1, "limit": limits[str((cred or {}).get("_cookie_key"))], "ratio": 1.0})

    def _refs(self):
        from app.services.pan_accounts import AccountRef

        return [AccountRef("xunlei", "xunlei", "xunlei", last_ratio=1640.9934),
                AccountRef("迅雷/xunlei#2", "xunlei", "xunlei#2", last_ratio=12.1691),
                AccountRef("迅雷/xunlei#3", "xunlei", "xunlei#3", last_ratio=102.4951),
                AccountRef("迅雷/xunlei#4", "xunlei", "xunlei#4", last_ratio=75.5592)]

    def test_限额全都一样时报读数不可信_而不是叫人清理(self, monkeypatch) -> None:
        self._patch(monkeypatch, self._refs(),
                    {"xunlei": 10_737_418_240, "xunlei#2": 10_737_418_240,
                     "xunlei#3": 10_737_418_240, "xunlei#4": 10_737_418_240})
        out = psc._check_quota(None, 1)
        assert out["level"] == "warn", "读数不可信不是「盘满了」,不该是 error"
        assert "读数不可信" in out["detail"] and "配额" in out["detail"]
        #: ★ 这两句是**错动作** —— 清多少、加多少个号都不会恢复,一个字都不许出现
        assert "清理" not in out["detail"], f"给错动作了:{out['detail']}"
        assert "加号" not in out["detail"], f"给错动作了:{out['detail']}"

    def test_限额各不相同就不编证据_照旧报error(self, monkeypatch) -> None:
        """各号 limit 不同 ⇒ 没有"兜底值"的证据,不许猜;老老实实报盘满。"""
        self._patch(monkeypatch, self._refs(),
                    {"xunlei": 30_000_000_000_000, "xunlei#2": 10_737_418_240,
                     "xunlei#3": 30_000_000_000_000, "xunlei#4": 30_000_000_000_000})
        out = psc._check_quota(None, 1)
        assert out["level"] == "error" and "读数不可信" not in out["detail"]

    def test_只有一个号超额时仍按原判据(self, monkeypatch) -> None:
        """池里还有余量时,超额那个号只是附注 —— 不能把整盘说成"不可信"。"""
        from app.services.pan_accounts import AccountRef

        from app.services import pan_accounts as pa

        refs = [AccountRef("迅雷/xunlei#2", "xunlei", "xunlei#2", last_ratio=12.1691),
                AccountRef("迅雷/xunlei#4", "xunlei", "xunlei#4", last_ratio=0.70)]
        monkeypatch.setattr(pa, "accounts",
                            lambda db, platform, *, user_id=1:
                            refs if platform == "xunlei" else [])
        monkeypatch.setattr(psc, "_probe_ratio", lambda *a, **k: None)
        out = psc._check_quota(None, 1)
        assert out["level"] == "ok" and "读数不可信" not in out["detail"]


class TestNoWrongActionWhenQuotaIsBogus:
    """★★★ 源码级守卫:**整池超额时,面板与告警都不许给「清理」这个动作**。

    这一族在本仓栽过两次(2026-10-21 报 `xunlei#2 已用 10253%`,2026-11-18 报
    「所有号 ≥99%,转存停摆」),两次都是**给错动作** —— 而错动作比不给动作更坏:
    人会照着去清盘,清完发现毫无变化,下次就不信告警了。

    所以这里钉的不是"文案好看",而是**判据链上有没有那个分叉**:
    `limit` 是兜底值(`xt.limit_looks_like_fallback`)时必须走进「读数不可信」那一支。
    """

    def test_兜底limit的判据只认十倍以上超出(self) -> None:
        """⚠️ 不能只看 `ratio > 1` —— 126% 那种是**真超额**,清理确实有用。"""
        from app.services import xunlei_transfer as xt

        free_tier = 10 * 1024 ** 3
        assert xt.limit_looks_like_fallback(
            {"limit": free_tier, "usage": 17_620_032_999_871, "ratio": 1640.99}) is True
        #: 真超额(38.08TB / 30.10TB = 126%)⇒ 不是兜底值,照旧说"盘满"
        assert xt.limit_looks_like_fallback(
            {"limit": 33_092_723_015_680, "usage": 40_901_000_000_000, "ratio": 1.26}) is False
        #: limit 是 10 GiB 但用量也没超它 ⇒ 那就是个真的小号,不编"读数不可信"
        assert xt.limit_looks_like_fallback(
            {"limit": free_tier, "usage": free_tier // 2, "ratio": 0.5}) is False
        assert xt.limit_looks_like_fallback({}) is False
        assert xt.limit_looks_like_fallback(None) is False

    def test_面板接口带上limit_suspect且不给清理建议(self) -> None:
        """`/api/xunlei/quota` 是「迅雷群组」页显示盘况的那个口子 —— 它也不能教人清盘。"""
        import inspect

        from app.api import xunlei as api

        src = inspect.getsource(api.xunlei_quota)
        assert "limit_suspect" in src, "面板必须能区分「真满」与「读数不可信」"
        assert "limit_looks_like_fallback" in src, "判据要复用同一个,别在路由里再写一份"
        assert "清盘不会恢复" in src, "要说清这个动作是错的,否则人会照着去清"

    def test_自检_整池超额时不给清理建议(self, monkeypatch) -> None:
        """与 `TestQuotaCollapsed` 同一判据,这里守的是**真库会走到的那条分支**。"""
        from app.services import pan_accounts as pa

        refs = [pa.AccountRef(f"迅雷/xunlei#{i}", "xunlei", f"xunlei#{i}",
                              last_ratio=r) for i, r in ((2, 12.17), (3, 102.5), (4, 75.6))]
        monkeypatch.setattr(pa, "accounts",
                            lambda db, platform, *, user_id=1: refs if platform == "xunlei" else [])
        monkeypatch.setattr(psc, "_probe_ratio", lambda *a, **k: None)
        monkeypatch.setattr(psc, "_quota_limit_note", lambda platform, refs: "读数不可信,")
        out = psc._check_quota(None, 1)
        assert "清盘不会恢复" in out["detail"] and "清理" not in out["detail"]


class TestProbeRatio:
    """★ 2026-11-11:夸克那一支**此前根本不存在**。

    后果实测:池里两个号的 `last_ratio` 恒为 `None` ⇒ `pan_accounts.ordered()` 的
    `usable_hint` 都退化成 0.0 ⇒ 按 label 排序 ⇒ **超容 123% 的 1 号永远排第一**,
    空着的 2 号永远轮不到(近 7 天 67 条「夸克容量不足」,积压 235→269)。
    """

    def test_夸克要能探到比例(self, monkeypatch) -> None:
        from app.services import pan_accounts, quark_transfer

        class _Q:
            def __init__(self, ck, **kw) -> None:
                assert ck == "QK"

            def quota_ratio(self):
                return 1.2327

        monkeypatch.setattr(quark_transfer, "QuarkTransfer", _Q)
        monkeypatch.setattr(pan_accounts, "cookie_of", lambda ref, db: "QK")
        ref = pan_accounts.AccountRef("夸克1", "quark", "quark")
        assert psc._probe_ratio(None, "quark", ref) == 1.2327

    def test_探不到时返回None而不是判满(self, monkeypatch) -> None:
        from app.services import pan_accounts, quark_transfer

        class _Q:
            def __init__(self, ck, **kw) -> None: ...
            def quota_ratio(self):
                raise RuntimeError("network")

        monkeypatch.setattr(quark_transfer, "QuarkTransfer", _Q)
        monkeypatch.setattr(pan_accounts, "cookie_of", lambda ref, db: "QK")
        ref = pan_accounts.AccountRef("夸克1", "quark", "quark")
        assert psc._probe_ratio(None, "quark", ref) is None

    def test_默认号不补探(self, monkeypatch) -> None:
        """没配池时那个隐式默认号探到了也没地方存 —— 白打一次接口。"""
        from app.services import pan_accounts
        assert psc._probe_ratio(
            None, "quark", pan_accounts.default_ref("quark")) is None
