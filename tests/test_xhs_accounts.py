"""小红书**统一账号池**单测(2026-10-12)。

## 它盯的是什么
"两条路(协议 / 页面渲染)共用**一台状态机**"这件事,最容易坏的地方**不是**逻辑本身,
而是**身份的归一化**和**能力的筛选**:

  ① **按能力取号**:协议路只能拿"有 cookie 串"的来源 —— 纯浏览器档案
     (`profile_dir`)里没有 cookie,取了它只会每轮白报一次失败、还把好档案标脏;
  ② **冷却要有时刻**:页面路原先只有"需验证"那个**没有时长**的标记,于是
     "频率风控"这种"等着就会好"的失败没法按时长退避(要么永远红、要么每轮重撞);
  ③ **两种失败不能混**:`need_verify`(要人去过一次) 与 `rate_limited`(只需等)
     —— 混了就会"该等的时候让人去重登"(白折腾 + 加重风控)。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest  # noqa: E402

from app.services import xhs_accounts as A  # noqa: E402


@pytest.fixture
def session():
    """内存库(池子的状态落在 `system_config`)。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db import models  # noqa: F401
    from app.db.database import Base

    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)()
    yield db
    db.close()


class _S:
    """只配'浏览器档案'的 settings —— 池里因此会有 `profile_dir` 这一类。"""

    xhs_browser_profiles = "data/xhs_1, data/xhs_2"
    xhs_browser_profile = ""
    xhs_web_session = ""


class _S2(_S):
    """再多一枚 `.env` 兜底 cookie ⇒ 池里同时有 `profile_dir` 与 `env`。"""

    xhs_web_session = "a1=aaa; web_session=bbb"


@pytest.fixture(autouse=True)
def _isolate_credentials(monkeypatch, tmp_path) -> None:
    """★ 与 `test_xhs_protocol.py` 同理:把"账号发现"的根目录挪到一个空临时目录,
    免得仓库里**恰好有真凭据**(登录后生成的 `data/xhs_session_*.json`)时,
    本文件的断言前提被它改变 —— 那种红是最冤的假红。"""
    monkeypatch.setattr(A, "_root", lambda: tmp_path)


def _wire(monkeypatch, session):
    monkeypatch.setattr("app.db.get_session_local", lambda: (lambda: session))


class TestKindsFilter:
    """★ **按能力取号**:协议路不拿纯浏览器档案。"""

    def test_档案也进池子(self, session, monkeypatch) -> None:
        _wire(monkeypatch, session)
        kinds = {r.kind for r in A.discover(_S())}
        assert "profile_dir" in kinds

    def test_协议路只拿有cookie的来源(self, session, monkeypatch) -> None:
        """⚠️ 纯浏览器档案(`profile_dir`)**没有 cookie 串** —— 协议路必须拿不到它,
        否则每轮都会"用空 cookie 去搜 ⇒ 报 guest",把好档案标脏。"""
        _wire(monkeypatch, session)
        ref = A.pick(_S2(), kinds=A.COOKIE_KINDS)
        assert ref is not None and ref.kind in A.COOKIE_KINDS
        #: 反复取,**永远**不该出现 profile_dir
        got = {A.pick(_S2(), kinds=A.COOKIE_KINDS).kind for _ in range(5)}
        assert got <= set(A.COOKIE_KINDS) and "profile_dir" not in got

    def test_不筛就都能取(self, session, monkeypatch) -> None:
        _wire(monkeypatch, session)
        assert A.pick(_S(), kinds=None) is not None


