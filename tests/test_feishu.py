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


def test_webhooks_for_additive_main_and_group() -> None:
    """webhooks_for:主群 + 该平台专属群 都发(加法模型,主群不停止)。"""
    from app.services.feishu_client import webhooks_for

    s = _settings(feishu_webhook="main", feishu_webhook_xianyu="xy")
    assert webhooks_for(s, "xianyu") == ["main", "xy"]   # 主群 + 专属群
    assert webhooks_for(s, "douhot") == ["main"]         # 未配抖音专属 → 仅主群
    assert webhooks_for(s, "") == ["main"]               # 无板块 → 主群


def test_collect_failures_sends_when_only_platform_hook(monkeypatch, session) -> None:
    """主群为空但闲鱼专属群已配时,闲鱼失败告警仍应发送(守卫不再只看主群)。"""
    from datetime import datetime, timedelta

    from app.services import alert_service
    from app.db.models import RunRecord

    for i in range(3):  # 近24h 3 次闲鱼失败
        session.add(RunRecord(user_id=1, run_id=f"r{i}", kind="xianyu", status="failed",
                              started_at=datetime.now() - timedelta(hours=i)))
    session.commit()
    sent = []
    monkeypatch.setattr(feishu_client, "FeishuClient",
                        lambda w, s: type("F", (), {"send": lambda self, t: (sent.append((w, t)), True)[1]})())
    n = alert_service.check_collect_failures(_settings(feishu_webhook="", feishu_webhook_xianyu="xy"), db=session)
    assert n == 1
    assert sent[0][0] == "xy"  # 推到闲鱼专属群(主群为空也不拦)


def test_daily_splits_to_platform_groups(monkeypatch, session) -> None:
    """日报拆分:配了专属群的平台段进专属群;总群收聚合段+未配专属群的平台段。"""
    from datetime import datetime, timedelta

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
    # 主群(总群)仍收到**完整日报**(含微博段+闲鱼段)—— 主群"不变、不停止"
    assert any(wh == main_hook and "热点日报" in t and "【微博热搜】" in t and "【闲鱼热榜】" in t for wh, t in sent)


def test_wechat_analysis_pushes_to_group(monkeypatch, session) -> None:
    """公众号内容选题分析:有文章时推送到公众号专属群。"""
    from datetime import datetime, timedelta

    from app.db.models import WechatArticle
    from app.services import feishu

    session.add(WechatArticle(user_id=1, title="揭秘AI副业3个方法", content="副业 AI 教程 干货",
                              author="科技君", url="http://x", publish_at=datetime.now()))
    session.commit()

    sent = []
    def fake_client(webhook, secret):
        class F:
            def send(self, t): sent.append((webhook, t)); return True
        return F()
    monkeypatch.setattr(feishu, "FeishuClient", fake_client)
    wx_hook = "https://open.feishu.cn/hook/wechat"
    n = feishu.run_feishu_wechat_analysis(_settings(feishu_webhook_wechat=wx_hook), db=session)
    assert n == 1
    assert sent[0][0] == wx_hook                     # 推到公众号群
    assert "内容选题分析" in sent[0][1] and "副业" in sent[0][1]


def test_wechat_analysis_masks_own_account(monkeypatch, session) -> None:
    """自营号脱敏(2026-09-29):分析推送点名"篇数最多的对标号"时,自营号名不得出现在飞书文本。"""
    from datetime import datetime

    from app.db.models import WechatArticle
    from app.services import feishu

    # 自营号文章数最多 → 旧逻辑会点名;脱敏后必须显示「内部号」
    for i in range(3):
        session.add(WechatArticle(user_id=1, title=f"天一项目拆解 揭秘副业{i}", content="副业 教程",
                                  author="天一项目拆解", url=f"http://own{i}", publish_at=datetime.now()))
    session.add(WechatArticle(user_id=1, title="科技快讯合集", content="科技 资讯",
                              author="科技君", url="http://x", publish_at=datetime.now()))
    session.commit()

    sent = []

    def fake_client(webhook, secret):
        class F:
            def send(self, t):
                sent.append((webhook, t))
                return True
        return F()
    monkeypatch.setattr(feishu, "FeishuClient", fake_client)
    wx_hook = "https://open.feishu.cn/hook/wechat"
    n = feishu.run_feishu_wechat_analysis(_settings(feishu_webhook_wechat=wx_hook), db=session)
    assert n == 1
    text = sent[0][1]
    assert "天一项目拆解" not in text, "自营号名泄漏到飞书推送"
    assert "内部号" in text


