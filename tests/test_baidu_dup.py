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
