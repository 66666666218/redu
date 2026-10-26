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