def test_mask_own_util() -> None:
    """mask_own:名单内替换为「内部号」,多号逗号分隔,无名单原样返回。"""
    s = _settings(own_account_names="天一项目拆解, 另一个号")
    assert feishu.mask_own("「天一项目拆解」篇数最多", s) == "「内部号」篇数最多"
    assert feishu.mask_own("另一个号也活跃", s) == "内部号也活跃"
    assert feishu.mask_own("普通对标号不受影响", s) == "普通对标号不受影响"
    assert feishu.mask_own("天一项目拆解", _settings(own_account_names="")) == "天一项目拆解"


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
    t1, t2 = datetime.now() - timedelta(days=1), datetime.now()
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

    base = datetime.now() - timedelta(hours=4)  # 采样点落在 7 天趋势窗口内
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

    def _fake(w, s):
        class F:
            def send(self, t): return (sent.append(t), True)[1]
            def send_card(self, c): return (sent.append(str(c)), True)[1]
        return F()

    monkeypatch.setattr(feishu, "FeishuClient", _fake)
    n = run_feishu_realtime("weibo", 1, _settings(), db=session)
    # 触发:新增 D(新增即推)、A 升 3 名(≥3 名);B 掉 3 名不推;C 只在上一批不推
    assert n == 2
    joined = sent[0]
    assert "D" in joined and "新增" in joined
    assert "A" in joined and "+3名" in joined
    assert "B" not in joined
    assert "column_set" in joined  # 网格卡片格式


def test_realtime_fans_out_to_main_and_section_group(session, monkeypatch) -> None:
    """主群 + 板块专属群都要收到实时卡片(加法模型,不因先送达主群而短路专属群)。

    回归:any(FeishuClient(...).send_card(c) for wh in whs) 在第一个 True 即短路,
    导致专属群永远收不到——即使冷却已落、pushed 计数正常。
    """
    seed_weibo(session)
    called: list[str] = []

    def _fake(w, s):
        class F:
            def send_card(self, c):
                called.append(w)
                return True
        return F()

    monkeypatch.setattr(feishu, "FeishuClient", _fake)
    n = run_feishu_realtime("weibo", 1, _settings(feishu_webhook="main", feishu_webhook_weibo="wb"), db=session)
    assert n == 2
    assert called == ["main", "wb"]  # 两个群各收到一次,主群成功不短路专属群


def test_realtime_respects_cooldown(session, monkeypatch) -> None:
    seed_weibo(session)
    n_calls = {"n": 0}

    def fake_send(self, t):
        n_calls["n"] += 1
        return True

    def fake_card(self, c):
        n_calls["n"] += 1
        return True

    monkeypatch.setattr(feishu, "FeishuClient", lambda w, s: type("F", (), {"send": fake_send, "send_card": fake_card})())
    settings = _settings()
    first = run_feishu_realtime("weibo", 1, settings, db=session)
    second = run_feishu_realtime("weibo", 1, settings, db=session)  # 已写去重表,冷却期内应全走冷却 → 不重推
    assert first == 2 and second == 0 and n_calls["n"] == 1


