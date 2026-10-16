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