class TestProfileState:
    """★ 页面路按**档案目录**进出池子 —— 状态要认得出、冷却要带时刻。"""

    def test_报成频率冷却后_该档案进冷却(self, session, monkeypatch) -> None:
        _wire(monkeypatch, session)
        A.report_profile("data/xhs_1", A.STATE_RATE_LIMITED, "请勿频繁操作")
        cooling = A.cooling_profiles()
        assert cooling, "频率冷却没记下时刻(那就没法按时长退避)"
        assert any(p.endswith("xhs_1") for p in cooling), cooling

    def test_冷却是带时刻的_过点就自己放出来(self, session, monkeypatch) -> None:
        """★ **这一条是"按时长退避"的关键**:冷却必须能**自己过期**,
        否则就是又造了一盏"永远红的假红灯"。"""
        _wire(monkeypatch, session)
        A.report_profile("data/xhs_1", A.STATE_RATE_LIMITED, "请勿频繁操作")
        assert A.cooling_profiles(), "刚报的应该还在冷却里"
        #: 把"现在"推到冷却之后(3h 冷却 ⇒ 推 4 小时)⇒ 应该**放出来**
        import time
        assert A.cooling_profiles(now=time.time() + 4 * 3600) == set()

    def test_需验证是要人_频率是只需等(self, session, monkeypatch) -> None:
        """⚠️ `need_verify`(要人去过一次)与 `rate_limited`(只需等)**不能混** ——
        混了就会在该等的时候让人去重登(白折腾 + 加重风控)。

        两者的真实差别**不在**"是不是在冷却里"(都在 —— 都得先别撞),
        而在**时间到了之后**:
          · `rate_limited` ⇒ 冷却**到点自己放出来**(等着就会好);
          · `need_verify`  ⇒ 冷却过了**仍然在名单里**(它不会自己好,得人去动)。
        """
        _wire(monkeypatch, session)
        A.report_profile("data/xhs_1", A.STATE_NEED_VERIFY, "安全验证")
        A.report_profile("data/xhs_2", A.STATE_RATE_LIMITED, "频繁")
        need = A.need_verify_profiles()
        cool = A.cooling_profiles()
        #: 要人过验证的号在 `need_verify` 名单里;频率风控的号**不在**(它不用人做什么)
        assert any(p.endswith("xhs_1") for p in need), need
        assert not any(p.endswith("xhs_2") for p in need), need
        #: 两者都在冷却里(都"先别撞")
        assert any(p.endswith("xhs_1") for p in cool), cool
        assert any(p.endswith("xhs_2") for p in cool), cool

        import time
        later = time.time() + 13 * 3600          # 越过 3h 与 12h 两道冷却
        assert not any(p.endswith("xhs_2") for p in A.cooling_profiles(now=later)), \
            "频率冷却到点该自己放出来(否则又是'永远红')"
        assert any(p.endswith("xhs_1") for p in A.need_verify_profiles()), \
            "需验证不该自己好 —— 必须人去过验证/重登 (clear) 才会摘"

    def test_报健康会清掉冷却(self, session, monkeypatch) -> None:
        _wire(monkeypatch, session)
        A.report_profile("data/xhs_1", A.STATE_RATE_LIMITED, "频繁")
        assert A.cooling_profiles()
        A.report_profile("data/xhs_1", A.STATE_HEALTHY, "3 条")
        assert A.cooling_profiles() == set()
        assert A.need_verify_profiles() == set()

    def test_相对与绝对路径是同一个身份(self, session, monkeypatch) -> None:
        """⚠️ 配置/调用方手上可能是相对路径,池子里存的是绝对路径
        —— 认成两个身份的话,状态永远串不上(本仓踩过"单向陷阱")。"""
        _wire(monkeypatch, session)
        from pathlib import Path
        A.report_profile(Path("data/xhs_1"), A.STATE_RATE_LIMITED, "频繁")
        #: ⚠️ "绝对路径"这一侧要按 **仓库根(`A._root()`)** 构造,**不是**按 CWD ——
        #: 本文件的规则就是"相对路径按仓库根解析"(与 `discover()` 来源④同一套),
        #: 拿 CWD 去 `.resolve()` 会得到**另一个**绝对路径,那条断言就成了测运气。
        assert A.profile_key("data/xhs_1") == A.profile_key(A._root() / "data" / "xhs_1")
        assert A.cooling_profiles(), "相对路径报的冷却也要能被查到"