def test_realtime_message_includes_prediction(session, monkeypatch) -> None:
    """实时提醒给推送词补"预测/置信度/趋势"(历史样本≥2 才预测)。"""
    seed_weibo(session)
    sent = []
    def _fake(w, s):
        class F:
            def send(self, t): return (sent.append(t), True)[1]
            def send_card(self, c): return (sent.append(str(c)), True)[1]
        return F()

    monkeypatch.setattr(feishu, "FeishuClient", _fake)
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
    from datetime import datetime, timedelta

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
    def _fake(w, s):
        class F:
            def send(self, t): return (sent.append(t), True)[1]
            def send_card(self, c): return (sent.append(str(c)), True)[1]
        return F()

    monkeypatch.setattr(feishu, "FeishuClient", _fake)
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
    """爆点回顾不再单占一条 cron,改由「推送时段表」的每分钟 tick 按配置触发(2026-10-01)。

    默认时刻仍在 `push_timeline.PUSH_KINDS["insight"]`(周一 09:00),由
    tests/test_push_timeline.py 逐条锁定;这里只确认调度器上装的是那条 tick。
    """
    from apscheduler.schedulers.background import BackgroundScheduler
    from app.services.scheduler import build_jobs
    from app.services.push_timeline import PUSH_KINDS

    sched = BackgroundScheduler(timezone="Asia/Shanghai")
    build_jobs(sched)
    ids = [j.id for j in sched.get_jobs()]
    assert "push_timeline" in ids
    assert "feishu_insight" not in ids            # 老的独立 job 已撤
    assert PUSH_KINDS["insight"]["times"] == ["13:30"]   # 错峰版(2026-10-01)
    assert PUSH_KINDS["insight"]["days"] == [1]   # 周一
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
    from datetime import datetime, timedelta
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
    from app.db.models import UserCookie, UserSchedule, XianyuItem

    session.add(UserSchedule(user_id=1, section="xianyu", interval_minutes=30, enabled=True))
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


def test_health_stalls_fans_out_to_main_and_section_group(monkeypatch, session) -> None:
    """停摆告警也要主群 + 专属群都发:回归 any(... for wh in whs) 短路漏发专属群。"""
    from datetime import datetime, timedelta

    from app.services import alert_service
    from app.db.models import UserCookie, UserSchedule, XianyuItem

    session.add(UserSchedule(user_id=1, section="xianyu", interval_minutes=30, enabled=True))
    session.add(UserCookie(user_id=1, platform="goofish", cookie="x"))
    session.add(XianyuItem(user_id=1, item_id="i1", title="商品", created_at=datetime.now() - timedelta(hours=48)))
    session.commit()

    called: list[str] = []
    monkeypatch.setattr(
        feishu_client, "FeishuClient",
        lambda w, s: type("F", (), {"send": lambda self, t: (called.append(w), True)[1]})())
    n = alert_service.check_health_stalls(
        _settings(health_stall_hours=24, feishu_webhook="main", feishu_webhook_xianyu="xy"), db=session)
    assert n == 1
    assert called == ["main", "xy"]  # 主群送达后仍继续发专属群


def test_health_stalls_escalates_long_term(monkeypatch, session) -> None:
    """停摆超过 escalate_days → 升级标注【长期】,区分偶发与长期坏。"""
    from datetime import datetime, timedelta

    from app.services import alert_service
    from app.db.models import UserCookie, UserSchedule, XianyuItem

    session.add(UserSchedule(user_id=1, section="xianyu", interval_minutes=30, enabled=True))
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
    from datetime import datetime, timedelta
    from app.db.models import BaiduHotItem

    t1, t2 = datetime.now() - timedelta(days=1), datetime.now()
    for title, rank in [("A", 5), ("B", 1), ("C", 3)]:
        session.add(BaiduHotItem(user_id=1, title=title, heat=rank * 1000, rank=rank, captured_at=t1))
    for title, rank in [("A", 2), ("B", 4), ("D", 7)]:
        session.add(BaiduHotItem(user_id=1, title=title, heat=rank * 1000, rank=rank, captured_at=t2))
    session.commit()
    sent = []
    def _fake(w, s):
        class F:
            def send(self, t): return (sent.append(t), True)[1]
            def send_card(self, c): return (sent.append(str(c)), True)[1]
        return F()

    monkeypatch.setattr(feishu, "FeishuClient", _fake)
    n = run_feishu_realtime("baidu", 1, _settings(), db=session)
    assert n == 2          # 新增 D、A 升 3 名
    assert "百度热搜" in sent[0] and "D" in sent[0] and "A" in sent[0]


