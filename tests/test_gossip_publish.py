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
