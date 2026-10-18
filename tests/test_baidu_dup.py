"""百度盘**跨目录/同目录重复**识别(2026-10-16)。

**为什么单独立文件**:三盘里只有百度这半边没接自动清理 —— 因为**百度没有回收站**,
所以用户拍板「先做识别 + 清单,默认不删」。既然是"只报告",判据错了**不会毁数据**,
但会**毁信任**(一份离谱的清单没人看第二眼),所以判据全在这儿钉住。

实测背景(2026-10-16,`/来自资源批量转存` 里 9950 个文件 / 1086 GiB):
    137号案件.apk / 137号案件_1011213845.apk / 137号案件_1011214401.apk … **同名同大小** ×4
    —— 百度重复保存时**在同一个目录里**给文件加 `_<数字>` 尾巴。
⚠️ 第一版照搬夸克那条"必须**跨目录**"的判据 ⇒ 扫了 9950 个文件报「重复 0 组」。
   这组测试里的 `test_同目录里的重复也要认得出来` 就是那次订正的守卫。
"""
import os
import tempfile
from pathlib import Path

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

from app.services import baidu_dup as bd  # noqa: E402


class TestBase:
    def test_剥掉括号序号(self) -> None:
        assert bd._base("资源(1).zip") == "资源.zip"

    def test_剥掉下划线长数字_百度形态(self) -> None:
        assert bd._base("137号案件_1011213845.apk") == "137号案件.apk"

    def test_短数字后缀不敢剥(self) -> None:
        """⚠️ `_123` 这种短尾巴**可能是文件名本身**(年头/编号)⇒ 不剥,
        宁可少判一组,也不要把两个真文件说成重复。"""
        assert bd._base("报告_123.pdf") == "报告_123.pdf"


class TestClassify:
    def _f(self, name, size, d="/a"):
        return {"path": f"{d}/{name}", "name": name, "size": size, "dir": d}

    def test_同目录里的重复也要认得出来(self) -> None:
        """★★ **那次订正的正身**:百度的重复就长在**同一个目录**里,
        要求"跨目录"会一条都判不出来。"""
        files = [self._f("137号案件.apk", 95433288),
                 self._f("137号案件_1011213845.apk", 95433288),
                 self._f("137号案件_1011214401.apk", 95433288)]
        p = bd.classify(files)
        assert p["n_groups"] == 1 and p["n_dup"] == 2
        assert p["groups"][0]["keep"].endswith("137号案件.apk"), "要留**没被加尾巴**的那份"

    def test_同名但大小不同不算重复(self) -> None:
        """⚠️ 只同名不够 —— 同名不同大小是**两份不同的东西**。"""
        p = bd.classify([self._f("资源.zip", 5 << 20), self._f("资源.zip", 7 << 20)])
        assert p["n_groups"] == 0 and p["n_dup"] == 0

    def test_太小不参与判重(self) -> None:
        p = bd.classify([self._f("小.txt", 1000), self._f("小_111111.txt", 1000)])
        assert p["n_dup"] == 0

    def test_宣传简介要排除(self) -> None:
        """⚠️ 简介被设计成"塞进每个资源目录",天然同名同大小 ——
        不排除的话它会是清单里最大的一坨,而删它等于砍功能。"""
        files = [self._f("简介.doc", 3 << 20, "/a"), self._f("简介.doc", 3 << 20, "/b")]
        assert bd.classify(files)["n_dup"] == 1            # 不排除 → 判成重复
        p = bd.classify(files, intro={"简介.doc"})
        assert p["n_dup"] == 0 and p["skipped_intro"] == 2

    def test_只有一份就不是重复(self) -> None:
        assert bd.classify([self._f("独一份.zip", 9 << 20)])["n_dup"] == 0


class TestWalk:
    class _C:
        """假客户端:按目录返回预置条目;`boom` 里的目录**抛错**。"""

        def __init__(self, tree, boom=(), big=()):
            self.tree, self.boom, self.big, self.calls = tree, set(boom), set(big), []

        def list_dir(self, path):
            self.calls.append(path)
            if path in self.boom:
                raise RuntimeError("errno=-9 目录不存在")
            if path in self.big:
                return [{"server_filename": f"f{i}", "size": 1, "isdir": False}
                        for i in range(bd._LIST_DIR_CAP)]
            return self.tree.get(path, [])

    def test_列不动的目录不许当成空的(self) -> None:
        """★★⚠️ **这条是那类静默失败的守卫**(我第一版就栽了):默认根在盘里
        **不存在**时 `list_dir` 抛 `errno=-9`,而我把异常吞了 ⇒ 报告写着
        「重复 0 组(扫 0 个文件)」—— 一个目录都没列成,却像"扫完了、很干净"。"""
        c = self._C({}, boom={"/"})
        got = bd.walk(c, "/")
        assert got["files"] == []
        assert got["errors"], "列不动**必须留痕**,否则就是静默零"
        assert "RuntimeError" in got["errors"][0]

    def test_撞盘商翻页上限要算没列完(self) -> None:
        """⚠️ `BaiduPanClient.list_dir` 内部**翻页到 10000 就停** ⇒ 拿到正好 10000 条
        意味着**这个目录还没列完**(实测生产那个目录就是这样)⇒ 不许当"扫完了"。"""
        got = bd.walk(self._C({}, big={"/"}), "/")
        assert got["truncated"] is True, "拿到正好 10000 条 = 它没列完"

    def test_摘要要把截断与列不动都说出来(self) -> None:
        s = bd.summary({"n_groups": 1, "n_dup": 2, "bytes": 0, "scanned": 3,
                        "dirs": 1, "truncated": True, "n_errors": 2, "errors": ["/x: E"]})
        assert "没列完" in s and "没列成" in s