class TestFromError:
    """错误 → 状态的映射(把两条路的判据对齐在这一层)。"""

    def test_restricted_映射(self) -> None:
        assert A.from_error("restricted", "code=-104") == A.STATE_RESTRICTED

    def test_说过期就是expired(self) -> None:
        assert A.from_error("need_login", "登录已过期") == A.STATE_EXPIRED

    def test_没说过期就按游客档(self) -> None:
        assert A.from_error("need_login", "无登录信息") == A.STATE_GUEST

    def test_签名问题不赖账号(self) -> None:
        """⚠️ 签名/算法问题是**代码问题** —— 拿它去标账号(还在账号池里)会
        把好号一串串地误封 ⇒ 必须映射成 `unknown`(不设冷却)。"""
        assert A.from_error("sign", "461") == A.STATE_UNKNOWN


class _SProfile:
    """只配一枚档案 `data/prof1`(孪生实验用:它同时有档案目录与会话文件)。"""

    xhs_browser_profiles = "data/prof1"
    xhs_browser_profile = ""
    xhs_web_session = ""


def _make_twin(tmp_path, name="prof1"):
    """造一个**同时有档案目录和会话文件**的账号(登录脚本就是这么落盘的)。

    返回 `(会话文件路径, 档案目录)` —— 两者是**同一个物理账号的两个入口**。
    """
    d = tmp_path / "data" / name
    d.mkdir(parents=True, exist_ok=True)
    fp = tmp_path / "data" / f"xhs_session_{name}.json"
    fp.write_text('{"cookies": {"web_session": "04abc"}}', encoding="utf-8")
    return fp, d


class TestTwinIdentity:
    """★ **同一个账号的两个入口必须共用一份状态**(2026-10-12 修)。

    换号对照实验里踩到的:页面路把 `data/xhs_new` 打成 `need_verify`(账号级风控),
    可协议路看到的孪生兄弟还是 `unknown` ⇒ 照取不误、照发请求。
    而本仓的教训是 **「在已被风控的号上重试,冷却会被推后」** ⇒ 等于自己踩自己。
    """

    def test_会话文件归并到档案目录(self, session, monkeypatch, tmp_path) -> None:
        _wire(monkeypatch, session)
        fp, d = _make_twin(tmp_path)
        assert A._canon(f"file:{fp.resolve()}") == f"profile:{d.resolve()}"

    def test_没有配对档案时不归并(self, session, monkeypatch, tmp_path) -> None:
        """⚠️ 一个纯导出的会话文件(档案目录已被删)**不该**并到不相干的档案名下。"""
        _wire(monkeypatch, session)
        (tmp_path / "data").mkdir(parents=True, exist_ok=True)
        fp = tmp_path / "data" / "xhs_session_alone.json"
        fp.write_text('{"cookies": {}}', encoding="utf-8")
        k = f"file:{fp.resolve()}"
        assert A._canon(k) == k

    def test_别的来源不参与归并(self, session, monkeypatch) -> None:
        _wire(monkeypatch, session)
        assert A._canon("env") == "env"
        assert A._canon("user:1") == "user:1"

    def test_页面路打死后_协议路的孪生兄弟一起停手(self, session, monkeypatch,
                                       tmp_path) -> None:
        """★ **要害**:协议路不许再取一枚"页面路已知被风控"的号。"""
        _wire(monkeypatch, session)
        _make_twin(tmp_path)
        #: 归并前:协议路拿得到它(状态 unknown)
        assert A.pick(_SProfile(), kinds=A.COOKIE_KINDS) is not None
        #: 页面路用**同一个号** ⇒ 被判账号异常
        A.report_profile("data/prof1", A.STATE_NEED_VERIFY, "当前账号存在异常")
        #: 归并后:协议路的孪生兄弟必须一起停手(返回 None ⇒ 调用方"整轮跳过")
        assert A.pick(_SProfile(), kinds=A.COOKIE_KINDS) is None, (
            "协议路还在取一个已知被风控的号 —— 重试会把冷却推后")

    def test_冷却也一样共享(self, session, monkeypatch, tmp_path) -> None:
        _wire(monkeypatch, session)
        fp, _ = _make_twin(tmp_path)
        A.report_profile("data/prof1", A.STATE_RESTRICTED, "code=300011")
        ref = next(r for r in A.discover(_SProfile()) if r.kind == "session_file")
        assert ref.state == A.STATE_RESTRICTED, "孪生兄弟没继承到状态"
        assert not ref.available(), "孪生兄弟没继承到冷却"
        assert str(fp.resolve()) in {r.path for r in A.discover(_SProfile())}

    def test_旧数据里的别名行会被并到正主(self, session, monkeypatch) -> None:
        """一次性迁移要**幂等**,且别名行不能把正主覆盖掉。"""
        _wire(monkeypatch, session)
        #: 正主已存在 ⇒ 别名行丢弃,正主说了算
        merged = A._merge_aliases({
            "profile:/x/p": {"state": A.STATE_NEED_VERIFY, "ok": 3},
            "file:/x/xhs_session_p.json": {"state": A.STATE_HEALTHY, "ok": 99},
        })
        #: 这里 `_canon` 归并不生效(档案目录不存在)⇒ 两条都留着;
        #: 真正归并生效的情形由上一条用例覆盖。这里验证**不误伤**。
        assert merged["profile:/x/p"]["state"] == A.STATE_NEED_VERIFY


