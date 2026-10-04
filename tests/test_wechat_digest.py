"""公众号板块:按阅读数总结 + 闭环体检(2026-10-04)。

钉住的是**最容易变成"假信号"的两处**:
  ① 体检的 stale 判据 —— 解析不出字段时**不许**下"连续 0"的结论;
  ② 总结必须报**阅读数覆盖率** —— 阅读数受列表额度限制(每轮 25 个号、~2 天一轮),
     "某些文章 read_num=0"是设计取舍,不报覆盖率就会被读成"没人看"。
"""
import os
from datetime import datetime, timedelta

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.db.database import Base  # noqa: E402
from app.db.models import RunRecord, WechatArticle  # noqa: E402
from app.services import wechat_digest as wd  # noqa: E402


@pytest.fixture()
def session():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()


_SEQ = {"n": 0}


def _run(session, kind: str, detail: str, status: str = "success", n: int = 1) -> None:
    for _ in range(n):
        _SEQ["n"] += 1
        session.add(RunRecord(user_id=1, kind=kind, status=status, detail=detail,
                              run_id=f"r{_SEQ['n']}", started_at=datetime.now()))
    session.commit()


def _art(session, author: str, read: int, days_ago: int = 1) -> None:
    session.add(WechatArticle(user_id=1, author=author, title="t", url="u",
                              read_num=read,
                              created_at=datetime.now() - timedelta(days=days_ago)))
    session.commit()


# ---------------------------------------------------------------- ① 解析
def test_detail_int_returns_none_not_zero_when_absent() -> None:
    """⚠️ "没这个字段" ≠ "产出是 0" —— 混在一起会把"格式变了"误读成"没产出"。"""
    assert wd._detail_int("accounts=51 new=17 failed=0", "new") == 17
    assert wd._detail_int("terms=5 new=0 blocked=2", "new") == 0
    assert wd._detail_int("terms=5 new=0", "listenable") is None      # 字段不在
    assert wd._detail_int("", "new") is None
    # 不许被别的键的前缀骗到
    assert wd._detail_int("renewal=3", "new") is None


# ---------------------------------------------------------------- ② 闭环体检
def test_pipeline_health_flags_consecutive_zero(session) -> None:
    """三段全跑过且**每段最近三次都解析得出且全为 0** → stale(要么正常要么断链,看一眼)。"""
    _run(session, "wechat_candidates", "terms=5 new=0 blocked=0", n=3)
    _run(session, "wechat_candidate_import", "picked=13 done=8 listenable=0", n=3)
    _run(session, "wechat_listen", "accounts=51 new=0 failed=0", n=3)
    hs = wd.pipeline_health(session, 1)
    assert [h["stage"] for h in hs] == ["① 发现候选号", "② 收录成对标号", "③ 监听采文章"]
    assert all(h["stale"] for h in hs), hs
    assert all("连续 3 次产出为 0" in h["note"] for h in hs), hs


def test_pipeline_health_says_never_ran_not_stale(session) -> None:
    """从没跑过是**另一类问题**(调度没注册),不许混进"连续 0"。"""
    hs = wd.pipeline_health(session, 1)
    assert all(h["last_at"] is None and h["stale"] is False for h in hs)
    assert all("从没跑过" in h["note"] for h in hs)


def test_pipeline_health_refuses_to_conclude_when_format_changed(session) -> None:
    """★ **解析不出字段时不许下结论** —— 那更可能是 detail 格式变了。
    与 `_should_have_fired` 的保守分支同源:宁可漏报,不可误报。"""
    _run(session, "wechat_candidate_import", "picked=8 收录=0", n=3)   # 没有 listenable=
    h = [x for x in wd.pipeline_health(session, 1) if x["kind"] == "wechat_candidate_import"][0]
    assert h["stale"] is False
    assert "解析不出" in h["note"]

    # 半新半旧(前两次有字段、第三次没有)→ 也不下结论
    session.query(RunRecord).delete()
    session.commit()
    _run(session, "wechat_candidates", "terms=5 new=0", n=2)
    _run(session, "wechat_candidates", "terms=5", n=1)
    h = [x for x in wd.pipeline_health(session, 1) if x["kind"] == "wechat_candidates"][0]
    assert h["stale"] is False and "暂不下" in h["note"]


def test_pipeline_health_not_stale_when_there_is_output(session) -> None:
    """有产出就正常,不该报。"""
    _run(session, "wechat_listen", "accounts=51 new=17 failed=0", n=3)
    h = [x for x in wd.pipeline_health(session, 1) if x["kind"] == "wechat_listen"][0]
    assert h["stale"] is False and h["values"] == [17, 17, 17]


# ---------------------------------------------------------------- ③ 按阅读数总结
def test_read_summary_reports_coverage_and_uses_single_source(session) -> None:
    """★ **覆盖率必须报出来**;预估拉新量必须走 `conversion`(不许在这儿再写一遍 0.3)。"""
    _art(session, "小小栀颜", 1151)
    _art(session, "小小栀颜", 682)
    _art(session, "墨滴", 183)
    _art(session, "没阅读数的号", 0)          # 窗口外的号:read_num=0 是设计取舍
    s = wd.read_summary(session, 1)
    assert s["articles"] == 4 and s["with_read"] == 3
    assert s["total_reads"] == 1151 + 682 + 183
    # 单一事实源:0.30 只该写在 conversion 里
    from app.services import conversion
    assert s["estimate"] == conversion.estimate("wechat", {"read_num": s["total_reads"]})["estimate"]
    assert "阅读数" in s["estimate_detail"]


def test_read_summary_ignores_old_articles(session) -> None:
    """只统计窗口内的文章。"""
    _art(session, "A", 999, days_ago=30)
    _art(session, "B", 100, days_ago=1)
    assert wd.read_summary(session, 1, days=7)["total_reads"] == 100


def test_digest_warns_when_no_read_count_at_all(session) -> None:
    """★ 有文章但**一篇阅读数都没读到** = 轮转可能没在工作 —— 必须显式告警,
    否则会被当成"这些文章都没人看"。"""
    _art(session, "A", 0)
    _art(session, "B", 0)
    txt = wd.build_digest(session, 1)
    assert "有阅读数的 0/2" in txt
    assert "一篇都没读到阅读数" in txt
    assert "列表额度轮转" in txt


def test_digest_labels_the_estimate_as_demand_side(session) -> None:
    """⚠️ 预估乘的是**对标号**的阅读数 ⇒ 必须写明"不是我方实收",别被当结算真值。"""
    _art(session, "A", 1000)
    txt = wd.build_digest(session, 1)
    assert "不是我方实收" in txt
    assert "阅读数×30%" in txt
