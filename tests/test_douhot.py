"""抖音热点·内容词趋势:解析与仓库单测(纯逻辑,无浏览器/网络)。"""
from app.services.douhot import _parse_word
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