# ---------------------------------------------------------------- 闲鱼风控即时告警
def test_notify_incident_sends_then_cooldowns(session) -> None:
    """事件级告警:首次推送 + FeishuAlert 冷却去重(6h 内不重发)。"""
    from app.services import alert_service

    sent: list[str] = []

    class _FakeFeishu:
        def __init__(self, webhook, secret="") -> None:
            pass

        def send(self, msg: str) -> bool:
            sent.append(msg)
            return True

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(feishu_client, "FeishuClient", _FakeFeishu)
    st = _settings(feishu_webhook_xianyu="https://open.feishu.cn/hook/xianyu",
                   xianyu_use_browser=False)   # 这两条用例测的是**协议路**的错误映射
    assert alert_service.notify_incident(session, 1, "xianyu", "🔴 闲鱼触发人机验证(滑块)",
                                         "FAIL_SYS_USER_VALIDATE", settings=st) is True
    assert "滑块" in sent[0]
    assert alert_service.notify_incident(session, 1, "xianyu", "🔴 闲鱼触发人机验证(滑块)",
                                         "FAIL_SYS_USER_VALIDATE", settings=st) is False
    assert len(sent) == 1  # 冷却期内不重发


def test_notify_incident_admin_only_records_alert_without_feishu(session) -> None:
    """运维诊断型告警(push_feishu=False)只进站内 `alerts` 表:飞书群只放文章与 Cookie 提醒。

    用户 2026-09-26 口径:像"微信读书只能拿到最新一篇,同日其它篇可能漏推"这种
    "要不要自建 WeRSS / 要不要充值"的长期决策项,不该刷进员工群,但也不能没人看见。
    """
    from app.db.models import AlertRecord
    from app.services import alert_service

    sent: list[str] = []

    class _FakeFeishu:
        def __init__(self, webhook, secret="") -> None:
            pass

        def send(self, msg: str) -> bool:
            sent.append(msg)
            return True

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(feishu_client, "FeishuClient", _FakeFeishu)
    st = _settings(feishu_webhook_wechat="https://open.feishu.cn/hook/wechat")

    def notify(title="⚠️ 微信读书只能拿到最新一篇,同日其它篇可能漏推", **kw):
        a = dict(kw)
        a.setdefault("settings", st)
        a.setdefault("push_feishu", False)
        return alert_service.notify_incident(session, 1, "wechat", title, "本轮列不出的号:10", **a)

    assert notify() is False and sent == []           # 一次飞书都不该发
    row = session.scalars(select(AlertRecord)).one()
    assert row.section == "incident_wechat" and "只能拿到最新一篇" in row.keyword
    assert "列不出的号:10" in row.reason
    assert notify() is False                          # 冷却门照旧:不逐轮堆同一条
    assert len(session.scalars(select(AlertRecord)).all()) == 1
    # 站内型不依赖飞书配置:没配 webhook 也照样记账(否则诊断项彻底没人看得见)
    assert notify(title="⚠️ 同日其它篇漏推风险(无 webhook 场景)", settings=_settings()) is False
    assert len(session.scalars(select(AlertRecord)).all()) == 2
    # 默认仍是发飞书的(Cookie 过期那类要立刻提醒用户);换标题绕开上面那条的冷却门
    assert alert_service.notify_incident(session, 1, "wechat", "🟠 微信读书 Cookie 已过期",
                                         "请重新复制", settings=st) is True
    assert len(sent) == 1 and "Cookie 已过期" in sent[0]


def test_run_xianyu_full_block_notifies_incident(session, monkeypatch: pytest.MonkeyPatch) -> None:
    """全关键词被滑块挡:RunRecord=failed + 即时事件告警(带原因)推闲鱼群。"""
    from app.db.models import RunRecord, UserCookie
    from app.services import alert_service, tenant, xianyu as xianyu_mod
    from app.services.xianyu import XianyuVerify

    from app.services import cookie_store

    cookie_store.set_cookie(session, 1, "goofish", "_m_h5_tk=tk_1_1; unb=1; cookie2=c2")

    class _VerifyClient:
        def __init__(self, cookie: str, proxy: str | None = None) -> None:
            pass

        def search(self, keyword: str) -> list[dict]:
            raise XianyuVerify("闲鱼人机验证(滑块),需人工处理:FAIL_SYS_USER_VALIDATE::need verify")

    sent: list[str] = []

    class _FakeFeishu:
        def __init__(self, webhook, secret="") -> None:
            pass

        def send(self, msg: str) -> bool:
            sent.append(msg)
            return True

    monkeypatch.setattr(xianyu_mod, "XianyuClient", _VerifyClient)
    monkeypatch.setattr(feishu_client, "FeishuClient", _FakeFeishu)
    st = _settings(feishu_webhook_xianyu="https://open.feishu.cn/hook/xianyu",
                   xianyu_use_browser=False)   # 这两条用例测的是**协议路**的错误映射

    with pytest.raises(XianyuVerify):
        tenant.run_xianyu(session, 1, settings=st)  # 失败后照常上抛(调度器记 failed)
    run = session.scalars(select(RunRecord).order_by(RunRecord.id.desc())).first()
    assert run.status == "failed" and "XianyuVerify" in run.detail
    assert any("滑块" in m for m in sent)  # 即时告警带原因

    # FeishuAlert 冷却行已建
    fa = session.scalars(select(FeishuAlert)).all()
    assert any(f.section == "incident_xianyu" for f in fa)


