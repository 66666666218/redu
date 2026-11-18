"""`app/services/link_audit.py` 的**对齐判据**(2026-10-16)。

⚠️ **判据主体在 service 里**(定时作业要调它),`scripts/link_audit.py` 只是命令行壳
—— 所以这里 from `app.services.link_audit` 取,别去 import 那个壳。

**为什么单独立文件**:这条判据当天被**实测校准过两次** ——
第一版只做「归一化后互含」,跑出来 `对齐 0/15`,而**肉眼一看大半是对的**
(真实世界里同一份资源会被加 emoji / 分隔符 / 年份 / 繁体):
    `《死刑将至》全集4K1080P高清…`      vs  `S_死丨刑丨将丨至丨2026丨率丨FLAC丨…`
    `1000套+优质PPT模版（免费下载/可修改）` vs  `2025年1000套优质PPT模板`
    `乔迁之喜【邀请函】高清电子版`        vs  `乔迁邀请函`
而**真正的错配**必须仍然判得出来(实测那条:库里的《三千年等一口将军气》那一行
**挂着一条分享内是「中医古籍」的链**)。

⚠️ 判错的代价**不对称**:把"对得上"判成"货不对版"只多一条待人工看的行;
反过来会让人**以为链是好的**。所以这些用例里,`test_真错配不许被放过` 那条最要紧。
"""
import os
import sys

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services import link_audit  # noqa: E402
from app.services.link_audit import _aligned, _first_url, _lcs, _platform, _verdict  # noqa: E402


class TestAligned:
    def test_名字被分隔符切开也算对得上(self) -> None:
        assert _aligned("《死刑将至》全集4K1080P高清", "S_死丨刑丨将丨至丨2026丨率丨FLAC")

    def test_多一个年份也算对得上(self) -> None:
        assert _aligned("1000套+优质PPT模版（免费下载/可修改）", "2025年1000套优质PPT模板")

    def test_短分享名整个出现在标题里也算对得上(self) -> None:
        """`乔迁之喜【邀请函】高清电子版` vs `乔迁邀请函` —— 反向字集那条认出来。"""
        assert _aligned("乔迁之喜【邀请函】高清电子版【免费保存】", "乔迁邀请函")

    def test_真错配不许被放过(self) -> None:
        """★★ **这条最要紧**(实测真实存在):库里那条「三千年等一口将军气」的记录
        挂着的链,分享内是**「中医古籍」** —— 逐字都对不上,必须判出来。"""
        assert not _aligned("《三千年等一口将军气》1-88一口气全看完了", "中医古籍")
        assert not _aligned("2026九九重阳节主题PPT模板精选35套", "二维码资源")

    def test_空串一律不对齐(self) -> None:
        assert not _aligned("", "中医古籍") and not _aligned("某资源", "")


class TestLcs:
    def test_基本(self) -> None:
        assert _lcs("abcde", "xxcdexx") == 3
        assert _lcs("", "abc") == 0


