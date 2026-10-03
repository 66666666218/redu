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

# 自动 fixture 会把 `_read_results` 打成假的;下面两条要测**真**的那个,先留个引用。
_REAL_READ_RESULTS = mc._read_results


@pytest.fixture(autouse=True)
def _stub_io(monkeypatch):
    """默认让"装好了、配置写得进、跑完读得到结果",各用例只覆盖自己关心的那一段。"""
    monkeypatch.setattr(mc, "available", lambda: (True, "ok"))
    monkeypatch.setattr(mc, "_write_config", lambda kws: None)
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
    """全部关键词都返回 `aweme_list:[]` → 报"登录态失效",**不能安静地交 0 条**。

    实测 2026-10-03:抖音对 `网盘资源` 这种必然有结果的泛词也返回空,而 10-02 同一档案能出
    40 条 —— 那就是扫码登录过期。不报出来的话,这条链会每天安静地交出 0 条、你毫无察觉。
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
    assert "登录态失效" in str(ei.value)


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
