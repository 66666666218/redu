# -*- coding: utf-8 -*-
"""爆款预判(2026-11-24)的守卫。

## 这条功能要守的是什么

用户口径:「麦穗和羊我们**最早 15 号就看到了**,但 **17 号才开始监控到发网盘** ——
能否**自己提前预判适合做网盘的热点**、自己写?**16 号发就会占很大先机**」。

所有判据都拿 11-15~11-23 的真实库回放过(出处见 `app/services/hot_forecast.py` 头注),
本文件把它们钉成回归 —— 尤其是**那几条被实测挡掉的噪音**,它们才是这条判据能不能用的关键:
`BLG拿资源输团战` / `泛黄手写信` / `马天宇…情感链接` 都曾因宽词混进来;
`谷雨` 2828 万却只有 1 倍自身基线(常驻短剧)。

## 本文件一律**不触网**
`detect` 的 `candidates`/`confirms`/`scores`/`degraded` 四个注入点就是为这件事留的。
"""
import os
from datetime import datetime

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.db import models  # noqa: E402,F401  (建表要先注册全部模型)
from app.db.database import Base  # noqa: E402
from app.db.models import User  # noqa: E402
from app.services import hot_forecast as hf  # noqa: E402

NOW = datetime(2026, 11, 16, 8, 49)


class _S:
    """够 `detect` 用的最小 settings(全部走模块默认值)。"""
    hot_forecast_lookback_hours = 24
    hot_forecast_min_last = 5_000_000
    #: ⚠️ 2026-11-25 由 30.0 放宽到 10.0(依据 `scripts/backtest_hot_forecast.py`)
    hot_forecast_min_ratio = 10.0
    hot_forecast_min_points = 2
    hot_forecast_min_score = 6
    hot_forecast_degraded_rank_top = 10
    hot_forecast_max_peer_authors = 0
    hot_forecast_top = 3
    hot_forecast_cooldown_hours = 20
    hot_forecast_materials = False
    deepseek_api_key = ""
    feishu_webhook_admin = "https://example.invalid/hook"
    feishu_secret = ""


def _c(title, *, srcs="百度", rank=26):
    return {"title": title, "srcs": srcs, "rank": rank, "first": NOW}


def _confirm(word="麦穗与羊", last=30_928_492, ratio=87.0, days=14, points=5):
    return {"word": word, "last": last, "ratio": ratio, "days": days,
            "points": points, "ok": True}


def _run(*, candidates, confirms=None, scores=None, degraded=None, settings=None):
    stats: dict = {}
    out = hf.detect(settings or _S(), now=NOW, candidates=list(candidates),
                    confirms=confirms if confirms is not None else {},
                    scores=scores if scores is not None else {},
                    degraded=degraded, stats=stats)
    return out, stats


MAISUI = _c("文章《麦穗与羊》爆火 作者发声", rank=26)


