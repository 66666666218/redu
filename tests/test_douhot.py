"""抖音热点·内容词趋势:解析与仓库单测(纯逻辑,无浏览器/网络)。"""
from config.settings import Settings
from app.services.douhot import _detect_rising, _parse_word, run_douhot_trend
from app.services.notifier import NullNotifier
from app.storage import ArchiveRepository


def test_parse_word_computes_delta() -> None:
    w = {
        "title": "景甜",
        "score": 50707012,
        "rising_ratio": 0,
        "rising_speed": "",
        "query_day": "20260829",
        "trends": [{"date": "01:00", "value": 100}, {"date": "02:00", "value": 300}],
    }
    o = _parse_word(w)
    assert o["title"] == "景甜"
    assert o["score"] == 50707012
    assert o["latest_value"] == 300
    assert o["trend_delta"] == 200
    assert o["trend_len"] == 2


def test_parse_word_no_trends() -> None:
    o = _parse_word({"title": "X", "score": 1, "trends": []})
    assert o["latest_value"] == 0
    assert o["trend_delta"] == 0


def test_repo_save_and_latest_douhot(tmp_path) -> None:
    repo = ArchiveRepository(tmp_path / "data")
    words = [
        {"title": "景甜", "score": 50707012, "rising_ratio": 0, "rising_speed": "",
         "trend_len": 2, "latest_value": 300, "trend_delta": 200, "query_day": "20260829"},
        {"title": "孙宇晨", "score": 38167000, "rising_ratio": 0, "rising_speed": "",
         "trend_len": 1, "latest_value": 100, "trend_delta": 0, "query_day": "20260829"},
    ]
    repo.save_douhot_words("run1", words)
    rows = repo.latest_douhot()
    assert len(rows) == 2
    assert rows[0]["title"] == "景甜"
    assert rows[0]["trend_delta"] == 200
    # min_score 过滤
    assert repo.latest_douhot(min_score=1e8) == []


def _word(title: str, score: float) -> dict:
    return {"title": title, "score": score, "rising_ratio": 0, "rising_speed": "",
            "trend_len": 1, "latest_value": score, "trend_delta": 0, "query_day": ""}


def test_detect_rising_and_cooldown(settings: Settings, tmp_path) -> None:
    repo = ArchiveRepository(tmp_path / "data")
    settings.growth_threshold = 0.3
    repo.save_douhot_words("r1", [_word("景甜", 100)])
    repo.save_douhot_words("r2", [_word("景甜", 160)])  # 环比 +60%
    notifier = NullNotifier()
    rising, alerted = _detect_rising(settings, repo, [{"title": "景甜", "score": 160}], notifier)
    assert len(rising) == 1 and rising[0]["title"] == "景甜"
    assert alerted == ["景甜"]
    # 冷却期内不再重复告警
    _, alerted2 = _detect_rising(settings, repo, [{"title": "景甜", "score": 160}], notifier)
    assert alerted2 == []


def test_run_douhot_trend_rising(settings: Settings, tmp_path, monkeypatch) -> None:
    repo = ArchiveRepository(tmp_path / "data")
    settings.growth_threshold = 0.3
    repo.save_douhot_words("prev", [_word("景甜", 100)])
    monkeypatch.setattr(
        "app.services.douhot.fetch_content_words",
        lambda cf: [{"title": "景甜", "score": 160, "rising_ratio": 0, "rising_speed": "",
                     "trend_len": 1, "latest_value": 160, "trend_delta": 60, "query_day": ""}],
    )
    out = run_douhot_trend(settings=settings, repo=repo, notifier=NullNotifier())
    assert out["count"] == 1
    assert out["rising_count"] == 1
