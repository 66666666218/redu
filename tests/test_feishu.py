"""飞书日报/实时提醒单测:签名算法、批次涨跌判定、日报文本、实时去重。

全部不联网:只测数据推演与签名计算,webhook 发送用 Fake 客户端替身。
"""
import os

os.environ.setdefault("JWT_SECRET", "test_secret_0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import base64
import hashlib
import hmac
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.db import models  # noqa: F401
from app.db.models import DouhotWord, FeishuAlert, User, WeiboHotItem, XianyuItem
from app.services import feishu, feishu_client
from app.services.feishu import _delta, build_daily, run_feishu_keyword_alerts, run_feishu_realtime
from app.services.feishu_client import _sign

SECRET = "test-sign-secret-placeholder"


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    user = User(username="t", password_hash="x")
    db.add(user)
    db.commit()
    db.refresh(user)
    yield db
    db.close()


def _settings(**kw):
    from config.settings import Settings

    return Settings(
        _env_file=None,
        feishu_webhook=kw.pop("feishu_webhook", "https://open.feishu.cn/open-apis/bot/v2/hook/test"),
        feishu_secret=SECRET,
        feishu_hot_rank_jump=3,
        feishu_hot_ratio=0.30,
        feishu_alert_cooldown_hours=6,
        **kw,
    )


# ---- 签名 ----
def test_webhook_for_routes_per_platform() -> None:
    """webhook_for:优先平台专属群,未配回落主群(总群)。"""
    from app.services.feishu_client import webhook_for

    s = _settings(feishu_webhook_xianyu="https://open.feishu.cn/hook/xianyu")
    assert webhook_for(s, "xianyu") == "https://open.feishu.cn/hook/xianyu"  # 闲鱼专属群优先
    assert webhook_for(s, "douhot") == s.feishu_webhook                    # 未配抖音专属 → 总群
    assert webhook_for(s, "") == s.feishu_webhook                         # 无板块 → 总群


def test_daily_splits_to_platform_groups(monkeypatch, session) -> None:
    """日报拆分:配了专属群的平台段进专属群;总群收聚合段+未配专属群的平台段。"""
    from datetime import datetime

    from app.db.models import WeiboHotItem, XianyuItem
    from app.services import feishu

    now = datetime.now()
    session.add(WeiboHotItem(user_id=1, title="微博热搜A", heat=100, rank=1, captured_at=now))
    session.add(XianyuItem(user_id=1, item_id="i1", title="闲鱼商品B", created_at=now))
    session.commit()

    sent = []  # (webhook, text)
    def fake_client(webhook, secret):
        class F:
            def send(self, t): sent.append((webhook, t)); return True
            def send_card(self, c): sent.append((webhook, c)); return True
        return F()
    monkeypatch.setattr(feishu, "FeishuClient", fake_client)  # 模块命名空间(顶部绑定的名字)

    main_hook = "https://open.feishu.cn/hook/main"
    xy_hook = "https://open.feishu.cn/hook/xianyu"
    feishu.run_feishu_daily(_settings(feishu_webhook=main_hook, feishu_webhook_xianyu=xy_hook), db=session)

    # 闲鱼专属群收到闲鱼日报段
    assert any(wh == xy_hook and "闲鱼热榜日报" in t and "【闲鱼热榜】" in t for wh, t in sent)
    # 总群收到聚合段 + 未配专属群的微博段(不含已被分流的闲鱼段)
    assert any(wh == main_hook and "热点日报" in t and "【微博热搜】" in t and "【闲鱼热榜】" not in t for wh, t in sent)


def test_sign_matches_feishu_algorithm() -> None:
    """用飞书官方算法独立算一遍,确认实现正确。

    官方:sign = base64(HmacSHA256("{timestamp}\\n{secret}", ""))——以 string_to_sign 为 key,空消息。
    """
    ts = 1700000000
    key = f"{ts}\n{SECRET}".encode()
    expect = base64.b64encode(hmac.new(key, msg=b"", digestmod=hashlib.sha256).digest()).decode()
    assert _sign(SECRET, ts) == expect
    # 用错方向(secret 当 key)会产生不同结果,证明实现用的是官方方向
    wrong = base64.b64encode(hmac.new(SECRET.encode(), f"{ts}\n{SECRET}".encode(), hashlib.sha256).digest()).decode()
    assert _sign(SECRET, ts) != wrong


def test_sign_deterministic() -> None:
    assert _sign(SECRET, 123) == _sign(SECRET, 123) and _sign(SECRET, 123) != _sign(SECRET, 124)


# ---- 涨跌判定(_delta)----
def _w(title, rank, ts=None):
    return WeiboHotItem(user_id=1, title=title, rank=rank, heat=rank * 1000, captured_at=ts or datetime.now())


def test_delta_up_down_new_stay(session) -> None:
    cur, prev = {"a": _w("a", 2)}, {"a": _w("a", 5), "b": _w("b", 1)}
    assert _delta("weibo", cur["a"], prev) == ("up", "+3名")   # 5 → 2:升 3 名
    assert _delta("weibo", _w("c", 4, datetime.now() + timedelta(days=1)), prev) == ("new", "")  # 新增
    assert _delta("weibo", _w("b", 1, datetime.now() + timedelta(days=1)), prev) == ("stay", "")  # 同排名持平
    assert _delta("weibo", _w("a", 8, datetime.now() + timedelta(days=1)), prev) == ("down", "-3名")


def test_delta_douhot_uses_score_ratio(session) -> None:
    prev = DouhotWord(user_id=1, title="词", score=100, created_at=datetime.now())
    cur_hot = DouhotWord(user_id=1, title="词", score=150, created_at=datetime.now() + timedelta(days=1))
    cur_cold = DouhotWord(user_id=1, title="词", score=90, created_at=datetime.now() + timedelta(days=1))
    assert _delta("douhot", cur_hot, {"词": prev}) == ("up", "+50%")        # 100 → 150
    assert _delta("douhot", cur_cold, {"词": prev}) == ("down", "-10%")     # 100 → 90


# ---- 批次切分与日报 ----
def seed_weibo(session, user_id=1):
    t1, t2 = datetime(2026, 9, 1, 8), datetime(2026, 9, 2, 8)
    # 上一批(9-1):A 第 5 名,B 第 1 名,C 第 3 名
    for title, rank in [("A", 5), ("B", 1), ("C", 3)]:
        session.add(WeiboHotItem(user_id=user_id, title=title, rank=rank, heat=rank * 1000, captured_at=t1))
    # 当前批(9-2):A 升到第 2 名,B 掉到第 4 名,D 新增
    for title, rank in [("A", 2), ("B", 4), ("D", 7)]:
        session.add(WeiboHotItem(user_id=user_id, title=title, rank=rank, heat=100000, captured_at=t2))
    session.commit()


def test_batches_split_into_two_snapshots(session) -> None:
    seed_weibo(session)
    cur, prev = feishu._batches(session, 1, "weibo")
    assert set(cur) == {"A", "B", "D"} and set(prev) == {"A", "B", "C"}
    # 当前批名次:A=2 B=4 D=7;上一批名次:A=5 B=1 C=3
    assert getattr(cur["A"], "rank") == 2 and getattr(prev["A"], "rank") == 5


def test_daily_lines_include_rank_tags(session) -> None:
    seed_weibo(session)
    text = build_daily(session, 1, _settings())
    assert "微博热搜" in text
    assert "A" in text and "🔥+3名" in text          # 5→2 升 3 名
    assert "D" in text and "✅新增" in text           # 新出现
    assert "B" in text and "📉-3名" in text          # 1→4 掉 3 名
    assert "分析:" in text


def test_daily_includes_cross_section_and_tally(session) -> None:
    """日报含"今日活跃对比"(各板块上升数)与"跨板块共同上升"(含各板块预测)两个对比总结段。"""
    from datetime import datetime, timedelta

    from app.db.models import DouhotWord, WeiboHotItem

    base = datetime(2026, 9, 1, 8)
    # 微博:共同词加速上升
    for i, h in enumerate([1000, 1300, 1800, 2600]):
        session.add(WeiboHotItem(user_id=1, title="共同词", heat=h, rank=1, captured_at=base + timedelta(hours=i)))
    # 抖音:同一词也在上升 → 跨板块共同上升
    for i, h in enumerate([500, 700, 1000, 1600]):
        session.add(DouhotWord(user_id=1, title="共同词", score=h, created_at=base + timedelta(hours=i)))
    session.commit()

    text = build_daily(session, 1, _settings())
    assert "今日活跃对比" in text
    assert "跨板块共同上升" in text
    assert "共同词" in text  # 跨板块段里出现该词


# ---- 实时提醒(替换发送为假客户端)----
def test_realtime_only_pushes_new_and_big_jump(session, monkeypatch) -> None:
    seed_weibo(session)
    sent = []
    monkeypatch.setattr(feishu, "FeishuClient", lambda w, s: type("F", (), {"send": lambda self, t: (sent.append(t), True)[1]})())
    n = run_feishu_realtime("weibo", 1, _settings(), db=session)
    # 触发:新增 D(新增即推)、A 升 3 名(≥3 名);B 掉 3 名不推;C 只在上一批不推
    assert n == 2
    joined = sent[0]
    assert "D" in joined and "新增" in joined
    assert "A" in joined and "+3名" in joined
    assert "B" not in joined


def test_realtime_respects_cooldown(session, monkeypatch) -> None:
    seed_weibo(session)
    n_calls = {"n": 0}

    def fake_send(self, t):
        n_calls["n"] += 1
        return True

    monkeypatch.setattr(feishu, "FeishuClient", lambda w, s: type("F", (), {"send": fake_send})())
    settings = _settings()
    first = run_feishu_realtime("weibo", 1, settings, db=session)
    second = run_feishu_realtime("weibo", 1, settings, db=session)  # 已写去重表,冷却期内应全走冷却 → 不重推
    assert first == 2 and second == 0 and n_calls["n"] == 1


def test_realtime_message_includes_prediction(session, monkeypatch) -> None:
    """实时提醒给推送词补"预测/置信度/趋势"(历史样本≥2 才预测)。"""
    seed_weibo(session)
    sent = []
    monkeypatch.setattr(feishu, "FeishuClient", lambda w, s: type("F", (), {"send": lambda self, t: (sent.append(t), True)[1]})())
    n = run_feishu_realtime("weibo", 1, _settings(), db=session)
    assert n >= 1
    joined = "\n".join(sent)
    assert "预测" in joined          # 推送词带预测
    assert "上升期" in joined        # 且带趋势标签


def test_section_weekly_tally(session) -> None:
    """周对比:各板块"本周 vs 上周"活跃话题数。本周=A,B;上周=A,C → 2↔2。"""
    from datetime import datetime, timedelta

    now = datetime(2026, 9, 7, 8)
    for t in ["A", "B"]:
        session.add(WeiboHotItem(user_id=1, title=t, heat=100, rank=1, captured_at=now - timedelta(days=1)))
    for t in ["A", "C"]:
        session.add(WeiboHotItem(user_id=1, title=t, heat=100, rank=1, captured_at=now - timedelta(days=10)))
    session.commit()
    tally = feishu._section_weekly_tally(session, now)
    assert any("微博" in line and "2↔2" in line for line in tally)


def test_daily_lists_topic_entries_with_new_marker(session) -> None:
    """日报为榜单搜索类关注列出 Top 相关主题,新进条目标 🆕。"""
    from datetime import datetime, timedelta

    from app.db.models import DouhotWatch, DouhotWatchSnap

    base = datetime(2026, 9, 1, 8)
    session.add(DouhotWatch(user_id=1, section="douhot", list_type="topic", keyword="早春晴朗"))
    session.add_all([
        # 主题1:两轮(非新增)
        DouhotWatchSnap(user_id=1, section="douhot", list_type="topic", keyword="早春晴朗",
                        entry_title="早春晴朗", score=15518628, rank_now=1, captured_at=base),
        DouhotWatchSnap(user_id=1, section="douhot", list_type="topic", keyword="早春晴朗",
                        entry_title="早春晴朗", score=16000000, rank_now=1, captured_at=base + timedelta(days=1)),
        # 主题2:一轮(新增)
        DouhotWatchSnap(user_id=1, section="douhot", list_type="topic", keyword="早春晴朗",
                        entry_title="早春晴朗·新", score=100000, rank_now=2, captured_at=base + timedelta(days=1)),
    ])
    session.commit()
    text = build_daily(session, 1, _settings())
    assert "早春晴朗" in text
    assert "2主题" in text                 # 两个相关主题(标题带趋势概览)
    assert "🆕" in text and "早春晴朗·新" in text  # 新进条目标 🆕
    assert "今日vs昨日" in text              # 对比汇总段
    assert "↑" in text                       # 上升期带 ↑ 箭头(早春晴朗两轮在涨)


def test_split_messages_chunks_long_text() -> None:
    text = "\n".join(f"line {i} " + "x" * 500 for i in range(100))
    chunks = feishu._split_messages(text, max_len=3000)
    assert len(chunks) > 1
    assert all(len(c) <= 3000 for c in chunks)
    assert "\n".join(chunks) == text


def test_pad_cell_left_aligns_columns() -> None:
    """全角空格补齐:把不同长度单元格补到固定显示宽度,让各列起点一致(左对齐)。"""
    wid = [32, 8, 12, 6, 8]
    rows = [
        ["英国公开赛", "新增", "预测471616", "中", "震荡"],
        ["卢克", "新增", "预测626755", "低", "震荡"],
        ["抗战胜利纪念日", "新增", "—", "—", "震荡"],
    ]
    for r in rows:
        # 每列显示宽度都被补到固定宽度
        assert [feishu._display_width(feishu._pad_cell(t, w)) for t, w in zip(r, wid)] == wid
    # 各列起点(=前序列宽累加)在每一行一致
    for r in rows:
        starts = [sum(wid[: i + 1]) for i in range(len(wid))]
        assert starts == [sum(wid[: i + 1]) for i in range(len(wid))]


def test_build_keyword_card_structure(session) -> None:
    """关键词监控交互卡片:含标题、各关注词的分块(关键词+主题明细)。"""
    from datetime import datetime

    from app.db.models import DouhotWatch, DouhotWatchSnap

    base = datetime(2026, 9, 1, 8)
    session.add(DouhotWatch(user_id=1, section="douhot", list_type="topic", keyword="早春晴朗"))
    session.add_all([
        DouhotWatchSnap(user_id=1, section="douhot", list_type="topic", keyword="早春晴朗",
                        entry_title="早春晴朗", score=15518628, rank_now=1, captured_at=base),
        DouhotWatchSnap(user_id=1, section="douhot", list_type="topic", keyword="早春晴朗",
                        entry_title="早春晴朗·酷酷", score=500, rank_now=2, captured_at=base),
    ])
    session.commit()
    card = feishu.build_keyword_card(session, 1, _settings())
    assert card is not None
    assert card["header"]["title"]["content"] == "🤖 关键词监控 · 智能体"
    body = str(card["elements"])
    assert "早春晴朗" in body and "🆕" in body  # 关键词 + 新进主题
    assert len(str(card)) < 20000  # 未超卡片长度上限


def test_keyword_realtime_pushes_on_new_topic(session, monkeypatch) -> None:
    """话题词实时提醒:检测到 新进/上升 主题即推一条(冷却去重,不刷屏)。"""
    from datetime import datetime, timedelta

    from app.db.models import DouhotWatch, DouhotWatchSnap
    from app.services.feishu import run_feishu_keyword_realtime

    base = datetime(2026, 9, 1, 8)
    session.add(DouhotWatch(user_id=1, section="douhot", list_type="topic", keyword="早春晴朗"))
    session.add_all([
        # 批次1:A
        DouhotWatchSnap(user_id=1, section="douhot", list_type="topic", keyword="早春晴朗",
                        entry_title="A", score=1000, rank_now=1, captured_at=base),
        # 批次2:A(上升,trend_growth)> B(新进)
        DouhotWatchSnap(user_id=1, section="douhot", list_type="topic", keyword="早春晴朗",
                        entry_title="A", score=1500, rank_now=1, captured_at=base + timedelta(days=1),
                        trend_growth=0.5),
        DouhotWatchSnap(user_id=1, section="douhot", list_type="topic", keyword="早春晴朗",
                        entry_title="B", score=500, rank_now=2, captured_at=base + timedelta(days=1)),
    ])
    session.commit()
    sent = []
    monkeypatch.setattr(feishu, "FeishuClient", lambda w, s: type("F", (), {
        "send": lambda self, t: (sent.append(t), True)[1],
        "send_card": lambda self, c: (sent.append(c), True)[1],
    })())
    n = run_feishu_keyword_realtime(1, _settings(), db=session)
    assert n == 1
    card = sent[0]
    body = str(card)
    assert "话题词监控" in body and "A" in body and "新进" in body and "上升" in body
    assert run_feishu_keyword_realtime(1, _settings(), db=session) == 0  # 冷却期内不重推


def test_keyword_burst_alert(monkeypatch, session) -> None:
    """智能体预警:关注词呈加速上升时推送"可能爆发",且进冷却去重。"""
    from app.services import tenant
    from app.db.models import DouhotWatch, DouhotWatchSnap

    # 关注一个词,喂它一段加速上升的热度序列
    session.add(DouhotWatch(user_id=1, list_type="word", keyword="爆点"))
    for v in [1000, 1100, 1300, 1800, 2600]:
        session.add(DouhotWatchSnap(user_id=1, list_type="word", keyword="爆点", score=v, rank_now=1))
    session.commit()

    sent = []
    monkeypatch.setattr(feishu, "FeishuClient", lambda w, s: type("F", (), {"send": lambda self, t: (sent.append(t), True)[1]})())
    n = run_feishu_keyword_alerts(1, _settings(), db=session)
    assert n == 1
    assert "可能爆发" in sent[0] and "爆点" in sent[0]
    # 冷却期内再跑 → 不重推
    assert run_feishu_keyword_alerts(1, _settings(), db=session) == 0


def test_keyword_burst_skips_when_not_rising(monkeypatch, session) -> None:
    from app.db.models import DouhotWatch, DouhotWatchSnap

    session.add(DouhotWatch(user_id=1, list_type="word", keyword="退潮"))
    for v in [2000, 1500, 1000, 500]:
        session.add(DouhotWatchSnap(user_id=1, list_type="word", keyword="退潮", score=v, rank_now=1))
    session.commit()
    n_calls = {"n": 0}
    monkeypatch.setattr(feishu, "FeishuClient", lambda w, s: type("F", (), {"send": lambda self, t: (n_calls.__setitem__("n", n_calls["n"] + 1), True)[1]})())
    assert run_feishu_keyword_alerts(1, _settings(), db=session) == 0
    assert n_calls["n"] == 0  # 回落期不推


def test_daily_includes_keyword_agent_section(session) -> None:
    """日报应把关注词的智能体分析(趋势/预测/置信度)带进去。"""
    from app.db.models import DouhotWatch, DouhotWatchSnap

    session.add(DouhotWatch(user_id=1, list_type="word", keyword="爆点"))
    for v in [1000, 1100, 1300, 1800, 2600]:
        session.add(DouhotWatchSnap(user_id=1, list_type="word", keyword="爆点", score=v, rank_now=1))
    session.commit()
    text = build_daily(session, 1, _settings())
    assert "关键词关注 · 智能体" in text
    assert "爆点" in text
    assert "上升期" in text and "上升期" in text


def test_scheduler_registers_insight_job() -> None:
    """调度器应注册"爆点回顾"周报 job(feishu_insight)。"""
    from apscheduler.schedulers.background import BackgroundScheduler
    from app.services.scheduler import build_jobs

    sched = BackgroundScheduler(timezone="Asia/Shanghai")
    build_jobs(sched)
    ids = [j.id for j in sched.get_jobs()]
    assert "feishu_insight" in ids
    sched.shutdown(wait=False) if sched.running else None


def test_burst_confidence_gate(monkeypatch, session) -> None:
    """置信度分级:FEISHU_BURST_MIN_CONFIDENCE=高 时,"中"置信的爆发不实时推(只进日报/洞察)。"""
    from app.db.models import DouhotWatch, DouhotWatchSnap

    from app.services import keyword_agent as ka

    session.add(DouhotWatch(user_id=1, list_type="word", keyword="中置信词"))
    for i in range(5):
        session.add(DouhotWatchSnap(user_id=1, list_type="word", keyword="中置信词", score=100 + i * 50, rank_now=1))
    session.commit()
    # 模拟 analyze 返回"中"置信的爆发
    monkeypatch.setattr(ka, "analyze", lambda kw, v: {
        "keyword": kw, "burst": True, "trend_label": "上升期", "growth": 0.5,
        "forecast_next": 100, "confidence": "中", "points": 5,
    })
    # min=高 → 不推
    sent = []
    monkeypatch.setattr(feishu, "FeishuClient", lambda w, s: type("F", (), {"send": lambda self, t: (sent.append(t), True)[1]})())
    n = feishu.run_feishu_keyword_alerts(1, _settings(feishu_burst_min_confidence="高"), db=session)
    assert n == 0 and not sent
    # min=中 → 推
    n2 = feishu.run_feishu_keyword_alerts(1, _settings(feishu_burst_min_confidence="中"), db=session)
    assert n2 == 1 and sent and "中置信词" in sent[0]


def test_agent_confidence_rank() -> None:
    assert feishu._agent_confidence_rank("高") > feishu._agent_confidence_rank("中")
    assert feishu._agent_confidence_rank("中") > feishu._agent_confidence_rank("低")
    assert feishu._agent_confidence_rank(None) == 0


def test_collect_failures_alert_and_cooldown(monkeypatch, session) -> None:
    """采集持续失败告警:近24h失败>=阈值才推,且冷却期内不重推。"""
    from datetime import datetime, timedelta
    from app.services import alert_service
    from app.db.models import RunRecord

    for i in range(3):  # 近24h 3 次失败
        session.add(RunRecord(user_id=1, run_id=f"r{i}", kind="douhot", status="failed",
                              started_at=datetime.now() - timedelta(hours=i)))
    session.add(RunRecord(user_id=1, run_id="ok", kind="weibo", status="success", started_at=datetime.now()))
    session.commit()

    sent = []
    monkeypatch.setattr(feishu_client, "FeishuClient", lambda w, s: type("F", (), {"send": lambda self, t: (sent.append(t), True)[1]})())
    n = alert_service.check_collect_failures(_settings(fail_alert_threshold=3), db=session)
    assert n == 1
    assert "douhot" in sent[0] and "失败 3 次" in sent[0]
    # 冷却期内再跑 → 不重推
    assert alert_service.check_collect_failures(_settings(fail_alert_threshold=3), db=session) == 0


def test_collect_failures_below_threshold_no_alert(monkeypatch, session) -> None:
    from datetime import datetime
    from app.services import alert_service
    from app.db.models import RunRecord

    session.add(RunRecord(user_id=1, run_id="r", kind="douhot", status="failed", started_at=datetime.now()))
    session.commit()
    sent = []
    monkeypatch.setattr(feishu_client, "FeishuClient", lambda w, s: type("F", (), {"send": lambda self, t: (sent.append(t), True)[1]})())
    assert alert_service.check_collect_failures(_settings(fail_alert_threshold=3), db=session) == 0
    assert not sent


def test_collect_failures_no_alert_if_recovered(monkeypatch, session) -> None:
    """失败几次后又成功 → 当前未断,不应误报"持续失败"。"""
    from datetime import datetime, timedelta
    from app.services import alert_service
    from app.db.models import RunRecord

    for i in range(3):  # 前 3 次失败
        session.add(RunRecord(user_id=1, run_id=f"f{i}", kind="douhot", status="failed",
                              started_at=datetime.now() - timedelta(hours=3, minutes=i)))
    session.add(RunRecord(user_id=1, run_id="ok", kind="douhot", status="success",
                          started_at=datetime.now()))  # 最近一次成功 → 已恢复
    session.commit()
    sent = []
    monkeypatch.setattr(feishu_client, "FeishuClient", lambda w, s: type("F", (), {"send": lambda self, t: (sent.append(t), True)[1]})())
    assert alert_service.check_collect_failures(_settings(fail_alert_threshold=3), db=session) == 0
    assert not sent


def test_health_stalls_alert_and_cooldown(monkeypatch, session) -> None:
    """采集停摆告警:在用平台(配了Cookie)超过 health_stall_hours 无新数据 → 推飞书;冷却期内不重推。"""
    from datetime import datetime, timedelta

    from app.services import alert_service
    from app.db.models import UserCookie, XianyuItem

    session.add(UserCookie(user_id=1, platform="goofish", cookie="x"))  # 闲鱼在用(session 已预置用户#1)
    # 闲鱼数据停在 48h 前(> 24h 阈值)→ 停摆
    session.add(XianyuItem(user_id=1, item_id="i1", title="商品", created_at=datetime.now() - timedelta(hours=48)))
    session.commit()

    sent = []
    monkeypatch.setattr(feishu_client, "FeishuClient", lambda w, s: type("F", (), {"send": lambda self, t: (sent.append(t), True)[1]})())
    n = alert_service.check_health_stalls(_settings(health_stall_hours=24), db=session)
    assert n == 1
    assert "闲鱼" in sent[0] and "停摆" in sent[0]
    # 冷却期内(6h)再跑 → 不重推
    assert alert_service.check_health_stalls(_settings(health_stall_hours=24), db=session) == 0


def test_health_stalls_escalates_long_term(monkeypatch, session) -> None:
    """停摆超过 escalate_days → 升级标注【长期】,区分偶发与长期坏。"""
    from datetime import datetime, timedelta

    from app.services import alert_service
    from app.db.models import UserCookie, XianyuItem

    session.add(UserCookie(user_id=1, platform="goofish", cookie="x"))
    # 闲鱼数据停在 5 天前(> 3 天升级阈值)
    session.add(XianyuItem(user_id=1, item_id="i1", title="商品", created_at=datetime.now() - timedelta(days=5)))
    session.commit()

    sent = []
    monkeypatch.setattr(feishu_client, "FeishuClient", lambda w, s: type("F", (), {"send": lambda self, t: (sent.append(t), True)[1]})())
    n = alert_service.check_health_stalls(_settings(health_stall_hours=24, health_escalate_days=3), db=session)
    assert n == 1
    assert "🔴" in sent[0] and "长期" in sent[0] and "5 天" in sent[0]


def test_realtime_baidu_no_keyerror(monkeypatch, session) -> None:
    """baidu 接入后实时提醒不应再 KeyError(此前 _TABLES 缺 baidu)。"""
    from datetime import datetime
    from app.db.models import BaiduHotItem

    t1, t2 = datetime(2026, 9, 1, 8), datetime(2026, 9, 2, 8)
    for title, rank in [("A", 5), ("B", 1), ("C", 3)]:
        session.add(BaiduHotItem(user_id=1, title=title, heat=rank * 1000, rank=rank, captured_at=t1))
    for title, rank in [("A", 2), ("B", 4), ("D", 7)]:
        session.add(BaiduHotItem(user_id=1, title=title, heat=rank * 1000, rank=rank, captured_at=t2))
    session.commit()
    sent = []
    monkeypatch.setattr(feishu, "FeishuClient", lambda w, s: type("F", (), {"send": lambda self, t: (sent.append(t), True)[1]})())
    n = run_feishu_realtime("baidu", 1, _settings(), db=session)
    assert n == 2          # 新增 D、A 升 3 名
    assert "百度热搜" in sent[0] and "D" in sent[0] and "A" in sent[0]