class Test形态与作品名:
    def test_麦穗那条要精确命中(self) -> None:
        """★ 整件事的锚点:11-16 08:49 跑一次,应当只报出「麦穗与羊」。"""
        out, stats = _run(candidates=[MAISUI],
                          confirms={"麦穗与羊": _confirm()})
        assert [a["seed"] for a in out] == ["麦穗与羊"], out
        assert out[0]["douhot"]["last"] == 30_928_492
        assert out[0]["rank"] == 26, "预告型候选不看名次(麦穗当天百度只有 #26)"
        assert stats["form"] == 1 and stats["seeds"] == 1

    def test_宽词不命中(self) -> None:
        """★★ 这三条是**实测混进来的噪音**,`资源/手写/情感` 已从词表剔除。

        加回去的话,每天的候选会从「3~4 条」涨到几十条,卡就没法看了。
        """
        out, stats = _run(candidates=[
            _c("BLG拿资源输团战", rank=3),
            _c("泛黄手写信", rank=8),
            _c("马天宇承认不需要任何情感链接", rank=12),
        ], confirms={})
        assert out == [], out
        assert stats["form"] == 0

    def test_纯品类词要丢(self) -> None:
        """`《短剧》完整版` 说的是**品类**不是作品 —— 热点宝里这种词天天有量。"""
        out, stats = _run(candidates=[_c("《短剧》完整版上线"), _c("小说合集")],
                          confirms={})
        assert out == [], out

    def test_同一事件两种说法只算一条(self) -> None:
        """同一条标题在多个 `user_id` / 多个源下都有一行 ⇒ **一条**候选。

        ⚠️ 远程库的 `GROUP BY title` 在 SQL 层做;这里是第二道:即使上游没去重,
        进 `groups` 之后也按作品名并成一条(名次取最好的那个)。
        """
        out, stats = _run(candidates=[
            _c("《麦穗与羊》凄美爱情故事原文", srcs="贴吧", rank=3),
            _c("《麦穗与羊》凄美爱情故事原文", srcs="贴吧", rank=3),
            _c("文章《麦穗与羊》爆火 作者发声", srcs="百度", rank=26),
        ], confirms={"麦穗与羊": _confirm()})
        assert [a["seed"] for a in out] == ["麦穗与羊"], out
        assert out[0]["srcs"] == {"贴吧", "百度"}, "跨源要合并(说明不止一个平台在报)"
        assert out[0]["rank"] == 3, "名次取最好的那个"

    def test_没有形态词就不进(self) -> None:
        """`麦穗与羊辟谣` 没东西可交付 ⇒ 不进(它不是一份资源)。"""
        out, stats = _run(candidates=[_c("麦穗与羊辟谣")], confirms={})
        assert out == [] and stats["form"] == 0


class Test趋势确认:
    def test_谷雨一倍基线要挡掉(self) -> None:
        """★ 2828 万听着很吓人,但它**只有 1 倍自身基线** —— 那是常驻短剧,不是"要炸"。

        ⚠️ 2026-11-25 倍数从 30 放宽到 10 之后,这条**仍然是守卫的重点**:
        噪音离 10 倍极远(实测 `丑狐` 1.3x / `我的有罪之人` 1.7x / `谷雨` 1.0x),
        所以放宽并没有把它们放进来。这条测试钉的就是「放宽 ≠ 放噪音」。
        """
        out, stats = _run(
            candidates=[_c("谷雨短剧完整版")],
            confirms={},                      # 热点宝**可达**、只是没达标
        )
        assert out == [], out
        assert not stats.get("douhot_down"), "「没达标」不能算成「热点宝故障」"

    def test_常驻剧低倍数要挡掉(self) -> None:
        """★★ 放宽到 10 倍之后新加的守卫:这三条是 11-25 实测被挡的真实候选。

        它们的单日量都不低(2203 万 / 3673 万),**只有倍数能救** ——
        如果哪天有人把 `min_ratio` 再往下调,这条会立刻红。
        """
        out, _ = _run(candidates=[
            _c("丑狐大结局"), _c("我的有罪之人完整版"), _c("吧友锐评GTA6大结局"),
        ], confirms={})
        assert out == [], out

    def test_麦穗30倍在放宽后必须过(self) -> None:
        """★ 放宽的**全部收益**就是这一条。

        按线上口径(只截到昨天)重放,麦穗 11-16 那一刻只有 **30.0 倍** ——
        在旧阈值 30x 下是贴边过线,**只要数据抖一点就会被挡**。
        模块头注里那个「87x」是事后用完整序列算的,决策当时看不到。
        """
        out, _ = _run(candidates=[MAISUI], confirms={"麦穗与羊": _confirm(ratio=30.0)})
        assert [a["seed"] for a in out] == ["麦穗与羊"], out

    def test_热点宝不可达才降级且只看靠前的(self) -> None:
        """★ 不可达时**不静默**:
        ① 只收名次 ≤10 的(宁缺勿滥);② 结果里 `douhot` 为 None,卡上会写「未确认」。
        """
        out, stats = _run(candidates=[
            _c("《饥困荒野》完整视频流出", srcs="贴吧", rank=5),
            _c("《后会友期》完整版", srcs="百度", rank=26),
        ], confirms={}, degraded=True)
        assert [a["seed"] for a in out] == ["饥困荒野"], out
        assert out[0]["douhot"] is None
        txt = hf._render(out, chr(10))
        assert "未确认" in txt, txt