class _SOne:
    """池里**只有一枚**档案 ⇒ 计数断言不会被另一个号搅浑。"""

    xhs_browser_profiles = "data/xhs_1"
    xhs_browser_profile = ""
    xhs_web_session = ""


class TestRefFromRow:
    """库里只剩一行(当前配置已查不到)时,要能把它还原成**说得准**的引用。

    ⚠️ key 前缀**比行里的 `kind` 权威**:归并(见 `_canon`)会把一行 `file:` 搬成
    `profile:`,而行里还留着归并前的 `kind="session_file"`。
    """

    def test_归并过来的行要按profile_dir算(self, session, monkeypatch) -> None:
        _wire(monkeypatch, session)
        row = {"label": "档案 x", "kind": "session_file",
               "state": A.STATE_RESTRICTED, "path": "/tmp/xhs_session_x.json"}
        ref = A._ref_from_row(f"{A.PROFILE_PREFIX}/tmp/x", row)
        assert ref.kind == "profile_dir", "key 是 profile: ⇒ 就得按 profile_dir 算"
        assert ref.profile == "/tmp/x"

    def test_会话文件行要把path带回来(self, session, monkeypatch) -> None:
        """没有 `path` 就取不到 cookie/UA ⇒ 体检会报「存档里没有 UA」这种假警报。"""
        _wire(monkeypatch, session)
        row = {"label": "档案 x", "kind": "session_file",
               "path": "/tmp/xhs_session_x.json"}
        ref = A._ref_from_row("file:/tmp/xhs_session_x.json", row)
        assert ref.kind == "session_file"
        assert ref.path == "/tmp/xhs_session_x.json"

    def test_行里缺字段也不炸(self, session, monkeypatch) -> None:
        _wire(monkeypatch, session)
        ref = A._ref_from_row(f"{A.PROFILE_PREFIX}/tmp/y", {})
        assert ref.state == A.STATE_UNKNOWN and ref.ok == 0


