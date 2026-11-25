# -*- coding: utf-8 -*-
"""★★ 跨板块共振**降级标注**(2026-11-25,用户拍板「做降级标注」)。

## 它补的是哪个盲区

`drop_recent` 的盘链闸只认**本 section**(不同板块的群不是一批人 —— 那是故意的)。
于是同一份资源在一个板块推过之后,**换到另一个板块再推一遍完全不受拦**:
近 7 天带盘 **1106 行**里,同一盘链在 **≥2 个板块**出现的有 **94 个盘链**;
逐行按时间序算,**41.0%** 的带盘行是「它已经在别的板块推过了」。

## 为什么是**标注**,不是**拦**

用户口径:「**丢资源比重复推一遍严重得多**」。别的群收到过 ≠ 这组人收到过。
⇒ 照推,但卡面标出来,让客户自己决定这条要不要再点。

## 这里盯的几件事

1. **本板块的历史不算**(那是 `drop_recent` 的事,标在本板块卡上是噪音);
2. **窗口外的不算**(72h,与判重同一把尺子 —— 否则会出现"判重放过、标注却说推过");
3. **两个身份都认**(源链优先 ∪ 我方链;存量行只有后者,只查一个会半边瞎);
4. **没盘链的条目不标注**(没链就无从判"同一份资源");
5. **只标不拦**(条目仍在 kept 里,飞书照发)。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import models  # noqa: F401
from app.db.database import Base
from app.db.models import PushLedger, User
from app.services import push_ledger as pl

#: 真实形状(quark 分享链)。两串不同 = 两份资源
_PAN_A = "https://pan.quark.cn/s/aaaa1111"
_PAN_B = "https://pan.quark.cn/s/bbbb2222"
_MY_A = "https://pan.xunlei.com/s/OUR-aaaa1111"


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    db.add(User(id=1, username="u1", email="u1@b.c", password_hash="x", enabled=True))
    db.commit()
    yield db
    db.close()


def _seed(session, section: str, *, pan_url: str = "", pan_id: str = "",
          hours_ago: float = 2, title: str = "某资源") -> None:
    session.add(PushLedger(user_id=1, section=section, item_key=title, title=title,
                           pan_url=pan_url, pan_id=pan_id,
                           pushed_at=datetime.now() - timedelta(hours=hours_ago)))
    session.commit()


class Test不拦只标:
    def test_本板块的历史不算(self, session) -> None:
        """同一板块推过 ⇒ `drop_recent` 管;这里**不该**标(标了就是每张卡都在喊)。

        ⚠️ 口径是"**别的**板块推过",这是整个功能的分界线。
        """
        _seed(session, "tieba", pan_id=pl.pan_key(_PAN_A))
        items = [{"name": "某资源", "pan_url": _PAN_A}]
        seen = pl.cross_section_seen(session, 1, "tieba", items)
        assert seen == {}, "本板块的推送记录不是'跨板块'"
        assert pl.annotate_cross_section(items, seen) == 0
        assert "_cross_sections" not in items[0]

    def test_别的板块推过要标出来(self, session) -> None:
        _seed(session, "tieba", pan_id=pl.pan_key(_PAN_A))
        items = [{"name": "某资源", "pan_url": _PAN_A}]
        n = pl.annotate_cross_section(
            items, pl.cross_section_seen(session, 1, "xiaohongshu", items))
        assert n == 1
        assert items[0]["_cross_sections"] == ["tieba"]

    def test_标注不拦_条目仍在_kept_里(self, session) -> None:
        """★ 这是用户口径的落地检查:**同一份资源照推**,只是卡上多一句话。

        ⚠️ 如果哪天有人把 `cross_section_seen` 接进 `drop_recent`(拦),
        这条会红 —— 那个决定得重新问用户,不能顺手改。
        """
        _seed(session, "tieba", pan_id=pl.pan_key(_PAN_A))

        class _S:
            push_dedupe_hours = 24
            push_pan_dedupe_hours = 72

        items = [{"name": "某资源", "pan_url": _PAN_A}]
        kept, dropped = pl.drop_recent(session, 1, "xiaohongshu", items, _S())
        assert len(kept) == 1 and dropped == 0, "跨板块**不参与判重**(那是本板块闸门的事)"

    def test_窗口外的不算(self, session) -> None:
        """72h 之前的不算 —— 再久以前的推送对客户已经不算"同一条了"。

        ⚠️ 窗口与 `push_pan_dedupe_hours` 同一个数:两把尺子不一致就会出现
        「判重放过、标注却说推过」这种自相矛盾的卡面。
        """
        _seed(session, "tieba", pan_id=pl.pan_key(_PAN_A), hours_ago=80)
        items = [{"name": "某资源", "pan_url": _PAN_A}]
        assert pl.cross_section_seen(session, 1, "xiaohongshu", items) == {}

    def test_窗口可以显式放宽(self, session) -> None:
        _seed(session, "tieba", pan_id=pl.pan_key(_PAN_A), hours_ago=80)
        items = [{"name": "某资源", "pan_url": _PAN_A}]
        seen = pl.cross_section_seen(session, 1, "xiaohongshu", items, hours=100)
        assert sorted(seen.values())[0] == ["tieba"]


class Test身份:
    def test_源链身份能认(self, session) -> None:
        """新行都有 `pan_id`(源链优先)⇒ 必须查它。"""
        _seed(session, "kuaishou", pan_id=pl.pan_key(_PAN_A))
        items = [{"name": "某资源", "src_pan_url": _PAN_A}]
        assert pl.annotate_cross_section(
            items, pl.cross_section_seen(session, 1, "douyin_peer", items)) == 1

    def test_我方链身份也能认(self, session) -> None:
        """存量行只有 `pan_url`(我方链)⇒ 只查 `pan_id` 会半边瞎。"""
        _seed(session, "wechat", pan_url=_MY_A)
        items = [{"name": "某资源", "pan_url": _MY_A}]
        assert pl.annotate_cross_section(
            items, pl.cross_section_seen(session, 1, "bilibili", items)) == 1

    def test_假形状的_link_也要认(self, session) -> None:
        """`resource_presence` 把链藏在 `link: {...}` 里(它每行都这么给)。"""
        _seed(session, "tieba", pan_id=pl.pan_key(_PAN_A))
        items = [{"name": "某资源", "link": {"my_link": _PAN_A}}]
        assert pl.annotate_cross_section(
            items, pl.cross_section_seen(session, 1, "zhihu", items)) == 1

    def test_两个身份命中取并集(self, session) -> None:
        """同一条目源链在 A 板块、我方链在 B 板块 ⇒ 两个都算「它去过的地方」。"""
        _seed(session, "tieba", pan_id=pl.pan_key(_PAN_A))
        _seed(session, "wechat", pan_url=_MY_A)
        items = [{"name": "某资源", "pan_url": _MY_A, "src_pan_url": _PAN_A}]
        n = pl.annotate_cross_section(
            items, pl.cross_section_seen(session, 1, "kuaishou", items))
        assert n == 1
        assert sorted(items[0]["_cross_sections"]) == ["tieba", "wechat"]

    def test_没盘链的条目不标注(self, session) -> None:
        """没链就无从判"同一份资源" —— 宁可不标,也不猜。"""
        _seed(session, "tieba", pan_id=pl.pan_key(_PAN_A))
        items = [{"name": "一篇没带盘的公众号文章", "url": "https://mp.weixin.qq.com/s/x"}]
        assert pl.annotate_cross_section(
            items, pl.cross_section_seen(session, 1, "kuaishou", items)) == 0
        assert "_cross_sections" not in items[0]

    def test_别的租户的行不算(self, session) -> None:
        """台账按 `user_id` 隔离 —— 别人的推送不能算到我们头上。"""
        session.add(PushLedger(user_id=2, section="tieba", item_key="x", title="x",
                               pan_id=pl.pan_key(_PAN_A), pushed_at=datetime.now()))
        session.commit()
        items = [{"name": "某资源", "src_pan_url": _PAN_A}]
        assert pl.cross_section_seen(session, 1, "kuaishou", items) == {}


class Test文案:
    def test_板块名走中文(self) -> None:
        note = pl.cross_section_note(["tieba", "xiaohongshu"])
        assert "已在 2 个板块推荐" in note
        assert "贴吧" in note and "小红书" in note
        assert "tieba" not in note, "英文键不能漏进客户卡"

    def test_没板块就空串(self) -> None:
        assert pl.cross_section_note([]) == ""
        assert pl.cross_section_note(None or []) == ""

    def test_文案里不许出现判断词(self) -> None:
        """口径:是**提示**,不是认错 —— 别写"重复"。"""
        note = pl.cross_section_note(["tieba"])
        assert "重复" not in note


class Test边界:
    def test_空条目表直接返回(self, session) -> None:
        assert pl.cross_section_seen(session, 1, "tieba", []) == {}
        assert pl.annotate_cross_section([], {}) == 0

    def test_会话为空不抛(self) -> None:
        """旁路不许把推送打挂 —— 查库失败/无会话时静默返回空。"""
        assert pl.cross_section_seen(None, 1, "tieba", [{"pan_url": _PAN_A}]) == {}

    def test_非_dict条目跳过(self, session) -> None:
        assert pl.annotate_cross_section(["x", None, 3], {}) == 0


# ------------------------------------------------------------------ 源码级守卫

_PUSH_FILES = (
    "app/services/resource_presence.py",
    "app/services/douyin_peer_scan.py",
    "app/services/xunlei_group.py",
    "app/services/wechat/_listen.py",
)


def _calls_in(fn) -> dict[str, list[int]]:
    """函数体里 `模块.函数名(...)` 形式的调用 → {函数名: [行号, …]}。"""
    import ast

    out: dict[str, list[int]] = {}
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
        if name:
            out.setdefault(name, []).append(node.lineno)
    return out


def test_先挡后标_标注必须排在去重之后() -> None:
    """★ **顺序错了会白算**:被本板块闸门挡掉的条文根本不上卡。

    而且在 `drop_recent` 之前标注意味着"本板块刚推过的也会被算进来"
    (那部分本板块历史应归闸门管,不该出现在跨板块口径里)。
    这条守卫按 **AST 行号**比,不按源码子串 —— 注释里写着调用名不算数。
    """
    import ast
    from pathlib import Path

    bad: list[str] = []
    seen = 0
    for rel in _PUSH_FILES:
        tree = ast.parse(Path(rel).read_text(encoding="utf-8"), filename=rel)
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            calls = _calls_in(node)
            ann = calls.get("annotate_cross_section") or []
            drop = calls.get("drop_recent") or []
            if not ann:
                continue
            seen += 1
            if not drop:
                bad.append(f"{rel}::{node.name} 标注了却**没接**去重(先挡后标)的调用")
                continue
            if min(ann) < min(drop):
                bad.append(f"{rel}::{node.name} 标注在第 {min(ann)} 行、去重在第 {min(drop)} 行")
    assert seen >= 4, f"接入点变少了(实测 {seen} 个),是不是被谁顺手删了"
    assert not bad, "跨板块标注必须在盘链闸之后:\n  " + "\n  ".join(bad)