class TestAI判价值:
    def test_低分被挡(self) -> None:
        """`行业话题` 实测 4 分、`数据新闻` 3 分 —— 都不该糊到运营眼前。"""
        out, stats = _run(candidates=[MAISUI], confirms={"麦穗与羊": _confirm()},
                          scores={"麦穗与羊": {"score": 3, "why": "纯资讯"}})
        assert out == [], out
        assert stats["ai_low"] == 1

    def test_AI没判要放行(self) -> None:
        """★ 与 `ai_filter` 纪律 1 同:模型挂了**不许静默丢** —— 放行并让卡片没有分数行。"""
        out, _ = _run(candidates=[MAISUI], confirms={"麦穗与羊": _confirm()}, scores={})
        assert [a["seed"] for a in out] == ["麦穗与羊"], out
        assert out[0]["score"] is None
        assert "AI 价值" not in hf._line_of(out[0]), "没判就不该写一个假分数"


class Test卡片口径:
    def test_必须写库内与预判(self) -> None:
        """★ 三个口径词一个都不能少:
        「预判」(不是确认)/「库内」(我们只看得到自己库里)/「未上传」(盘满是事实)。
        """
        out, _ = _run(candidates=[MAISUI], confirms={"麦穗与羊": _confirm()})
        out[0]["peer_authors"] = 0
        txt = hf._render(out, chr(10))
        for word in ("爆款预判", "库内", "未上传", "麦穗与羊", "3093 万", "87 倍"):
            assert word in txt, f"卡片缺「{word}」:{txt}"


# ---------------------------------------------------------------- tick 级守卫

@pytest.fixture
def session():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)()
    db.add(User(id=1, username="u1", email="u1@b.c", password_hash="x", enabled=True))
    db.commit()
    yield db
    db.close()


def _tick(session, monkeypatch, *, send_ok: bool):
    """把出网/慢活全替掉,只留「冷却 / 发卡 / 记录」这段真代码。"""
    sent_alerts = [{"seed": "麦穗与羊", "title": "文章《麦穗与羊》爆火", "srcs": {"百度"},
                    "rank": 26, "douhot": _confirm(), "score": 7, "why": "有原文"}]
    marked: list[str] = []
    monkeypatch.setattr(hf, "detect", lambda *a, **k: sent_alerts)
    monkeypatch.setattr(hf, "_enrich_peer",
                        lambda alerts, uid: [a.update(peer_authors=0, owned=False)
                                             for a in alerts])
    monkeypatch.setattr(hf, "prepare", lambda *a, **k: {"pdf": "", "article": "", "note": ""})
    monkeypatch.setattr(hf, "_mark_alerted",
                        lambda s, uid, key, cd, why: marked.append(key))
    monkeypatch.setattr(hf, "_record", lambda *a, **k: None)
    #: `get_session_local` 是在函数**内部** import 的 ⇒ patch 到 `app.db` 那个名字上
    monkeypatch.setattr("app.db.get_session_local", lambda: (lambda: session))

    class _Client:
        def __init__(self, *a, **k):
            pass

        def send(self, text):
            return send_ok

    monkeypatch.setattr("app.services.feishu_client.FeishuClient", _Client)
    return hf.hot_forecast_tick(_S()), marked


class Test冷热:
    def test_发送失败不落冷却(self, session, monkeypatch) -> None:
        """★ 失败还落冷却 = 这条**永久丢失**(20h 内不再报)。只在成功后落。"""
        n, marked = _tick(session, monkeypatch, send_ok=False)
        assert n == 0 and marked == [], f"发送失败却落了冷却:{marked}"

    def test_发送成功才落冷却(self, session, monkeypatch) -> None:
        n, marked = _tick(session, monkeypatch, send_ok=True)
        assert n == 1
        assert marked == ["forecast:麦穗与羊"], marked