class TestAccountTag:
    """★ **"这几个档案其实是同一个号"** —— 多账号轮换的头号杀手(2026-10-12)。

    风控是**账号级**的 ⇒ 多账号轮换是核心降险手段。可若池里几个"号"其实是同一个,
    轮换**一点用都没有**,还会让人误判成"换号也救不回来 ⇒ 是 IP 问题"。
    判据是 `web_session` 里编码的**账号段**(实测:两个不同档案、不同 `a1`、不同会话,
    只要账号段相同就是同一个号)。
    """

    #: 三个真实脱敏片段:前两个**同一个号**,第三个是另一个号
    WS_A1 = "040069b7366765643da334f0f9384b0899c555"
    WS_A2 = "040069b7366765643da32bbaf9384b46d6fa68"
    WS_B = "0400698eff879e57bfcf1ff7f9384b95bb1885"

    def test_同一个号的两种写法抽出同一个标识(self) -> None:
        assert A.account_tag(self.WS_A1) == A.account_tag(self.WS_A2)
        assert A.account_tag(self.WS_A1)

    def test_不同账号要能分开(self) -> None:
        assert A.account_tag(self.WS_A1) != A.account_tag(self.WS_B)

    def test_能从整串cookie里挑出web_session(self) -> None:
        ck = f"a1=abc; web_session={self.WS_A1}; webId=xyz"
        assert A.account_tag(ck) == A.account_tag(self.WS_A1)

    def test_游客档不给标识(self) -> None:
        """⚠️ `03` 是游客,没有稳定账号段 ⇒ 宁可返回空,也别乱猜成"同一个号"。"""
        assert A.account_tag("030069b7366765643da334f0f9384b0899c555") == ""

    def test_太短或为空不给标识(self) -> None:
        assert A.account_tag("") == ""
        assert A.account_tag("040069b7") == ""
        assert A.account_tag(None) == ""

    def test_没有web_session的cookie串不给标识(self) -> None:
        assert A.account_tag("a1=abc; webId=xyz") == ""


class TestReloginClears:
    """★ **人把要求做的事做完了 ⇒ 记号要自己摘掉**(2026-10-12 修)。

    `need_verify`/`expired`/`guest` 按设计**没有"到点自愈"**,只能靠人清 ——
    可之前没有任何一处会在**重登成功后**自动清它 ⇒ 会话活了、池子还红,
    除非那人恰好知道还要再跑一次 `tools/xhs_accounts.py reset`。
    这是本仓记过一次的**「单向陷阱」**(只写得进、摘不掉)。
    """

    def test_重登后要解除需验证(self, session, monkeypatch) -> None:
        _wire(monkeypatch, session)
        A.report_profile("data/xhs_1", A.STATE_NEED_VERIFY, "要过一次验证")
        assert A.need_verify_profiles(), "先确认它进去了"
        A.note_relogin("data/xhs_1")
        assert A.need_verify_profiles() == set(), "重登了还不摘 ⇒ 单向陷阱"
        assert A.pick(_SOne(), kinds=A.BROWSER_KINDS) is not None, "该能被重新取用"

    def test_重登后冷却也要清(self, session, monkeypatch) -> None:
        _wire(monkeypatch, session)
        A.report_profile("data/xhs_1", A.STATE_EXPIRED, "登录已过期")
        assert not A.discover(_SOne())[0].available()
        A.note_relogin("data/xhs_1")
        assert A.discover(_SOne())[0].available(), "重登了还在冷却 ⇒ 白等 15 分钟"

    def test_重登是unknown不是healthy(self, session, monkeypatch) -> None:
        """⚠️ 不能提前发奖状:会话活了 ≠ 真取到过数,让下一轮去证明它。"""
        _wire(monkeypatch, session)
        A.report_profile("data/xhs_1", A.STATE_EXPIRED, "登录已过期")
        A.note_relogin("data/xhs_1")
        assert A.discover(_SOne())[0].state == A.STATE_UNKNOWN

    def test_本来就没事就别打扰(self, session, monkeypatch) -> None:
        """反例:一个好端端的号被重登,不该被改回 unknown(会丢掉成功计数)。"""
        _wire(monkeypatch, session)
        A.report_profile("data/xhs_1", A.STATE_HEALTHY, "")
        before = A.discover(_SOne())[0]
        A.note_relogin("data/xhs_1")
        after = A.discover(_SOne())[0]
        assert after.state == A.STATE_HEALTHY and after.ok == before.ok

    def test_没记录过时调用不报错(self, session, monkeypatch) -> None:
        _wire(monkeypatch, session)
        A.note_relogin("data/从没见过的档案")               #: 不该抛


