"""夸克**保存目录漂移**的守卫(2026-10-08)。全部离线。

## 它守的是什么(用户报的「夸克还是会保存相同的资源导致占用」)

`transfer_and_share` **本来就有文件级去重**:在目标目录里按「同名 + 同大小」找,
命中就**跳过保存、直接并入分享**。这段逻辑没坏 —— 坏的是**目标目录本身会换**。

`_ensure_dir` 原来**只创建、从不查找**(旧 docstring 把「完全不做重扫」当特性写)。
于是 fid 缓存一丢:`redian监听` 撞 `23008` → 走梯度候选 → **新建 `redian监听_MMDD`**
→ 新目录是空的 ⇒ **按目录做的查重一个都认不出来** ⇒ 同一资源又存一份。

实测代价(不是推测,是盘上数出来的):
- `redian监听_*` **19 个**,`0913 / 0915 / 0916 / 0918 / 0920 / 0926~0929 / 1001 / 1004 / 1007`
  —— 约**每 3 天**新增一个;
- `高性价比人生指南-HowToLiveBetter-现代-338页.pdf` 在 `redian监听_0929` 与
  `redian监听_1004` 里**各一份**;
- 缓存文件只有一个,且它的 mtime 正好是「换家」那一刻 ⇒ 换家与写缓存同源。

## 两个修法各自守什么
1. **`_adopt_existing_dir`**:缓存缺失时**按名搜索**找回已在用的目录(取 `updated_at`
   最新的兄弟),不新建 —— 这条与"缓存为什么丢"**无关**,丢了也不会再漂。
2. **`_persist_fids` 合并写**:原先是把构造时载入的内存快照**整份覆盖**,
   典型的丢更新 ⇒ 盘上那份正确映射被旧快照抹掉。配套 `_dropped` 墓碑:
   否则 `invalidate_dir` 刚作废的 fid 会被磁盘旧值**并回来**,作废等于没作。
"""
from __future__ import annotations

import json

import pytest

from app.services.quark_transfer import QuarkTransfer


def _dir(name: str, fid: str, updated: int, pdir: str = "0") -> dict:
    return {"file_name": name, "fid": fid, "dir": True, "updated_at": updated,
            "pdir_fid": pdir, "size": 0}


@pytest.fixture()
def store(tmp_path):
    return str(tmp_path / "quark_fid_cache.json")


def _client(store: str, hits: list[dict], created: list[str]) -> QuarkTransfer:
    """一个断网的 QuarkTransfer:搜索返回 `hits`,创建被记进 `created`。"""
    qt = QuarkTransfer("fake-cookie", fid_store=store)
    qt.search_files = lambda kw, size=20: list(hits)          # type: ignore[method-assign]
    qt._mk_dir = lambda parent, name: created.append(name) or f"NEW::{name}"  # type: ignore[method-assign]
    return qt


# ---------------------------------------------------------------------------
# ① 找回已存在的目录(核心)
# ---------------------------------------------------------------------------


def test_缓存缺失时沿用盘上最近在用的目录_而不是新建(store) -> None:
    """★★ 对着那 19 个目录加的:缓存丢了**也不许**再新建一个家。

    真实形状:`redian监听` 本身还在(只剩 4 项、停在 09-13),而最近在用的是
    `redian监听_1007`。要取**最近更新的那个**,否则后续资源会全被搬进一个早已不用的
    旧目录 —— 那等于**主动制造**下一次重复。
    """
    hits = [_dir("redian监听", "OLD", 1_791_360_000),
            _dir("redian监听_1004", "MID", 1_791_420_000),
            _dir("redian监听_1007", "NEW", 1_791_430_000)]
    created: list[str] = []
    qt = _client(store, hits, created)
    assert qt._ensure_dir("/redian监听") == "NEW"
    assert created == [], f"盘上已有目录却还是新建了:{created}"


def test_新建是最后手段_盘上没有才走到(store) -> None:
    created: list[str] = []
    qt = _client(store, [], created)
    assert qt._ensure_dir("/redian监听") == "NEW::redian监听"
    assert created == ["redian监听"]


def test_只认同一个父目录下的候选(store) -> None:
    """⚠️ 搜索是**全盘**的:别人盘里别处也可能有同名目录 —— 认错了家比新建更糟。"""
    hits = [_dir("redian监听_1007", "ELSEWHERE", 9_999_999_999, pdir="FAR_AWAY")]
    created: list[str] = []
    qt = _client(store, hits, created)
    assert qt._ensure_dir("/redian监听") == "NEW::redian监听"