@pytest.mark.parametrize("exc_name,expect_feishu", [
    ("XianyuVerify", True),          # 整轮滑块:不人工过验证就一直停摆 → 该当场看到
    ("XianyuCookieExpired", True),   # 登录态过期:同上,且属"Cookie 提醒"口径
    ("XianyuWafBlock", False),       # 网关压制:自动冷却到点自愈 → 只进站内
])
def test_xianyu_block_alert_routing_by_actionability(session, monkeypatch: pytest.MonkeyPatch,
                                                     exc_name: str, expect_feishu: bool) -> None:
    """用户 2026-09-27 口径:飞书只留"人不动手就一直坏"的,能自愈的降级只进站内。"""
    from app.services import alert_service, tenant, xianyu as xianyu_mod
    from app.services import cookie_store

    captured: list[tuple] = []
    monkeypatch.setattr(alert_service, "notify_incident",
                        lambda *a, **kw: captured.append((a, kw)) or False)
    monkeypatch.setattr(feishu_client, "FeishuClient", _FakeFeishuClient)

    exc_cls = getattr(xianyu_mod, exc_name)

    def _collect(settings, client, start_offset=0, stats=None):
        raise exc_cls("整轮被挡")

    cookie_store.set_cookie(session, 1, "goofish", "_m_h5_tk=tk_1_1; unb=1; cookie2=c2")
    monkeypatch.setattr(xianyu_mod, "XianyuClient", lambda ck, proxy=None: object())
    monkeypatch.setattr(xianyu_mod, "collect_hot", _collect)
    with pytest.raises(exc_cls):
        # ⚠️ 显式关掉浏览器路径:本用例测的是**协议路**的错误映射/告警路由,
        # 而 `run_xianyu` 默认已改走浏览器(`xianyu_use_browser=True`)
        tenant.run_xianyu(session, 1, settings=_settings(xianyu_use_browser=False))
    assert captured, f"{exc_name} 应当产生事件告警"
    # 没写 push_feishu 就等于默认发飞书
    assert captured[0][1].get("push_feishu", True) is expect_feishu, captured[0][1]


def test_xianyu_partial_verify_is_admin_only(session, monkeypatch: pytest.MonkeyPatch) -> None:
    """只有部分关键词被挡、其余照常采到 → 命中率下降而已,不修也能跑,别刷群。"""
    from app.services import alert_service, tenant, xianyu as xianyu_mod
    from app.services import cookie_store

    captured: list[tuple] = []
    monkeypatch.setattr(alert_service, "notify_incident",
                        lambda *a, **kw: captured.append((a, kw)) or False)
    monkeypatch.setattr(feishu_client, "FeishuClient", _FakeFeishuClient)

    def _collect(settings, client, start_offset=0, stats=None):
        stats["verify"] = ["坏词"]
        return [{"item_id": "i-ok", "title": "好词 商品", "hit_keywords": 1, "keywords": "好词",
                 "best_rank": 1}]

    cookie_store.set_cookie(session, 1, "goofish", "_m_h5_tk=tk_1_1; unb=1; cookie2=c2")
    monkeypatch.setattr(xianyu_mod, "XianyuClient", lambda ck, proxy=None: object())
    monkeypatch.setattr(xianyu_mod, "collect_hot", _collect)
    tenant.run_xianyu(session, 1, settings=_settings())
    hit = [c for c in captured if "部分关键词" in str(c)]
    assert hit and hit[0][1]["push_feishu"] is False