class TestUsableCount:
    """★ **"可用"的分档不能是假绿灯**(2026-10-12 修,2026-10-13 改成四分类)。

    `available` 只看冷却;可 `expired`/`guest`/`need_verify` 的号**要人动手**才活得过来。
    原来报"当前可用 3 个" ⇒ 调度器真敢接着跑,然后一直撞南墙。

    ⚠️ 分类必须**互斥且穷尽**:之前摘要看 `available`、体检看"状态是不是
    healthy/unknown",两边各判各的 ⇒ 同一份数据里出现「当前可用 2 个」与
    「🔴 一个都不能用」**两行互相打架**;被连坐的号还**没被任何一类统计到**。
    ⇒ 现在统一走 `classify()`:每一条登记恰好落一类。
    """

    def test_过期的号归到要人动(self, session, monkeypatch) -> None:
        _wire(monkeypatch, session)
        A.report_profile("data/xhs_1", A.STATE_EXPIRED, "登录已过期")
        s = A.summary(_SOne())
        assert "可用 0" in s and "要人动 1" in s, s

    def test_需验证的号归到要人动(self, session, monkeypatch) -> None:
        _wire(monkeypatch, session)
        A.report_profile("data/xhs_1", A.STATE_NEED_VERIFY, "要过一次验证")
        assert "要人动 1" in A.summary(_SOne()), A.summary(_SOne())

    def test_健康的号才算可用(self, session, monkeypatch) -> None:
        _wire(monkeypatch, session)
        A.report_profile("data/xhs_1", A.STATE_HEALTHY, "")
        assert "可用 1" in A.summary(_SOne()), A.summary(_SOne())

    def test_没用过的号值得一试_算可用(self, session, monkeypatch) -> None:
        """`unknown` 不在 `NEEDS_HUMAN` 里 ⇒ 与 `pick()` 的排序一致(先试没试过的)。"""
        _wire(monkeypatch, session)
        assert "可用 1" in A.summary(_SOne()), A.summary(_SOne())

    def test_快照里的needs_human与可用互斥(self, session, monkeypatch) -> None:
        _wire(monkeypatch, session)
        A.report_profile("data/xhs_1", A.STATE_GUEST, "游客档")
        rows = {r["label"]: r for r in A.snapshot(_SOne())}
        r = rows["档案 xhs_1"]
        #: 冷却只有 5 分钟 ⇒ 一过 `available` 就为真,但 `needs_human` 仍是真
        assert r["needs_human"] is True
        assert r["state"] in A.NEEDS_HUMAN
        #: 摘要里的"可用"必须认这一条,不能只看冷却
        assert "可用 0" in A.summary(_SOne()), A.summary(_SOne())


class _STwo:
    """两个档案 —— 用于"它们登的是不是同一个号"的实验。"""

    xhs_browser_profiles = "data/p1, data/p2"
    xhs_browser_profile = ""
    xhs_web_session = ""


