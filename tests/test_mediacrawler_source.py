"""MediaCrawler 适配层的**失败契约**单测(2026-10-03)。

核心一条:**硬失败必须抛 `MediaCrawlerError`,不能返回空列表冒充"没搜到"**。
这条链只在每天 11:00 无人值守时跑(扫码超时是最常见的失败),吞掉就等于静默失灵。
"""
import os
import subprocess

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest

from app.services import mediacrawler_source as mc

# 自动 fixture 会把 `_read_results` / `_write_config` 打成假的;下面要测**真**的那个,先留个引用。
_REAL_READ_RESULTS = mc._read_results
_REAL_WRITE_CONFIG = mc._write_config


@pytest.fixture(autouse=True)
def _stub_io(monkeypatch):
    """默认让"装好了、配置写得进、跑完读得到结果",各用例只覆盖自己关心的那一段。"""
    monkeypatch.setattr(mc, "available", lambda: (True, "ok"))
    # ⚠️ `_write_config` 现在多一个参数(登录 cookie,2026-10-06 起支持 --lt cookie);
    # 桩要跟着收宽,否则报 `lambda() takes 1 positional argument but 2 were given`。
    monkeypatch.setattr(mc, "_write_config", lambda kws, cookie="": None)
    #: 2026-10-19:抖音也走 cookie 登录 ⇒ 别让单测去碰真库/真档案。
    monkeypatch.setattr(mc, "_login_cookie", lambda platform, user_id=1: "")
    monkeypatch.setattr(mc, "profile_login_state", lambda platform: True)
    #: 重试之间的 sleep 不该真的等 3 秒。
    monkeypatch.setattr(mc.time, "sleep", lambda s: None)
    monkeypatch.setattr(mc, "_read_results", lambda platform, since=None: [{"uid": "u"}])
    monkeypatch.setattr(mc, "PLATFORM_IDS", {"douyin": "dy"})


class _Proc:
    def __init__(self, returncode: int = 0, stderr: bytes = b"", stdout: bytes = b""):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout


def test_crawl_raises_when_tool_not_installed(monkeypatch) -> None:
    monkeypatch.setattr(mc, "available", lambda: (False, "独立 venv 未建"))
    with pytest.raises(mc.MediaCrawlerError) as ei:
        mc.crawl("douyin", ["ps教程"])
    assert "venv" in str(ei.value)


def test_crawl_raises_on_timeout_with_actionable_hint(monkeypatch) -> None:
    """超时是最常见的无人值守失败 —— 而且**多半卡在等扫码**,提示里要写出来。"""
    def _boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="main.py", timeout=600)

    monkeypatch.setattr(mc.subprocess, "run", _boom)
    with pytest.raises(mc.MediaCrawlerError) as ei:
        mc.crawl("douyin", ["ps教程"])
    assert "扫码" in str(ei.value)


def test_crawl_raises_on_nonzero_exit(monkeypatch) -> None:
    monkeypatch.setattr(mc.subprocess, "run",
                        lambda *a, **k: _Proc(returncode=1, stderr=b"Traceback: login failed"))
    with pytest.raises(mc.MediaCrawlerError) as ei:
        mc.crawl("douyin", ["ps教程"])
    assert "退出码 1" in str(ei.value) and "login failed" in str(ei.value)


def test_瞬时超时要重试一次(monkeypatch) -> None:
    """★ 2026-10-19:00:10 那次浏览器兜底的真因是 `Page.goto` 30s 超时。

    这类**瞬时故障**重试一次通常就好;它和"登录失效 / 风控全空"不是一回事。
    """
    class _Bad:
        returncode = 1
        stderr = b"playwright._impl._errors.TimeoutError: Page.goto: Timeout 30000ms exceeded"
        stdout = b""

    calls = {"n": 0}

    def _run(cmd, *a, **k):
        #: ⚠️ `kill_stale_browsers()` 也走 `subprocess.run`(PowerShell)——只数真采集那一次。
        if not (isinstance(cmd, list) and any("main.py" in str(x) for x in cmd)):
            return _Proc()
        calls["n"] += 1
        return _Bad() if calls["n"] == 1 else _Proc()

    monkeypatch.setattr(mc.subprocess, "run", _run)
    assert mc.crawl("douyin", ["x"]) == [{"uid": "u"}]
    assert calls["n"] == 2, "瞬时超时应当重试一次"


