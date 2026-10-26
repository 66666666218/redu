"""吃瓜资料发布(夸克上传+建链)单测(2026-10-25)。**不联网、不开浏览器**。

只验最容易出错的那一小块:`_find_file` 的两条查询路径。
2026-10-25 实测过的坑:文件**已经**传上去了,而 `search_files`(搜索索引)120 秒都没命中,
`list_recent` 却立刻看得到 ⇒ 只查搜索会得出"没传上去"的**假结论**。
"""
import importlib.util
import os
from pathlib import Path

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")


def _mod():
    p = Path(__file__).resolve().parents[1] / "scripts" / "gossip_publish.py"
    spec = importlib.util.spec_from_file_location("gossip_publish", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class _Client:
    def __init__(self, search=None, recent=None, search_raises=False):
        self._search, self._recent, self._raises = search or [], recent or [], search_raises

    def search_files(self, name, size=20):
        if self._raises:
            raise RuntimeError("搜索索引超时")
        return self._search

    def list_recent(self, size=30):
        return self._recent


def test_搜索命中就直接返回():
    gp = _mod()
    got = gp._find_file(_Client(search=[{"file_name": "a.pdf", "fid": "1"}]), "a.pdf")
    assert got == {"name": "a.pdf", "fid": "1"}


def test_搜索没命中要退回最近列表_实测索引有延迟():
    gp = _mod()
    got = gp._find_file(_Client(search=[], recent=[{"file_name": "a.pdf", "fid": "2"}]),
                        "a.pdf")
    assert got == {"name": "a.pdf", "fid": "2"}, "只查搜索会误判成「没传上去」"


def test_搜索抛错也要退回最近列表():
    gp = _mod()
    got = gp._find_file(_Client(recent=[{"file_name": "a.pdf", "fid": "3"}],
                                search_raises=True), "a.pdf")
    assert got == {"name": "a.pdf", "fid": "3"}


def test_两条路都没有就返回空():
    gp = _mod()
    assert gp._find_file(_Client(), "a.pdf") is None


def test_目录不算文件():
    """`list_recent` 里可能混着目录(同名目录不该被当成要分享的文件)。"""
    gp = _mod()
    assert gp._find_file(_Client(recent=[{"file_name": "a.pdf", "fid": "9", "dir": True}]),
                         "a.pdf") is None


def _sent(monkeypatch):
    from app.services import feishu_client

    box: dict = {}

    class _C:
        def __init__(self, hook, secret):
            self.hook = hook

        def send(self, text):
            box["hook"] = self.hook
            box["text"] = text
            return True

    monkeypatch.setattr(feishu_client, "FeishuClient", _C)
    return box


class _S:
    feishu_webhook_admin = "hook-admin"
    feishu_webhook_douyin_peer = "hook-dy"
    feishu_secret = ""


def test_发群文案必须带AI标识(monkeypatch):
    """★ 用户口径:「提醒用户是 AI 创作的就可以了」。收的人有权知道,所以**写死在文案里**。"""
    from app.services.gossip_doc import AI_NOTICE

    gp = _mod()
    box = _sent(monkeypatch)
    gp.notify(_S(), "某热点", "https://pan.quark.cn/s/x", "1234", "a.pdf")
    assert "AI" in box["text"] and AI_NOTICE in box["text"], box["text"]
    assert "1234" in box["text"], "有提取码就要写出来"
    assert box["hook"] == "hook-admin", "默认发管理群"


def test_可以指定发到客户群(monkeypatch):
    gp = _mod()
    box = _sent(monkeypatch)
    gp.notify(_S(), "某热点", "https://pan.quark.cn/s/x", "", "a.pdf", to="douyin_peer")
    assert box["hook"] == "hook-dy", "指定哪个群就发哪个群"


def test_没配置的群要报错_不许静默发到别处(monkeypatch):
    gp = _mod()
    _sent(monkeypatch)
    try:
        gp.notify(_S(), "某热点", "https://pan.quark.cn/s/x", "", "a.pdf", to="不存在的群")
    except gp.PublishError as exc:
        assert "没有配置" in str(exc)
    else:
        raise AssertionError("发到不存在的群必须报错")


def test_按来源平台路由_哪个平台的热点就发哪个群():
    """用户口径:「哪个平台的热点信息写的就发哪个群里」。

    ⚠️ **认不出来时走全平台群,不许静默丢弃** —— 发错群比不发更糟,
    但"悄悄不发"同样糟(你会以为发了)。
    """
    gp = _mod()
    assert gp.resolve_targets(["auto"], "微博") == ["weibo_peer"]
    assert gp.resolve_targets(["auto"], "抖音") == ["douyin_peer"]
    assert gp.resolve_targets(["auto"], "百度") == ["baidu"]
    assert gp.resolve_targets(["auto"], "小红书") == ["multiplatform"], "认不出来要给个兜底"
    assert gp.resolve_targets(["auto"], "") == ["multiplatform"]


def test_可以一次发多个群_并去重保序():
    gp = _mod()
    assert gp.resolve_targets(["douyin_peer", "weibo_peer"], "") == ["douyin_peer", "weibo_peer"]
    assert gp.resolve_targets(["a,b", "b"], "") == ["a", "b"], "逗号+重复要去重"
    assert gp.resolve_targets([], "") == ["admin"], "不指定默认发管理群"


def test_夸克的同名_N_后缀也算同一份():
    """⚠️ 实测:夸克遇到同名会存成 `名字(1).pdf`。只按原名精确匹配 ⇒ 认不出刚传的那份
    ⇒ 误报"没传上去"然后**再传一份**(越传越多)。"""
    gp = _mod()
    assert gp._same_doc("a.pdf", "a.pdf")
    assert gp._same_doc("a(1).pdf", "a.pdf")
    assert gp._same_doc("a(12).pdf", "a.pdf")
    assert not gp._same_doc("b.pdf", "a.pdf")
    assert not gp._same_doc("a.pdf", "")


class _ClientUploadDir:
    """模拟真实盘:根目录只有**目录**,文件落在「夸克上传文件」子目录里。"""

    def __init__(self):
        self.list_dir_calls = []

    def search_files(self, name, size=20):
        return []                                   #: 搜索索引对刚传的文件不命中(实测)

    def list_recent(self, size=30):
        return [{"file_name": "夸克上传文件", "fid": "dir-upload", "dir": True},
                {"file_name": "redian监听", "fid": "dir-2", "dir": True}]

    def list_dir(self, fid):
        self.list_dir_calls.append(fid)
        return [{"file_name": "a.pdf", "fid": "F1", "dir": False},
                {"file_name": "a(1).pdf", "fid": "F2", "dir": False}]


def test_网页上传落在子目录也要能找到():
    """★ 2026-10-26 实测踩到的假结论:文件**传上去了**,但落在「夸克上传文件」子目录;
    而搜索索引不命中、`list_recent` 只列顶层目录 ⇒ 我们白等 120 秒,以为上传失败。"""
    gp = _mod()
    c = _ClientUploadDir()
    got = gp._find_file(c, "a.pdf")
    assert got and got["fid"] in ("F1", "F2"), got
    assert c.list_dir_calls == ["dir-upload"], "必须去上传目录里找"


def test_浏览器档案必须是绝对路径():
    """★★ 2026-10-26 血的教训:`--user-data-dir` 用**相对路径**时,Edge 会**退回默认档案**,
    于是它"加入"用户正在用的那个浏览器、在里面开 about:blank,而我们的端口永远不就绪
    (受控对照:相对 FAIL / 绝对 OK)。

    ⚠️ 这个坑的代价不只是脚本失败 —— 它会**动到用户自己的浏览器**。所以钉死:
    常量必须是绝对路径。
    """
    gp = _mod()
    assert gp.PROFILE.is_absolute(), f"PROFILE 必须是绝对路径:{gp.PROFILE}"