def test_xianyu_deep_partial_verify_is_admin_only(
        session, monkeypatch: pytest.MonkeyPatch) -> None:
    """深采中途撞滑块但已采到部分:搜索照常,只进站内(同一口径)。"""
    from app.services import alert_service, cookie_store, xianyu_analytics
    from app.services.xianyu import XianyuVerify

    captured: list[tuple] = []
    monkeypatch.setattr(alert_service, "notify_incident",
                        lambda *a, **kw: captured.append((a, kw)) or False)
    monkeypatch.setattr(xianyu_analytics.xianyu, "XianyuClient",
                        lambda ck, proxy=None: object())

    def _detail(client, iid):
        if iid == "i2":
            raise XianyuVerify("滑块")
        return {"want_count": 3}

    monkeypatch.setattr(xianyu_analytics.xianyu, "fetch_detail", _detail)
    cookie_store.set_cookie(session, 1, "goofish", "_m_h5_tk=tk_1_1; unb=1; cookie2=c2")
    xianyu_analytics.run_xianyu_deep(session, 1, settings=_settings(), hot=[
        {"item_id": "i1", "title": "商品一", "price": "1"},
        {"item_id": "i2", "title": "商品二", "price": "2"}])
    hit = [c for c in captured if "详情抓取触发人机验证" in str(c)]
    assert hit and hit[0][1]["push_feishu"] is False


class _FakeFeishuClient:
    def __init__(self, webhook, secret="") -> None:
        pass

    def send(self, msg: str) -> bool:
        return True


def test_build_keyword_card_filters_by_section(session) -> None:
    """关键词监控卡按板块过滤:总群全量、板块专属群只含该板块词(含名次变化)。"""
    import json

    from app.db.models import DouhotWatch, DouhotWatchSnap
    from app.services.feishu import build_keyword_card

    session.add_all([
        DouhotWatch(user_id=1, section="douhot", list_type="word", keyword="抖音词"),
        DouhotWatch(user_id=1, section="weibo", list_type="word", keyword="微博词"),
    ])
    session.commit()
    ts = datetime.now()
    session.add_all([
        DouhotWatchSnap(user_id=1, section="douhot", list_type="word", keyword="抖音词",
                        score=100, captured_at=ts),
        DouhotWatchSnap(user_id=1, section="weibo", list_type="word", keyword="微博词",
                        score=50, captured_at=ts),
    ])
    session.commit()
    st = _settings()
    full = json.dumps(build_keyword_card(session, 1, st), ensure_ascii=False)
    assert "抖音词" in full and "微博词" in full  # 总群=全量
    only = json.dumps(build_keyword_card(session, 1, st, section="douhot"), ensure_ascii=False)
    assert "抖音词" in only and "微博词" not in only  # 板块群=只含该板块
    assert build_keyword_card(session, 1, st, section="baidu") is None  # 无该板块词


def test_jobs_不自己持有_feishu_client_的函数引用():
    """⚠️ 回归守卫(2026-10-05):`platform_webhook` / `webhooks_for` 必须**经包命名空间**查找。

    为什么:`feishu/` 包的既有约定是**补丁打在包命名空间上**
    (`monkeypatch.setattr(feishu, "webhook_for", ...)`,见 tests/test_wechat_monitor.py)。
    若 `_jobs.py` 在顶层 `from app.services.feishu_client import platform_webhook`,
    就会在模块里**另绑一份** —— 补丁门面时 `_jobs` 里的那份**不被替换**,
    于是**测试静默假通过**(补丁看着生效了,实际这条链根本没被覆盖)。

    2026-10-05 审查发现这两个名字是**全包唯一的漏点**;本测试把它钉死,
    再有人图省事写顶层 import,立刻红。
    """
    from app.services.feishu import _jobs

    for name in ("platform_webhook", "webhooks_for", "webhook_for", "FeishuClient"):
        assert not hasattr(_jobs, name), (
            f"`feishu/_jobs.py` 顶层绑定了 `{name}` —— 必须改成经包命名空间 `_pkg.{name}` 调用,"
            "否则 monkeypatch 门面名不生效(静默假通过)"
        )


