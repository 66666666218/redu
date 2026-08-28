"""指数解析容错提取器单测(纯逻辑,无网络)。"""
from app.services.index_fetcher import _coerce_dt, _find_trend_points


def test_parse_douyin_like_payload() -> None:
    # 模拟巨量算数响应结构
    payload = {
        "code": 0,
        "data": {
            "trend": [
                {"date": "2026-08-27", "value": 100},
                {"date": "2026-08-28", "value": 150},
                {"date": "2026-08-29", "value": 210},
            ]
        },
    }
    points = _find_trend_points(payload)
    assert len(points) == 3
    assert [p.value for p in points] == [100.0, 150.0, 210.0]
    # 时间升序
    assert points[0].ts <= points[1].ts <= points[2].ts


def test_parse_baidu_like_payload() -> None:
    # 模拟百度指数 userIndexes.allData 结构
    payload = {
        "status": 0,
        "data": {
            "userIndexes": [
                {
                    "allData": [
                        {"day": "2026-08-01", "value": 500},
                        {"day": "2026-08-02", "value": 620},
                    ]
                }
            ]
        },
    }
    points = _find_trend_points(payload)
    assert len(points) == 2
    assert [p.value for p in points] == [500.0, 620.0]


def test_empty_when_no_trend() -> None:
    payload = {"data": {"a": 1, "b": [1, 2, 3], "c": {"x": "y"}}}
    assert _find_trend_points(payload) == []


def test_picks_largest_series() -> None:
    payload = {"small": [{"date": "2026-08-01", "value": 1}, {"date": "2026-08-02", "value": 2}],
               "big": [{"date": "2026-08-01", "value": i} for i in range(10)]}
    points = _find_trend_points(payload)
    assert len(points) == 10


def test_number_keys_variants() -> None:
    payload = {"rows": [{"ds": 1690000000, "num": 10}, {"ds": 1690003600, "num": 12}]}
    points = _find_trend_points(payload)
    assert len(points) == 2
    assert points[0].value == 10.0


def test_coerce_dt_formats() -> None:
    assert _coerce_dt("2026-08-29") is not None
    assert _coerce_dt("2026-08-29 10:00:00") is not None
    assert _coerce_dt(1690000000) is not None
    assert _coerce_dt("not-a-date") is None
    assert _coerce_dt(None) is None
