"""链路全检脚本(2026-10-04)。

它存在的理由:用户要「链路都跑通之后再来完善 agent」——
所以在完善 agent **之前**,得先有一个**可重复跑**的东西回答"每条链到底通不通",
而不是靠人回忆。这里钉住的是它的**汇报口径**:红/黄/绿的判据要能自己复核。
"""
import os

import pytest

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

from app.services.chain_health import CHAINS, GREEN, RED, YELLOW, render  # noqa: E402


def test_render_counts_and_names_the_red_ones() -> None:
    """★ 红项要**点名** —— 体检报告的价值全在"先修哪个"这一句上。"""
    sections = [("依赖", [{"name": "WeRSS", "level": GREEN, "detail": "HTTP 200"},
                          {"name": "newsnow", "level": RED, "detail": "容器不存在"}]),
                ("产出", [{"name": "监听", "level": YELLOW, "detail": "13h 前"}]),
                ("凭证", [{"name": "迅雷", "level": GREEN, "detail": "0 天前"}])]
    txt = render(sections)
    assert "🔴 1" in txt and "🟡 1" in txt and "🟢 2" in txt
    assert "先修红的:newsnow" in txt
    assert all(t in txt for t in ("【依赖】", "【产出】", "【凭证】"))


def test_render_without_red_omits_the_fix_line() -> None:
    txt = render([("依赖", [{"name": "A", "level": GREEN, "detail": "ok"}])])
    assert "先修红的" not in txt


def test_every_chain_declares_how_to_judge_it() -> None:
    """每条链都要声明"用哪个字段判产出" —— 判据是体检的灵魂。

    ⚠️ `metric=None` 是**允许**的(有些链没有产出计数),但不能全是 None,
    否则这个体检就退化成了"只看有没有跑过",看不出"跑了但什么都没产出"。
    """
    assert len(CHAINS) >= 6
    assert any(m for _l, _k, m, _d in CHAINS), "至少要有一条链声明了产出字段"
    for entry in CHAINS:
        label, kind, metric, desc = entry[0], entry[1], entry[2], entry[3]
        assert label and kind and desc, (label, kind, desc)
        if metric:
            assert isinstance(metric, str) and "=" not in metric, metric
        # 侧标记只允许 local/remote(远程侧**不能按本地标准判红** —— 两库独立)
        assert (entry[4] if len(entry) > 4 else "local") in ("local", "remote")


def test_newsnow_is_not_red_on_a_wechat_only_instance(monkeypatch) -> None:
    """★ **依赖检查要按实例角色判断**。

    newsnow 只服务于 `hot_source`(role=**hotspot**),而本机是 role=**wechat**
    ⇒ **本来就不该有它**,报红是**误报**。
    ⚠️ 一条永远红的项会训练人忽略整份报告 —— 与"闸门失准"告警是同一条教训
    (那时也是"把不该报的报了",导致真问题淹在噪音里)。
    """
    import config.settings as cs

    from app.services import chain_health as ch

    monkeypatch.setattr(cs, "get_settings",
                        lambda: type("S", (), {"scheduler_role": "wechat"})())
    row = [r for r in ch.check_dependencies() if "newsnow" in r["name"]][0]
    assert row["level"] == GREEN, f"wechat 侧不该报红,实际 {row}"
    assert "不该跑热榜" in row["detail"]


def test_newsnow_is_checked_on_a_hotspot_instance(monkeypatch) -> None:
    """但**热点侧就必须真查** —— 那边它是要用的,缺了就该红/黄。"""
    import config.settings as cs

    from app.services import chain_health as ch

    monkeypatch.setattr(cs, "get_settings",
                        lambda: type("S", (), {"scheduler_role": "hotspot"})())
    row = [r for r in ch.check_dependencies() if "newsnow" in r["name"]][0]
    assert row["level"] in (RED, YELLOW), f"hotspot 侧必须真查,实际 {row}"


def test_remote_side_chain_is_not_flagged_red_locally(session) -> None:
    """★ **远程侧的链不能按本地标准判红**(2026-10-05 补)。

    两库独立 —— 本机**本来就查不到**远程作业的运行记录,报红就是**误报**。
    而"一条永远红的项会训练人忽略整份报告"(与"闸门失准"告警同一条教训:
    那次也是把不该报的报了,真问题淹在噪音里)。
    """
    from app.services import chain_health as ch

    rows = ch.check_chains(session, days=3)
    agent = [r for r in rows if "Agent" in r["name"]]
    assert agent, "热点选题 Agent 应在体检表里"
    assert agent[0]["level"] != ch.RED, "远程侧不该判红"
    assert "远程" in agent[0]["detail"]

@pytest.fixture()
def session():
    """这个脚本的 `check_chains` 要查库(虽然只读)。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db import models as _models      # 建表要靠它把模型注册进 metadata
    from app.db.database import Base

    assert _models is not None

    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    yield db
    db.close()