def test_health_stalls_covers_wechat_listen_stopped(monkeypatch, session) -> None:
    """★ **公众号原来根本不在这条告警的名单里**(2026-10-07 实测代价)。

    监听轮从 10-06 20:02 起整整 **30 小时一轮没跑**(应用全程在线、其他作业照跑),
    而这条"专门抓静默停摆"的告警**一个字都没说过** —— 因为
    `data_tables`/`labels`/`cookie_to_data` 三处都没有公众号。
    代价是"阅读数又断了"要靠用户来问才发现。
    """
    from datetime import datetime, timedelta

    from app.db.models import RunRecord, UserCookie, UserSchedule, WechatArticle
    from app.services import alert_service

    session.add(UserSchedule(user_id=1, section="wechat", interval_minutes=60, enabled=True))
    session.add(UserCookie(user_id=1, platform="weread", cookie="x"))
    # 最后一轮成功是 30h 前(阈值 = 两个定点空档 = 8h*2 = 16h)
    session.add(RunRecord(user_id=1, run_id="r1", kind="wechat_listen", status="success",
                          started_at=datetime.now() - timedelta(hours=30)))
    session.add(WechatArticle(user_id=1, title="老文",
                              created_at=datetime.now() - timedelta(hours=30)))
    session.commit()

    sent: list[str] = []
    monkeypatch.setattr(feishu_client, "FeishuClient",
                        lambda w, s: type("F", (), {"send": lambda self, t: (sent.append(t), True)[1]})())
    n = alert_service.check_health_stalls(_settings(health_stall_hours=24), db=session)
    assert n == 1
    assert "公众号" in sent[0] and "停摆" in sent[0]
    # 讯息要给出**这一条**的具体成因,不是一堆通用词
    assert "监听轮次" in sent[0]


def test_health_stalls_wechat_running_quietly_is_not_stall(monkeypatch, session) -> None:
    """⚠️ 与上一条配对的**反例**:轮次在跑、只是这几小时没人发文 ⇒ **不算停摆**。

    判据落在**运行记录**而不是"有没有新文章",为的就是把这两件事分开 ——
    否则一个安静的凌晨就会换来一次假警,而假警会训练人忽略告警。
    """
    from datetime import datetime, timedelta

    from app.db.models import RunRecord, UserCookie, UserSchedule
    from app.services import alert_service

    session.add(UserSchedule(user_id=1, section="wechat", interval_minutes=60, enabled=True))
    session.add(UserCookie(user_id=1, platform="weread", cookie="x"))
    session.add(RunRecord(user_id=1, run_id="r1", kind="wechat_listen", status="success",
                          started_at=datetime.now() - timedelta(hours=2)))
    session.commit()          # 注意:一篇新文章都没有

    sent: list[str] = []
    monkeypatch.setattr(feishu_client, "FeishuClient",
                        lambda w, s: type("F", (), {"send": lambda self, t: (sent.append(t), True)[1]})())
    assert alert_service.check_health_stalls(_settings(health_stall_hours=24), db=session) == 0
    assert not sent


def test_health_stalls_wechat_only_success_counts(monkeypatch, session) -> None:
    """失败轮次不算"在跑" —— 只有 success/partial 才重置停摆计时。

    否则"每轮都失败"会永远看着像"轮次在跑",正是这条告警要抓的东西。
    """
    from datetime import datetime, timedelta

    from app.db.models import RunRecord, UserCookie, UserSchedule
    from app.services import alert_service

    session.add(UserSchedule(user_id=1, section="wechat", interval_minutes=60, enabled=True))
    session.add(UserCookie(user_id=1, platform="weread", cookie="x"))
    # 最近两小时一直在**失败**;最后一次成功在 30h 前
    session.add(RunRecord(user_id=1, run_id="ok", kind="wechat_listen", status="success",
                          started_at=datetime.now() - timedelta(hours=30)))
    for i in range(3):
        session.add(RunRecord(user_id=1, run_id=f"f{i}", kind="wechat_listen", status="failed",
                              started_at=datetime.now() - timedelta(minutes=10 * (i + 1))))
    session.commit()

    sent: list[str] = []
    monkeypatch.setattr(feishu_client, "FeishuClient",
                        lambda w, s: type("F", (), {"send": lambda self, t: (sent.append(t), True)[1]})())
    assert alert_service.check_health_stalls(_settings(health_stall_hours=24), db=session) == 1