class TestAccountLevelState:
    """★ **风控按账号记账,不按档案目录记账**(2026-10-13 修)。

    体检自己报出来的:池里 **7 条登记其实只有 2 个真账号**。而状态机是**档案级**记的
    ⇒ `xhs_2` 被打成 `need_verify`,孪生兄弟 `xhs_3` 仍写着 `healthy`
    ⇒ `pick()` 高高兴兴挑中 `xhs_3` ⇒ **用那个正被风控的号又发了一次请求**。

    这就是「**一个号都活不过一天**」的机制性原因:我们以为在多号轮换降险,
    其实是在**反复戳同一个号**;而本仓的教训是**在已被风控的号上重试会把冷却推后**。
    单体看每次都是"新档案、新会话、新指纹";账号维度看却是"同一个号一分钟前刚被判异常,又来了"。
    """

    #: 前两个**同一个号**(账号段相同),第三个是另一个号
    WS_A = "040069b7366765643da334f0f9384b0899c555"
    WS_B = "0400698eff879e57bfcf1ff7f9384b95bb1885"

    @staticmethod
    def _cookies(monkeypatch, mapping) -> None:
        """按档案名喂 cookie —— 免去落真实凭据文件。"""
        monkeypatch.setattr(
            A, "cookie_of",
            lambda r: mapping.get(A.Path(r.profile or r.key).name, ""))

    def test_两个档案登同一个号_只能算一个账号(self, session, monkeypatch) -> None:
        _wire(monkeypatch, session)
        self._cookies(monkeypatch, {"p1": f"web_session={self.WS_A}",
                                    "p2": f"web_session={self.WS_A}"})
        assert len(A.account_state_map(_STwo())) == 1

    def test_孪生档案里一个红了_整组停用(self, session, monkeypatch) -> None:
        _wire(monkeypatch, session)
        self._cookies(monkeypatch, {"p1": f"web_session={self.WS_A}",
                                    "p2": f"web_session={self.WS_A}"})
        A.report_profile("data/p1", A.STATE_NEED_VERIFY, "当前账号存在异常")
        g = A.account_state_map(_STwo())[A.account_tag(self.WS_A)]
        #: 取**最坏**的那个状态,不是"多数派" —— 保守方向才不会踩"越试越久"
        assert g["state"] == A.STATE_NEED_VERIFY
        assert g["blocked"] is True and g["needs_human"] is True

    def test_自己绿的孪生兄弟也不许被选中(self, session, monkeypatch) -> None:
        """★ **要害**:这正是"换了档案其实还在戳同一个号"的缺口。"""
        _wire(monkeypatch, session)
        self._cookies(monkeypatch, {"p1": f"web_session={self.WS_A}",
                                    "p2": f"web_session={self.WS_A}"})
        A.report_profile("data/p1", A.STATE_NEED_VERIFY, "当前账号存在异常")
        #: p2 **从没被报过**(状态 unknown),可它登的正是那个出事的号
        assert A.pick(_STwo()) is None, (
            "还在取一个正被风控的账号 —— 重试会把冷却推后")

    def test_不同账号互不影响(self, session, monkeypatch) -> None:
        """⚠️ 别把"连坐"做成"一个坏了全池停摆" —— 不同账号必须还能用。"""
        _wire(monkeypatch, session)
        self._cookies(monkeypatch, {"p1": f"web_session={self.WS_A}",
                                    "p2": f"web_session={self.WS_B}"})
        A.report_profile("data/p1", A.STATE_NEED_VERIFY, "当前账号存在异常")
        got = A.pick(_STwo())
        assert got is not None and A.Path(got.profile).name == "p2"

    def test_冷却按账号取最晚的那个时刻(self, session, monkeypatch) -> None:
        _wire(monkeypatch, session)
        self._cookies(monkeypatch, {"p1": f"web_session={self.WS_A}",
                                    "p2": f"web_session={self.WS_A}"})
        A.report_profile("data/p1", A.STATE_RESTRICTED, "code=300011")
        g = A.account_state_map(_STwo())[A.account_tag(self.WS_A)]
        assert g["cooldown_until"] > 0 and g["harmful"] is True

    def test_冷却过了就自动解封(self, session, monkeypatch) -> None:
        """`rate_limited` 是"只需等" ⇒ 到点必须自己放出来,不能变成永远红。"""
        _wire(monkeypatch, session)
        self._cookies(monkeypatch, {"p1": f"web_session={self.WS_A}",
                                    "p2": f"web_session={self.WS_A}"})
        A.report_profile("data/p1", A.STATE_RATE_LIMITED, "code=300012")
        tag = A.account_tag(self.WS_A)
        assert A.account_state_map(_STwo())[tag]["blocked"] is True
        later = A.account_state_map(_STwo(), now=A.time.time() + 99 * 3600)[tag]
        assert later["blocked"] is False, "冷却过了却还封着 ⇒ 造出永远红的假红灯"

    def test_摘要要报真实账号数而不是档案数(self, session, monkeypatch) -> None:
        _wire(monkeypatch, session)
        self._cookies(monkeypatch, {"p1": f"web_session={self.WS_A}",
                                    "p2": f"web_session={self.WS_A}"})
        n_acc, n_rows = A.real_account_count(_STwo())
        assert (n_acc, n_rows) == (1, 2)
        assert "实际 1 个账号" in A.summary(_STwo()), A.summary(_STwo())

    def test_摘要要区分只在等和要人动手(self, session, monkeypatch) -> None:
        """记过的纪律:坏消息要说准是**哪种坏** —— 谁该动手、谁只需等。"""
        _wire(monkeypatch, session)
        self._cookies(monkeypatch, {"p1": f"web_session={self.WS_A}",
                                    "p2": f"web_session={self.WS_B}"})
        #: p1 在有害冷却(180 分)⇒ 只需等;p2 已过期 ⇒ 要人重登
        A.report_profile("data/p1", A.STATE_RATE_LIMITED, "code=300012")
        A.report_profile("data/p2", A.STATE_EXPIRED, "登录已过期")
        s = A.summary(_STwo())
        assert "只在等 1" in s and "要人动 1" in s, s
        assert A.classify({"state": A.STATE_RATE_LIMITED, "available": False,
                            "cooldown_min": 180, "blocked_by_account": False}
                           ) == A.CLS_WAIT
        assert A.classify({"state": A.STATE_EXPIRED, "available": True,
                            "cooldown_min": 0, "blocked_by_account": False}
                           ) == A.CLS_HUMAN

    def test_连坐的号单独归一类(self, session, monkeypatch) -> None:
        """★ 自己状态是好的,但**登的是同一个出事的号** ⇒ 归「连坐」,不是「可用」。"""
        _wire(monkeypatch, session)
        self._cookies(monkeypatch, {"p1": f"web_session={self.WS_A}",
                                    "p2": f"web_session={self.WS_A}"})
        A.report_profile("data/p1", A.STATE_NEED_VERIFY, "账号异常")
        rows = {r["label"]: r for r in A.snapshot(_STwo())}
        assert A.classify(rows["档案 p1"]) == A.CLS_HUMAN
        assert A.classify(rows["档案 p2"]) == A.CLS_SHADOWED
        #: 且**不能**被算进"可用"
        assert A.row_usable(rows["档案 p2"]) is False

    def test_不同账号就该各走各的(self, session, monkeypatch) -> None:
        """⚠️ 反例:连坐**不能**滥杀 —— 不同账号段互不影响。"""
        _wire(monkeypatch, session)
        self._cookies(monkeypatch, {"p1": f"web_session={self.WS_A}",
                                    "p2": f"web_session={self.WS_B}"})
        A.report_profile("data/p1", A.STATE_NEED_VERIFY, "账号异常")
        rows = {r["label"]: r for r in A.snapshot(_STwo())}
        assert A.classify(rows["档案 p1"]) == A.CLS_HUMAN
        assert A.classify(rows["档案 p2"]) == A.CLS_USABLE
        assert A.row_usable(rows["档案 p2"]) is True

    def test_快照带账号段与连坐标记(self, session, monkeypatch) -> None:
        _wire(monkeypatch, session)
        self._cookies(monkeypatch, {"p1": f"web_session={self.WS_A}",
                                    "p2": f"web_session={self.WS_A}"})
        A.report_profile("data/p1", A.STATE_RESTRICTED, "code=300011")
        rows = {r["label"]: r for r in A.snapshot(_STwo())}
        p2 = rows["档案 p2"]
        assert p2["account"] == A.account_tag(self.WS_A)
        assert p2["blocked_by_account"] is True

    def test_取不到账号段时不归并也不炸(self, session, monkeypatch) -> None:
        """游客档没有稳定账号段 ⇒ **宁可不说**,也不能因此把一个号都选不出来。"""
        _wire(monkeypatch, session)
        self._cookies(monkeypatch, {})
        assert A.account_state_map(_STwo()) == {}
        assert A.pick(_STwo()) is not None
