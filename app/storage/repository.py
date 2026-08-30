"""归档与持久化(见 doc/dev.md §5.7)。

`ArchiveRepository` 用标准库 `sqlite3` 落盘运行记录、热搜条目、趋势分析、告警,
并额外写 JSON 快照便于人工排查。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from app.models import Alert, HotItem, IndexPoint, TrendAnalysis


def _parse_iso(value: str | None) -> datetime:
    """解析存储的 ISO 时间字符串,失败时回退当前时间。"""
    if not value:
        return datetime.now()
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return datetime.now()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    items_collected INTEGER DEFAULT 0,
    analyses_count INTEGER DEFAULT 0,
    rising_count INTEGER DEFAULT 0,
    error TEXT
);
CREATE TABLE IF NOT EXISTS hot_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    rank INTEGER,
    title TEXT NOT NULL,
    heat INTEGER,
    category TEXT,
    tag TEXT,
    captured_at TEXT
);
CREATE TABLE IF NOT EXISTS trend_analysis (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    keyword TEXT NOT NULL,
    source TEXT,
    growth REAL,
    slope REAL,
    rising INTEGER,
    decided_at TEXT
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    keyword TEXT NOT NULL,
    reason TEXT,
    triggered_at TEXT
);
CREATE TABLE IF NOT EXISTS xianyu_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    item_id TEXT,
    title TEXT,
    price TEXT,
    seller TEXT,
    pic TEXT,
    hit_keywords INTEGER,
    best_rank INTEGER,
    keywords TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS xianyu_summary (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    summary_date TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    created_at TEXT
);
"""


class ArchiveRepository:
    """SQLite 归档仓库。线程安全,单进程内共享一个连接(加锁)。"""

    def __init__(self, data_dir: str | Path) -> None:
        self._dir = Path(data_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._db_path = self._dir / "monitor.db"
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "ArchiveRepository":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        self.close()
        return None

    # ---- 记录一次运行 ----
    def save_run(self, run: dict) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO runs (run_id, status, started_at, finished_at,
                                  items_collected, analyses_count, rising_count, error)
                VALUES (:run_id, :status, :started_at, :finished_at,
                        :items_collected, :analyses_count, :rising_count, :error)
                ON CONFLICT(run_id) DO UPDATE SET
                    status=excluded.status,
                    finished_at=excluded.finished_at,
                    items_collected=excluded.items_collected,
                    analyses_count=excluded.analyses_count,
                    rising_count=excluded.rising_count,
                    error=excluded.error
                """,
                run,
            )
            self._conn.commit()

    def save_items(self, run_id: str, items: list[HotItem]) -> None:
        with self._lock:
            self._conn.executemany(
                """
                INSERT INTO hot_items (run_id, rank, title, heat, category, tag, captured_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        run_id,
                        it.rank,
                        it.title,
                        it.heat,
                        it.category,
                        it.tag,
                        it.captured_at.isoformat(),
                    )
                    for it in items
                ],
            )
            self._conn.commit()

    def save_analysis(self, run_id: str, analyses: list[TrendAnalysis]) -> None:
        with self._lock:
            self._conn.executemany(
                """
                INSERT INTO trend_analysis (run_id, keyword, source, growth, slope, rising, decided_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        run_id,
                        a.keyword,
                        a.source.value,
                        a.growth,
                        a.slope,
                        int(a.rising),
                        a.decided_at.isoformat(),
                    )
                    for a in analyses
                ],
            )
            self._conn.commit()

    def save_alert(self, run_id: str, alert: Alert) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO alerts (run_id, keyword, reason, triggered_at)
                VALUES (?, ?, ?, ?)
                """,
                (run_id, alert.keyword, alert.reason, alert.triggered_at.isoformat()),
            )
            self._conn.commit()

    def save_xianyu_top(self, run_id: str, items: list[dict]) -> None:
        with self._lock:
            self._conn.executemany(
                """
                INSERT INTO xianyu_items
                    (run_id, item_id, title, price, seller, pic, hit_keywords, best_rank, keywords, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        run_id,
                        it["item_id"],
                        it["title"],
                        it["price"],
                        it["seller"],
                        it["pic"],
                        it["hit_keywords"],
                        it["best_rank"],
                        it["keywords"],
                        datetime.now().isoformat(),
                    )
                    for it in items
                ],
            )
            self._conn.commit()

    # ---- 查询 ----
    def latest_xianyu(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT item_id, title, price, seller, pic, hit_keywords, best_rank, keywords, created_at
                FROM xianyu_items
                WHERE run_id = (SELECT run_id FROM xianyu_items ORDER BY id DESC LIMIT 1)
                ORDER BY hit_keywords DESC, best_rank ASC, id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def xianyu_items_between(self, start_iso: str) -> list[dict]:
        """返回指定起始时间之后的闲鱼热榜条目(用于当日聚合)。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT item_id, title, price, seller, hit_keywords, best_rank, keywords, created_at "
                "FROM xianyu_items WHERE created_at >= ? ORDER BY id ASC",
                (start_iso,),
            ).fetchall()
        return [dict(r) for r in rows]

    def save_xianyu_summary(self, summary_date: str, items: list[dict]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO xianyu_summary (summary_date, summary_json, created_at) VALUES (?, ?, ?)",
                (summary_date, json.dumps(items, ensure_ascii=False), datetime.now().isoformat()),
            )
            self._conn.commit()

    def latest_xianyu_summary(self) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT summary_date, summary_json, created_at FROM xianyu_summary "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if not row:
            return None
        return {
            "summary_date": row["summary_date"],
            "created_at": row["created_at"],
            "items": json.loads(row["summary_json"]),
        }

    def xianyu_summary_before(self, date_str: str) -> dict | None:
        """返回严格早于给定日期的最近一次总结(用于"较昨日"对比)。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT summary_date, summary_json, created_at FROM xianyu_summary "
                "WHERE summary_date < ? ORDER BY id DESC LIMIT 1",
                (date_str,),
            ).fetchone()
        if not row:
            return None
        return {
            "summary_date": row["summary_date"],
            "created_at": row["created_at"],
            "items": json.loads(row["summary_json"]),
        }

    def latest_analysis(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT keyword, source, growth, slope, rising, decided_at
                FROM trend_analysis
                WHERE rising = 1
                ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def latest_alerts(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT keyword, reason, triggered_at FROM alerts ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def latest_run(self) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM runs ORDER BY run_id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def keyword_heat_series(self, keyword: str, limit: int = 30) -> list[IndexPoint]:
        """按采集先后返回某关键词的热度时间序列(每轮一个采样点)。

        用于"微博热度序列"指数源:跨多轮调度累积,形成可供趋势分析的时间序列。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT captured_at, heat FROM hot_items WHERE title = ? ORDER BY id ASC",
                (keyword,),
            ).fetchall()
        points: list[IndexPoint] = []
        for row in rows:
            ts = _parse_iso(row["captured_at"])
            points.append(IndexPoint(ts=ts, value=float(row["heat"])))
        return points[-limit:]

    # ---- JSON 快照 ----
    def snapshot(self, run_id: str, payload: dict) -> Path:
        snap_dir = self._dir / "snapshots"
        snap_dir.mkdir(parents=True, exist_ok=True)
        path = snap_dir / f"{run_id}.json"
        with self._lock:
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return path