def test_全空不重试(monkeypatch) -> None:
    """空结果**不是**瞬时故障 —— 重试只会对同一个账号多打一次风控。"""
    class _Empty:
        returncode = 0
        stderr = b""
        stdout = b"keyword:a, aweme_list:[]\n"

    calls = {"n": 0}

    def _run(cmd, *a, **k):
        if not (isinstance(cmd, list) and any("main.py" in str(x) for x in cmd)):
            return _Proc()
        calls["n"] += 1
        return _Empty()

    monkeypatch.setattr(mc.subprocess, "run", _run)
    monkeypatch.setattr(mc, "_read_results", lambda platform, since=None: [])
    with pytest.raises(mc.MediaCrawlerError):
        mc.crawl("douyin", ["a"])
    assert calls["n"] == 1, "空结果不该重试"


def test_档案未登录且无cookie要给出可照做的错(monkeypatch) -> None:
    """把"档案没登录"与"风控空结果"分开 —— 前者要人能直接照做。"""
    monkeypatch.setattr(mc, "profile_login_state", lambda platform: False)
    monkeypatch.setattr(mc, "_login_cookie", lambda platform, user_id=1: "")
    with pytest.raises(mc.MediaCrawlerError) as ei:
        mc.crawl("douyin", ["x"])
    assert "mc_login.py dy" in str(ei.value)


def test_write_config_用repr避免引号把配置写坏(monkeypatch, tmp_path) -> None:
    """抖音完整 jar 有 5000+ 字符,可能带引号/反斜杠/emoji —— 手工拼引号会写出语法错。"""
    import ast

    cfg = tmp_path / "base_config.py"
    cfg.write_text('KEYWORDS = "x"\nSAVE_DATA_OPTION = "csv"\n'
                   'LOGIN_TYPE = "qrcode"\nCOOKIES = ""\n', encoding="utf-8")
    monkeypatch.setattr(mc, "CONFIG", cfg)
    cookie = 'sessionid=a"b\\c; sid_tt=x'
    _REAL_WRITE_CONFIG(['词"quote', "emoji🥩"], cookie)
    vals: dict[str, object] = {}
    for line in cfg.read_text(encoding="utf-8").splitlines():
        if " = " in line and not line.lstrip().startswith("#"):
            k, v = line.split(" = ", 1)
            vals[k] = ast.literal_eval(v)
    assert vals["COOKIES"] == cookie
    assert vals["LOGIN_TYPE"] == "cookie"
    assert "🥩" in str(vals["KEYWORDS"])


def test_crawl_returns_empty_when_it_ran_but_found_nothing(monkeypatch) -> None:
    """跑通了、只是**真没结果** → 空列表(不是错误)。这条界线必须守住,
    否则每天都会误报失败,真正的失败就被淹了。"""
    monkeypatch.setattr(mc.subprocess, "run", lambda *a, **k: _Proc())
    monkeypatch.setattr(mc, "_read_results", lambda platform, since=None: [])
    assert mc.crawl("douyin", ["ps教程"]) == []


def test_crawl_ignores_unknown_platform(monkeypatch) -> None:
    """没见过的平台名 → 空列表(而不是抛错):配置里多写一个平台名不该让整轮炸。"""
    assert mc.crawl("tiktok", ["ps教程"]) == []


def test_read_results_ignores_stale_files(monkeypatch, tmp_path) -> None:
    """⚠️ **只认本轮写出的文件**(2026-10-03 实测踩到)。

    MediaCrawler **搜到 0 条时根本不写文件**,而文件名是按日期的
    (`search_contents_2026-10-02.jsonl`)—— 于是"今天什么都没搜到"会回退读到**昨天的文件**,
    把 40 条旧线索当新线索返回,下游每天把同一批旧内容再推一遍。
    """
    d = tmp_path / "douyin" / "jsonl"
    d.mkdir(parents=True)
    old = d / "search_contents_2026-10-02.jsonl"
    old.write_text('{"title": "昨天的旧线索", "nickname": "籽***）", "creator_hash": "h1"}',
                   encoding="utf-8")
    import os
    import time as _t
    old_mtime = _t.time() - 86400
    os.utime(old, (old_mtime, old_mtime))
    monkeypatch.setattr(mc, "DATA_DIR", tmp_path)
    monkeypatch.setattr(mc, "_read_results", _REAL_READ_RESULTS)

    # 本轮开始时间在旧文件之后 → 旧文件必须被排除,返回空(而不是假装"有 1 条")
    assert mc._read_results("douyin", since=_t.time() - 10) == []