class Test百度删除开关:
    """★★ 用户口径「**先做识别 + 清单**」⇒ 开关**默认关**;开了才删,且只删重复的那几份。

    ⚠️⚠️ **百度没有回收站,删了不可逆** —— 所以这条链的每一道边界都要有测试盯着:
    ① 默认不删;② 只删重复副本(**`keep` 那份永不删**);③ **共用夸克/迅雷那个硬上限**;
    ④ 只收**绝对路径**;⑤ 删失败要**如实报**而不是静默。
    """

    class _S:
        pan_intro_baidu_dir = ""
        pan_auto_clean_baidu_root = ""
        #: ⚠️ **必须指向临时目录**(2026-10-19):`report_baidu` 原来写死
        #: `data/pan_dedupe_baidu.json`,而本文件调的是**真实函数** ⇒ 跑一次测试就把
        #: 生产清单覆盖成 `dup0.apk`(10-18 23:40 实测发生)。清单是决定"要不要开百度删除"
        #: 的唯一依据,污染它比没有清单更坏。
        pan_auto_clean_baidu_report_path = os.path.join(
            tempfile.gettempdir(), "redian_test_pan_dedupe_baidu.json")

    def _dup(self, n_extras: int = 3):
        return {"n_groups": 1, "n_dup": n_extras, "bytes": n_extras * 100,
                "dup_files": [{"path": f"/来自资源批量转存/dup{i}.apk", "name": f"dup{i}.apk",
                               "size": 100, "dir": "/来自资源批量转存", "keep": "/来自资源批量转存/x.apk",
                               "why": "重复"} for i in range(n_extras)],
                "scanned": 9, "dirs": 2, "truncated": False, "errors": [],
                "n_errors": 0, "intro_protected": 0, "groups": []}

    def _patch(self, monkeypatch, dup, called: list):
        from app.services import baidu_dup as bd
        from app.services import pan_auto_clean as pac
        from app.services import pan_accounts as pa
        from app.services import pan_promo_sweep as pps

        monkeypatch.setattr(pa, "cookie_for", lambda *a, **k: "ck")
        monkeypatch.setattr(pps, "build_plan", lambda *a, **k: {"delete": [], "review": []})
        monkeypatch.setattr(pps, "BaiduAdapter", lambda *a, **k: object())
        monkeypatch.setattr(bd, "build_plan", lambda *a, **k: dup)
        monkeypatch.setattr(
            "app.services.baidupan_transfer.BaiduPanClient",
            lambda ck: type("C", (), {
                "delete_paths": lambda self, paths, **k: (called.append(list(paths)),
                                                          len(paths))[1]})())
        return pac

    def test_默认关_只报告绝不删(self, monkeypatch) -> None:
        called: list = []
        pac = self._patch(monkeypatch, self._dup(), called)
        out = pac.report_baidu(object(), 1, self._S(), left=10)
        assert called == [], "**默认关**就一个字节都不许删"
        assert out["dup_files"] == 3 and out["state"] == "report_only"

    def test_报告只写临时路径_绝不碰生产清单(self, monkeypatch) -> None:
        """★ 回归守卫:跑测试不许覆盖 `data/pan_dedupe_baidu.json`。

        2026-10-18 23:40 实测发生:一次测试把生产清单写成了 `dup0.apk / 100 字节`
        的造数据。清单是人工决定「要不要开百度删除」的唯一依据。
        """
        real = Path("data/pan_dedupe_baidu.json")
        before = real.stat().st_mtime_ns if real.exists() else None
        pac = self._patch(monkeypatch, self._dup(), [])
        out = pac.report_baidu(object(), 1, self._S(), left=10)
        after = real.stat().st_mtime_ns if real.exists() else None
        assert before == after, "测试把生产百度清单覆盖了"
        assert Path(out["dup_list"]).parent == Path(tempfile.gettempdir()), out["dup_list"]

    def test_开了才删_且只删重复副本并共用上限(self, monkeypatch) -> None:
        class _S2(self._S):
            pan_auto_clean_baidu_delete = True

        called: list = []
        pac = self._patch(monkeypatch, self._dup(3), called)
        out = pac.report_baidu(object(), 1, _S2(), left=2)
        assert len(called[0]) == 2, f"上限 2 ⇒ 只删 2 份:{called}"
        assert out["baidu_deleted"] == 2 and out["baidu_freed"] == 200
        assert out["baidu_blocked_by_cap"] == 1, "撞上限的那份要留下(下轮再来)"
        #: ⚠️ **`keep` 那份永不在删除清单里** —— 它是我们在用的那份
        assert not any("x.apk" in p for p in called[0]), f"把自己留的那份也删了:{called[0]}"

    def test_删失败要如实报(self, monkeypatch) -> None:
        class _S2(self._S):
            pan_auto_clean_baidu_delete = True

        called: list = []
        pac = self._patch(monkeypatch, self._dup(1), called)

        def _boom(self, paths, **k):
            raise RuntimeError("errno=-9 文件不存在")
        monkeypatch.setattr("app.services.baidupan_transfer.BaiduPanClient",
                            lambda ck: type("C", (), {"delete_paths": _boom})())
        out = pac.report_baidu(object(), 1, _S2(), left=5)
        assert "baidu_delete_error" in out and "errno" in out["baidu_delete_error"]
        assert out.get("baidu_deleted") is None, "没删成就不许报删了"