def test_名字只是前缀相近的兄弟不算数(store) -> None:
    """`redian监听备份` / `redian监听旧` 不是**我们的家**,不能被认领(会串到别的资源堆里)。"""
    hits = [_dir("redian监听备份", "BK", 9_999_999_999),
            _dir("redian监听旧", "OLD2", 9_999_999_998)]
    created: list[str] = []
    qt = _client(store, hits, created)
    assert qt._ensure_dir("/redian监听") == "NEW::redian监听"


def test_文件不算目录候选(store) -> None:
    hits = [{"file_name": "redian监听_1007", "fid": "F", "dir": False,
             "updated_at": 9_999_999_999, "pdir_fid": "0", "size": 10}]
    created: list[str] = []
    qt = _client(store, hits, created)
    assert qt._ensure_dir("/redian监听") == "NEW::redian监听"


def test_搜索接口挂了要退回新建_不能把整轮转存带崩(store) -> None:
    """搜索是**冷路径上的额外一步**,它失败只该让行为回到修之前,不该抛。"""
    created: list[str] = []
    qt = _client(store, [], created)

    def _boom(*a, **k):
        raise RuntimeError("网络抖")

    qt.search_files = _boom                                  # type: ignore[method-assign]
    assert qt._ensure_dir("/redian监听") == "NEW::redian监听"
    assert created == ["redian监听"]


def test_已有的_fid_store_仍然优先_不多打一次搜索(store) -> None:
    """热路径(下轮起复用 fid)**一次搜索都不该多发** —— 那是每轮每资源都花的成本。"""
    with open(store, "w", encoding="utf-8") as f:
        json.dump({"redian监听": "CACHED"}, f)
    hits = [_dir("redian监听_1007", "NEW", 9_999_999_999)]
    created: list[str] = []
    qt = _client(store, hits, created)
    called: list[str] = []
    qt.search_files = lambda kw, size=20: called.append(kw) or list(hits)  # type: ignore[method-assign]
    assert qt._ensure_dir("/redian监听") == "CACHED"
    assert called == [], "缓存命中时不该去搜盘"


# ---------------------------------------------------------------------------
# ② 持久化:合并写 + 墓碑
# ---------------------------------------------------------------------------


def test_持久化是合并写_不抹掉别人刚写的映射(store) -> None:
    """★ 丢更新的形状:构造时载入 {a},之后**别人**又写了 {b},我们再落盘 ——
    整份覆盖会把 b 抹掉,而 b 正是"家"在哪的答案。"""
    with open(store, "w", encoding="utf-8") as f:
        json.dump({"路径A": "FID_A"}, f, ensure_ascii=False)
    qt = QuarkTransfer("fake-cookie", fid_store=store)       # 载入 {路径A}
    with open(store, "w", encoding="utf-8") as f:            # 别人(另一个实例/脚本)写入
        json.dump({"路径A": "FID_A", "路径B": "FID_B"}, f, ensure_ascii=False)
    qt._persisted["路径C"] = "FID_C"
    qt._persist_fids()
    saved = json.load(open(store, encoding="utf-8"))
    assert saved == {"路径A": "FID_A", "路径B": "FID_B", "路径C": "FID_C"}, saved


def test_invalidate_作废的_fid_不许被磁盘旧值并回来(store) -> None:
    """⚠️ 合并写的**反面**:`invalidate_dir` 的语义是"这个 fid 不要了"。
    不做墓碑的话,合并时磁盘上那条旧值会把它**原样并回来** —— 作废等于没作,
    下一次保存又拿着失效 fid 去撞错,然后新建目录(正是我们要根除的那条路)。"""
    with open(store, "w", encoding="utf-8") as f:
        json.dump({"redian监听": "STALE"}, f, ensure_ascii=False)
    qt = QuarkTransfer("fake-cookie", fid_store=store)
    qt._dir_cache["redian监听"] = "STALE"
    qt.invalidate_dir("redian监听")
    saved = json.load(open(store, encoding="utf-8"))
    assert "redian监听" not in saved, f"作废的 fid 又被并回磁盘了:{saved}"


def test_作废之后仍能重新解析出目录(store) -> None:
    """作废不是终点:下一次 `_ensure_dir` 必须能重新拿到一个可用的家。"""
    created: list[str] = []
    qt = _client(store, [_dir("redian监听_1007", "NEW", 9_999_999_999)], created)
    qt._dir_cache["redian监听"] = "STALE"
    qt.invalidate_dir("redian监听")
    assert qt._ensure_dir("/redian监听") == "NEW"
    assert created == [], "盘上明明有 `_1007`,作废后应找回它而不是新建"


def test_没有_fid_store_配置时一切照旧_不报错() -> None:
    created: list[str] = []
    qt = _client("", [], created)
    assert qt._ensure_dir("/redian监听") == "NEW::redian监听"
    qt._persist_fids()                                        # 空路径:直接返回,不许抛