class TestVerdict:
    def _p(self, **kw):
        return {"alive": True, "names": ["某资源"], **kw}

    def test_打不开与判不对是两件事(self) -> None:
        """⚠️ **两道判据必须分开报** —— 修法完全不同(一个重做链、一个核对配对)。"""
        assert _verdict("某资源", {"alive": False, "err": "share_status=share_overdue"})[0] == "打不开"
        assert _verdict("不相干", self._p())[0] == "货不对版"

    def test_空分享单独一档(self) -> None:
        """链能打开但**里面没东西** ⇒ 点开是空的 —— 不许混进"对齐",也不许混进"打不开"。"""
        assert _verdict("某资源", {"alive": True, "names": []})[0] == "空分享"

    def test_判不了的要明说待人工看(self) -> None:
        assert _verdict("某资源", {"alive": None, "err": "疑似限流"})[0] == "待人工看"

    def test_货不对版要把两边名字都带出来(self) -> None:
        """判据是启发式的 ⇒ **每条非对齐都要能让人一眼复核**,不许只给个结论。"""
        v, why = _verdict("2026九九重阳节主题PPT模板", self._p(names=["二维码资源"]))
        assert v == "货不对版" and "二维码资源" in why and "库里" in why

    def test_顶层只有一个分类目录时不算货不对版(self) -> None:
        """★★ **2026-11-18 实测的判据盲区**。

        一个迅雷分享顶层只有单个叫 **`文字类`** 的文件夹(真实内容在下一层),
        而它被 **6 个不同标题**复用(抖音线索 4 + 公众号 2)⇒ 体检里报 6 条
        「货不对版」,占当轮 11 条里的一半多 —— **看起来像大面积错配,其实判不了**。

        `文字类` / `古装大剧` / `分类整理` 是**收纳目录**,不是资源名,
        拿它跟标题比注定对不上。判据只能承认「判不了」,不能硬判错配。
        """
        v, why = _verdict("近期超火《麦穗与羊》原稿", {
            "alive": True, "names": ["文字类"], "only_dir_name": "文字类"})
        assert v == "待人工看", f"单一目录 ≠ 货不对版:{v}"
        assert "文字类" in why, f"真实容器名要写出来:{why}"

    def test_多条目时仍然是货不对版(self) -> None:
        """反例:顶层有**多个**条目还全对不上 ⇒ 那**是**真的错配,别被上一条放宽掉。"""
        v, _ = _verdict("某资源", {"alive": True,
                                   "names": ["甲剧全集", "乙剧全集"], "only_dir_name": ""})
        assert v == "货不对版"

    def test_单一目录但目录名对不上_仍是待人工看且要点名不符(self) -> None:
        """★★ **别把真事故埋进「判不了」**(2026-11-18 同一天里两种都有):

        * `《麦穗与羊》→ 文字类`      ⇒ 容器名,判不出对错
        * `《阳光姐妹淘》→ 中医古籍`  ⇒ **真错配**(2026-10-16 就确认过的事故链)

        两者的形状**完全一样**(顶层单一目录),只靠形状分不开 ⇒ 文案必须把容器名
        与库里的标题**并排写出来**,让人一眼看出"这两个根本不是一个东西",
        而不是一句含糊的"需下探才能判"。
        """
        v, why = _verdict("《阳光姐妹淘》 百度云盘_百度网盘_高清 免费下载", {
            "alive": True, "names": ["中医古籍"], "only_dir_name": "中医古籍"})
        assert v == "待人工看", v
        assert "中医古籍" in why, f"真实容器名必须写出来:{why}"
        assert "不符" in why, f"要说清是**对不上**,不能只说判不了:{why}"
        assert "阳光姐妹淘" in why, f"库里标题也要带出来供并排复核:{why}"

    def test_容器名与标题对得上就算对齐(self) -> None:
        """反方向:标题说的就是这层目录名 ⇒ 那就是对齐,别浪费人一次复核。"""
        v, why = _verdict("某剧全集", {"alive": True,
                                       "names": ["某剧全集"], "only_dir_name": "某剧全集"})
        assert v == "对齐", (v, why)