def test_read_results_accepts_file_written_this_run(monkeypatch, tmp_path) -> None:
    """本轮真写了文件 → 正常读出来(别把闸门做成一刀切)。"""
    d = tmp_path / "douyin" / "jsonl"
    d.mkdir(parents=True)
    f = d / "search_contents_2026-10-03.jsonl"
    f.write_text('{"title": "今天的新线索", "nickname": "籽***）", "creator_hash": "h2",'
                 ' "aweme_url": "https://d/1", "source_keyword": "网盘资源"}', encoding="utf-8")
    monkeypatch.setattr(mc, "DATA_DIR", tmp_path)
    monkeypatch.setattr(mc, "_read_results", _REAL_READ_RESULTS)
    import time as _t
    out = mc._read_results("douyin", since=_t.time() - 10)
    assert len(out) == 1 and "新线索" in out[0]["snippet"]


def test_crawl_raises_when_every_keyword_came_back_empty(monkeypatch) -> None:
    """全部关键词都返回 `aweme_list:[]` → 必须报出来,**不能安静地交 0 条**。

    实测 2026-10-03:抖音对 `网盘资源` 这种必然有结果的泛词也返回空,而 10-02 同一档案能出
    40 条 —— 那就是扫码登录过期。不报出来的话,这条链会每天安静地交出 0 条、你毫无察觉。

    ★★ **2026-10-16 订正:不能再只报"登录态失效"。** 同一天实测 08:10 全空、
    **09:30 同一档案、同一出口 IP 又搜到了 8 条** —— 这种"时好时坏"是**风控**,
    而风控的动作是**等**,重扫档案**什么都不会变**。
    ⇒ 两种原因**动作相反**,消息必须**都说到**,并给出**怎么分**
    (否则又是一次"报告喊重导 → 人照做 → 无效",本仓为此栽过多次)。
    """
    class _P:
        returncode = 0
        stderr = b""
        stdout = (b"keyword:\xe7\xbd\x91\xe7\x9b\x98\xe8\xb5\x84\xe6\xba\x90, aweme_list:[]\n"
                  b"keyword:ps\xe6\x95\x99\xe7\xa8\x8b, aweme_list:[]\n")

    monkeypatch.setattr(mc.subprocess, "run", lambda *a, **k: _P())
    monkeypatch.setattr(mc, "_read_results", lambda platform, since=None: [])
    with pytest.raises(mc.MediaCrawlerError) as ei:
        mc.crawl("douyin", ["网盘资源", "ps教程"])
    msg = str(ei.value)
    assert "登录态过期" in msg and "重扫" in msg, f"①（登录态)要说:{msg}"
    assert "过验证" in msg and "没用" in msg, f"②（风控）要说清「重扫没用」:{msg}"
    assert "怎么分" in msg, f"两种原因动作相反 ⇒ 必须给出判别方法:{msg}"


def test_crawl_returns_empty_when_only_some_keywords_empty(monkeypatch) -> None:
    """只要**有一个**词有结果,就不该报错(否则"某几个词没人搜"会被误判成登录失效)。"""
    class _P:
        returncode = 0
        stderr = b""
        stdout = b"keyword:a, aweme_list:[]\nkeyword:b, aweme_list:[{...}]\n"

    monkeypatch.setattr(mc.subprocess, "run", lambda *a, **k: _P())
    monkeypatch.setattr(mc, "_read_results", lambda platform, since=None: [])
    assert mc.crawl("douyin", ["a", "b"]) == []


def test_parse_record_keeps_share_count() -> None:
    """**转发量必须带出来**(2026-10-03):结算要拿它当线索级强弱代理。

    抖音给的是**字符串**("176"),不转 int 的话后面算总和会变成字符串拼接。
    """
    rec = {"nickname": "籽***）", "creator_hash": "h", "aweme_url": "https://d/video/1",
           "title": "《三岁分享》某某资源", "share_count": "176"}
    item = mc._parse_record(rec, "douyin")
    assert item is not None and item["share_count"] == 176
    assert mc._parse_record({"nickname": "n", "creator_hash": "h", "title": "t"}, "douyin")["share_count"] == 0


