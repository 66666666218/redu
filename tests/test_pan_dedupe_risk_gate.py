"""盘内去重的**风险闸**单测(2026-10-07)。

★ 守的是那条干跑救回来的规则:**「要删的那份比留的还大」→ 不许自动删**。

为什么:"留内容多的"是按**先条目数、再体积**判的,而**条目多 ≠ 内容全**。
实测「宝可梦朱紫」:留 36 项 / **2.55 GiB**,要删 24 项 / **27.76 GiB** ——
那 24 项几乎肯定是视频/镜像大文件,而这两份**可能是不同的东西**(本体 vs 本体+DLC),
**根本不是同一份存了两次**。自动删下去就是删掉一份真资源。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest  # noqa: E402

# ⚠️ 逻辑已从 `scripts/_pan_dedupe.py` 提到服务层(排定作业要跑它,而服务层不能
# 依赖 scripts/)。脚本现在只是薄壳。
from app.services import pan_dedupe as pd  # noqa: E402

MiB = 1024 * 1024


def _fake_tree(monkeypatch, keep_items: int, keep_mib: float,
               drop_items: int, drop_mib: float) -> None:
    """造一棵最小的树:一个父目录下并排「X」和「X(1)」。"""
    tree = {
        "": [_d("P", "父目录"), _f("K", "资源", keep_items, keep_mib * MiB)],
        "P": [_d("A", "资源"), _d("B", "资源(1)")],
        "A": [_f(f"a{i}", "f", 1, drop_mib * MiB / drop_items) for i in range(drop_items)],
        "B": [_f(f"b{i}", "f", 1, keep_mib * MiB / keep_items) for i in range(keep_items)],
    }
    monkeypatch.setattr(pd.xt, "list_all_files", lambda pid, **k: tree.get(pid, []))


def _d(fid: str, name: str) -> dict:
    return {"id": fid, "name": name, "kind": "drive#folder"}


def _f(fid: str, name: str, n: int, size_each: float) -> dict:
    return {"id": fid, "name": name, "kind": "drive#file", "size": int(size_each)}


class TestRiskyGate:
    def test_留的比删的小_要标成risky(self, monkeypatch) -> None:
        # 留 36 项 2.55 GiB;删 24 项 27.76 GiB(实测那组的形状)
        _fake_tree(monkeypatch, keep_items=36, keep_mib=2610, drop_items=24, drop_mib=28422)
        out = pd.build_plan(max_depth=3)
        assert out["plan"], "该认出这一组重复"
        g = out["plan"][0]
        assert g["risky"] is True, f"要删的比留的大 10 倍,必须标 risky:{g}"
        assert "倍" in g["risky_reason"]
        assert out["n_drop"] == 0, "risky 的组**不该**进自动删的计数"

    def test_留的比删的大_正常进自动删(self, monkeypatch) -> None:
        """反向:正常情形(留的更大)不该被误判成 risky,否则闸门把活全挡了。"""
        _fake_tree(monkeypatch, keep_items=30, keep_mib=5000, drop_items=10, drop_mib=500)
        out = pd.build_plan(max_depth=3)
        g = out["plan"][0]
        assert g["risky"] is False, g
        assert out["n_drop"] == 1

    def test_小差异不惊动人工(self, monkeypatch) -> None:
        """⚠️ 差值不够大(低于 RISKY_MIN_MIB)时**不算 risky** —— 小文件之间正常波动很常见。"""
        _fake_tree(monkeypatch, keep_items=10, keep_mib=50, drop_items=10, drop_mib=60)
        out = pd.build_plan(max_depth=3)
        assert out["plan"][0]["risky"] is False, "差值太小,不该每次都喊人工"