class Test告警口径:
    """★★ **「待人工看」里有两种东西,只有一种该进告警**(2026-11-18 实测的漏报)。

    `_BAD` 刻意不含「待人工看」,因为那一档大部分是「我们自己被限流/超时」——
    算进去就是假红。但「顶层单一目录且目录名与标题不符」是**链本身**的可疑形状:
    实测 `《阳光姐妹淘》→ 中医古籍` 就是 2026-10-16 已确认的**真错配**。
    两者共用「待人工看」这个档名 ⇒ 必须靠 `_CONTAINER` 标记分家。
    """

    def _row(self, title: str, probe: dict) -> dict:
        v, why = _verdict(title, probe)
        return {"source": "抖音线索", "title": title, "verdict": v, "why": why}

    def test_容器名不符要进告警(self) -> None:
        r = self._row("《阳光姐妹淘》 百度云盘_高清 免费下载", {
            "alive": True, "names": ["中医古籍"], "only_dir_name": "中医古籍"})
        assert r["verdict"] == "待人工看"
        assert link_audit._needs_eyes(r), "真错配被当成'判不了'丢掉 ⇒ 谁也不会去看"

    def test_限流那种待人工看不许进告警(self) -> None:
        """反方向:`疑似限流` 进告警就是**假红**,会训练人忽略整份报告。"""
        r = self._row("某资源", {"alive": None, "err": "疑似限流/网络"})
        assert r["verdict"] == "待人工看" and not link_audit._needs_eyes(r)

    def test_确凿坏链照旧进告警(self) -> None:
        for p in ({"alive": False, "err": "share_status=share_overdue"},
                  {"alive": True, "names": []},
                  {"alive": True, "names": ["甲剧全集", "乙剧全集"], "only_dir_name": ""}):
            assert link_audit._needs_eyes(self._row("某资源", p))

    def test_汇总要把容器名单列(self) -> None:
        """混成一个「待人工看 N」时,容器名那几条会被限流那几条淹掉。"""
        rows = [self._row("某资源", {"alive": None, "err": "疑似限流/网络"}),
                self._row("某剧全集", {"alive": True, "names": ["某剧全集"],
                                       "only_dir_name": "某剧全集"})]
        rows[0]["verdict"] = "待人工看"
        rows.append(self._row("《阳光姐妹淘》", {
            "alive": True, "names": ["中医古籍"], "only_dir_name": "中医古籍"}))
        s = link_audit.summary(rows)
        assert "容器名待核1" in s and "待人工看1" in s, s

    def test_同一条链要多行只聚成一条(self) -> None:
        """★★ **实测的形状**:一个迅雷分享被 **6 个标题**复用

        (`VP6XqobV5vdtY7KuROqmhouKA1`,抖音 4 + 公众号 2)。按行报时它一条就占满
        告警前 8 行里的大半,别的链被挤掉 —— 读起来像"只有这一个问题"。
        聚合后一条链一行,「影响 6 个标题」反而成了它的严重度排序依据。
        """
        u = "https://pan.xunlei.com/s/VP6X?pwd=8cze"
        rows = []
        for i in range(6):
            r = {"source": "抖音线索", "title": f"麦穗与羊 #{i}", "url": u,
                 "verdict": "待人工看",
                 "why": "[容器名待核] 顶层是单一目录「文字类」,与标题不符 | 库里:x"}
            rows.append(r)
        rows.append({"source": "公众号", "title": "另一条坏链", "url": "https://pan.quark.cn/s/b",
                     "verdict": "空分享", "why": "分享里没有顶层条目"})
        g = link_audit.bad_groups(rows)
        assert len(g) == 2, f"6 行同链必须聚成一条:{g}"
        assert g[0][0] == u and g[0][3] == 6, "影响面大的排前面"
        assert "容器名待核" in g[0][1] and "空分享" in g[1][1]
        assert g[0][3] == 6
        assert "容器名待核" not in g[0][2], f"标记已进 kinds,说明里别再重复:{g[0][2]}"

    def test_限流那类不进聚合(self) -> None:
        """聚合只收「要人看」的;限流行照旧不进(否则又变成假红)。"""
        rows = [{"source": "抖音线索", "title": "t", "url": "https://pan.xunlei.com/s/x",
                 "verdict": "待人工看", "why": "疑似限流/网络"}]
        assert link_audit.bad_groups(rows) == []

    def test_全对齐只对齐才算(self) -> None:
        rows = [self._row("某剧全集", {"alive": True, "names": ["某剧全集"],
                                       "only_dir_name": "某剧全集"})]
        assert "全对齐" in link_audit.summary(rows)
        assert "全对齐" not in link_audit.summary(
            rows + [self._row("某资源", {"alive": None, "err": "疑似限流"})])