class TestExplain:
    """把退出原因翻成**能照做**的话(2026-10-04)。

    ⚠️ 最要命的一条 `TargetClosedError`:本项目 CDP 模式 `CDP_CONNECT_EXISTING=False`,
    程序**自己启动 Edge 并接管**、跑完自己关 —— 采集期间弹出来的窗口是**程序正在用的**,
    关它就直接失败(实测小红书/快手就是这么挂的),而堆栈长得很容易被误读成"没登录"。
    """

    def test_target_closed_says_dont_close_the_window(self) -> None:
        tail = ('playwright._impl._errors.TargetClosedError: BrowserContext.cookies: '
                'Target page, context or browser has been closed')
        msg = mc._explain(tail)
        assert "不要手工关掉" in msg and "不是登录问题" in msg

    def test_cdp_port_conflict_is_explained(self) -> None:
        assert "CDP 端口" in mc._explain("Error: CDP port 9222 is not accessible")

    def test_unknown_tail_passes_through_unchanged(self) -> None:
        """认不出来的原因**原样带出** —— 别自作主张翻译成一句含糊的话,那是毁证据。"""
        tail = "Traceback: some_other_random_failure at foo.py:12"
        assert mc._explain(tail) == tail


# ------------------------------------------------ 残留浏览器清理(2026-10-05)

class TestKillStaleBrowsers:
    """★ **2026-10-05 实测事故**:小红书连挂几天,**根因不是登录过期**。

    链路:`subprocess.run(timeout=600)` 超时**只杀直接子进程**,而 Edge 是 `main.py`
    **另起**的、不在它的进程树里 ⇒ 留在那儿占着 profile ⇒ 下一个跑同平台的进程**撞锁**
    ⇒ 又失败 ⇒ 又留一批。**失败会自我延续。**

    实测:今早 09:00 那轮留下 **10 个** Edge(`xhs` 档案),一直占着,
    连 **Cookies 文件都读不出来**(PermissionError)。而档案里
    **`web_session` 到期 2027-10-05** —— **登录一直是好的**。
    """

    def test_非_windows_直接返回0(self, monkeypatch) -> None:
        """本模块只在 Windows 上用,但别让别的平台炸。"""
        import app.services.mediacrawler_source as mc

        monkeypatch.setattr("sys.platform", "linux")
        assert mc.kill_stale_browsers() == 0

    def test_按输出里的_pid_计数(self, monkeypatch) -> None:
        import subprocess

        import app.services.mediacrawler_source as mc

        calls = {}

        class _P:
            stdout = b"1234\n5678\n"

        def fake_run(cmd, **kw):
            calls["cmd"] = cmd
            return _P()

        monkeypatch.setattr("sys.platform", "win32")
        monkeypatch.setattr(subprocess, "run", fake_run)
        assert mc.kill_stale_browsers() == 2

    def test_只杀带_MediaCrawler_路径的_不碰用户的浏览器(self, monkeypatch) -> None:
        """⚠️ **这条是安全阀**:机器上实测 36 个 Edge 里只有 10 个是我们的,
        杀错就是**把用户正在用的浏览器关了**。所以命令行里必须有 MediaCrawler 的路径。
        """
        import subprocess

        import app.services.mediacrawler_source as mc

        calls = {}

        class _P:
            stdout = b""

        def fake_run(cmd, **kw):
            calls["ps"] = cmd[-1]
            return _P()

        monkeypatch.setattr("sys.platform", "win32")
        monkeypatch.setattr(subprocess, "run", fake_run)
        mc.kill_stale_browsers()
        ps = calls["ps"]
        assert "MediaCrawler" in ps and "browser_data" in ps, \
            f"筛选条件必须限定在 MediaCrawler 的档案目录,实际是:{ps[:120]}"
        assert "msedge.exe" in ps

    def test_清理失败不能挡住采集(self, monkeypatch) -> None:
        """它是**善后**,不是主流程 —— 失败就返回 0,绝不抛。"""
        import subprocess

        import app.services.mediacrawler_source as mc

        def boom(*a, **k):
            raise OSError("powershell 不在 PATH")

        monkeypatch.setattr("sys.platform", "win32")
        monkeypatch.setattr(subprocess, "run", boom)
        assert mc.kill_stale_browsers() == 0


class Test指标与链接的别名:
    """★★ **2026-10-16 用户问「知乎快手是否可以做到这样了,可以看到作品,链接,互动数」**。

    实测四个平台**字段名各不同**,少认一个那个平台就**永远是空** —— 而看起来像"没人看/没这条":

        douyin   aweme_url           liked/collected/comment/share ✓
        xhs      note_url            liked_count = **"1.3万"**(字符串带量词)
        tieba    note_url            **total_replay_num**(回复数,从没被读)
        kuaishou **video_url**       **viewd_count**(播放量 1310138,从没被读)
    """

    def _rec(self, **kw):
        base = {"nickname": "某人", "user_id": "7", "title": "某资源[网盘链接]"}
        base.update(kw)
        return base

    def test_快手的作品链接键是_video_url(self) -> None:
        r = mc._parse_record(self._rec(video_url="https://www.kuaishou.com/short-video/x"), "kuaishou")
        assert r and r["url"] == "https://www.kuaishou.com/short-video/x"

    def test_快手的播放量键是_viewd_count(self) -> None:
        r = mc._parse_record(self._rec(viewd_count=1310138), "kuaishou")
        assert r and r["metrics"].get("play_count") == 1310138

    def test_贴吧的回复数当评论(self) -> None:
        r = mc._parse_record(self._rec(total_replay_num=39), "tieba")
        assert r and r["metrics"].get("comment_count") == 39

    def test_带万字的点赞数要认得(self) -> None:
        """小红书实测 `liked_count = "1.3万"`;`int(float(...))` 会抛 ValueError ⇒ 被静默丢掉。"""
        r = mc._parse_record(self._rec(note_url="https://x/1", liked_count="1.3万"), "xhs")
        assert r and r["metrics"].get("liked_count") == 13000

    def test_缺失还是缺失_不许变成0(self) -> None:
        """`None`/空串 = 平台没给 ⇒ 不放进 metrics(填 0 会被读成"没人看")。"""
        r = mc._parse_record(self._rec(liked_count=None, viewd_count=""), "kuaishou")
        assert r and "liked_count" not in r["metrics"] and "play_count" not in r["metrics"]


class Test认不出字段就由_id_拼链接:
    """★ 2026-10-16:**同一天踩了两次同一族** —— 「那个平台的『对标作品』列恒为空」,
    根因都是**字段名对不上**(快手 `video_url`、微博 `mblogid`)。逐字段打补丁是**打地鼠**:
    上游改一次名,那一列就再空一次,而且**空得毫无提示**(卡片上只是 `—`)。

    ⇒ 兜底改成"**从 id 拼**":这些平台的规范地址与 id 一一对应,而 id 字段比 URL 字段稳得多。
    ⚠️ 只按**该平台自己的 id** 拼 —— 「给错链比不给链更坏」。
    """

    def _rec(self, **kw):
        base = {"nickname": "某人", "user_id": "7", "title": "某资源"}
        base.update(kw)
        return base

    def test_快手_认不出_url_字段时由_video_id_拼(self) -> None:
        r = mc._parse_record(self._rec(video_id="3xabc"), "kuaishou")
        assert r and r["url"] == "https://www.kuaishou.com/short-video/3xabc"

    def test_抖音_由_aweme_id_拼(self) -> None:
        r = mc._parse_record(self._rec(aweme_id="7300000000000000000"), "douyin")
        assert r and r["url"] == "https://www.douyin.com/video/7300000000000000000"

    def test_贴吧_由_tid_拼(self) -> None:
        r = mc._parse_record(self._rec(tid="10210510154"), "tieba")
        assert r and r["url"] == "https://tieba.baidu.com/p/10210510154"

    def test_认得出的_url_字段优先_不许被拼出来的顶掉(self) -> None:
        r = mc._parse_record(self._rec(video_id="3xabc",
                                       video_url="https://www.kuaishou.com/short-video/REAL"),
                             "kuaishou")
        assert r and r["url"] == "https://www.kuaishou.com/short-video/REAL"

    def test_不跨平台拼(self) -> None:
        """⚠️ 快手记录只有 `aweme_id`(**不是它的 id**)时**不许**拼出抖音地址。"""
        r = mc._parse_record(self._rec(aweme_id="7300"), "kuaishou")
        assert r and r["url"] == ""

    def test_一个_id_都没有就留空(self) -> None:
        r = mc._parse_record(self._rec(), "kuaishou")
        assert r and r["url"] == "", "拼不出就老实留空(`—(源未给链接)`),别编一个"