class Test夸克下探:
    """★ **只有夸克能下探**(`_list_share_files` 有 `pdir_fid`;迅雷没有这个参数)。

    不下探时,「顶层是单一目录」只能报「待人工看」—— 而实测这种形状里
    **既有容器名(判不了)也有真错配**(`穿成炮灰后…` 顶层是 `获取链接`,里面也不是它,
    那条链被 **100+ 个标题**共用)。下探一层就能把后者转成确定结论。
    """

    class _FakeQ:
        """按 fid 返回预设目录内容;`boom=True` 时下探抛异常(模拟限流)。"""

        def __init__(self, tree, boom=False):
            self.tree, self.boom, self.calls = tree, boom, []

        def _parse_share(self, url):
            return "sid", "pwd"

        def _get_stoken(self, sid, pwd):
            return "stoken"

        def _list_share_files(self, sid, stoken, pdir_fid="0"):
            self.calls.append(pdir_fid)
            if pdir_fid != "0" and self.boom:
                raise RuntimeError("模拟限流")
            return self.tree.get(pdir_fid, [])

    def _probe(self, tree, boom=False):
        pr = link_audit.Prober()
        pr._q = lambda: self._FakeQ(tree, boom)      # 绕过真凭据
        return pr.quark("https://pan.quark.cn/s/x")

    def test_下探到底且内容对得上就是对齐(self) -> None:
        p = self._probe({
            "0": [{"fid": "f1", "file_name": "获取链接", "dir": True}],
            "f1": [{"fid": "f2", "file_name": "穿成炮灰后：不是反派吗", "dir": True},
                   {"fid": "f3", "file_name": "番外.txt", "dir": False}],
        })
        assert p["inner_names"], p
        assert _verdict("穿成炮灰后：不是反派吗？怎么要抱抱", p)[0] == "对齐"

    def test_下探到底且内容对不上就是货不对版(self) -> None:
        """⚠️ **不许再挂在「待人工看」** —— 那等于把 100+ 共用的错链留在告警之外。"""
        p = self._probe({
            "0": [{"fid": "f1", "file_name": "获取链接", "dir": True}],
            "f1": [{"fid": "f2", "file_name": "中医古籍", "dir": True}],
            "f2": [{"fid": "f3", "file_name": "黄帝内经.pdf", "dir": False},
                   {"fid": "f4", "file_name": "伤寒论.pdf", "dir": False}],
        })
        v, why = _verdict("穿成炮灰后：不是反派吗？怎么要抱抱", p)
        assert v == "货不对版", (v, why)
        assert "中医古籍" in why and "穿成炮灰后" in why, why

    def test_连着两层容器要下探到底(self) -> None:
        """实测有 `古装大剧 / 分类整理 / 文字类` 这种**连着两层**都是收纳目录的。

        只下探一层会把「第二层容器名」当成真实内容 ⇒ 硬判货不对版(而且判错)。
        """
        p = self._probe({
            "0": [{"fid": "f1", "file_name": "古装大剧", "dir": True}],
            "f1": [{"fid": "f2", "file_name": "分类整理", "dir": True}],
            "f2": [{"fid": "f3", "file_name": "某剧全集01.mp4", "dir": False},
                   {"fid": "f4", "file_name": "某剧全集02.mp4", "dir": False}],
        })
        assert p["container_names"] == ["古装大剧", "分类整理"], p
        v, why = _verdict("某剧全集", p)
        assert v == "对齐", (v, why)

    def test_下探失败退回容器名待核(self) -> None:
        """⚠️ 下探本身被限流时,**不许**把"我们探不动"升级成「货不对版」。"""
        p = self._probe({"0": [{"fid": "f1", "file_name": "获取链接", "dir": True}]},
                        boom=True)
        assert "inner_names" not in p
        assert _verdict("某资源", p)[0] == "待人工看"

    def test_空目录不下探(self) -> None:
        """顶层不是单一目录 ⇒ 一次都不该多发请求(每次下探都是平台请求)。"""
        q = self._FakeQ({"0": [{"fid": "f1", "file_name": "甲", "dir": False},
                               {"fid": "f2", "file_name": "乙", "dir": True}]})
        pr = link_audit.Prober()
        pr._q = lambda: q
        p = pr.quark("https://pan.quark.cn/s/x")
        assert q.calls == ["0"] and "inner_names" not in p


class Test单个目录识别:
    """`_only_dir_name` —— 只认「**单一目录**」这一种形状。"""

    def test_单条且是目录(self) -> None:
        items = [{"name": "文字类", "kind": "drive#folder"}]
        assert link_audit._only_dir_name(
            items, lambda x: "folder" in x["kind"], lambda x: x["name"]) == "文字类"

    def test_单条但是文件(self) -> None:
        items = [{"name": "某剧01.mp4", "kind": "drive#file"}]
        assert link_audit._only_dir_name(
            items, lambda x: "folder" in x["kind"], lambda x: x["name"]) == ""

    def test_多条里有目录(self) -> None:
        """多个条目 ⇒ **不认**,哪怕里面有目录 —— 那种形状下对不上是真的对不上。"""
        items = [{"name": "甲", "kind": "drive#folder"}, {"name": "乙", "kind": "drive#file"}]
        assert link_audit._only_dir_name(
            items, lambda x: "folder" in x["kind"], lambda x: x["name"]) == ""

    def test_空列表(self) -> None:
        assert link_audit._only_dir_name([], lambda x: True, lambda x: x) == ""


class Test小工具:
    def test_认盘(self) -> None:
        assert _platform("https://pan.quark.cn/s/x") == "quark"
        assert _platform("https://pan.baidu.com/s/1abc?pwd=1") == "baidu"
        assert _platform("https://pan.xunlei.com/s/VP?pwd=1") == "xunlei"
        assert _platform("https://example.com") == ""

    def test_从带提取码的一行里只取第一个_URL(self) -> None:
        assert _first_url("https://pan.quark.cn/s/abc (提取码 8888) [百度]") == \
            "https://pan.quark.cn/s/abc"
        assert _first_url("没有链接") == ""
