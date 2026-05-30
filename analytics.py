"""缓存分析数据库，单独存每次缓存事件。

跟缓存数据库分开，不影响缓存本身的读写。
每次缓存查询都会记录一条事件，用于仪表盘展示命中率趋势。
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path

ANALYTICS_DIR = os.getenv("CACHE_DIR", ".cache")
ANALYTICS_DB = str(Path(ANALYTICS_DIR) / "analytics.db")

_lock = threading.Lock()


def _conn():
    return sqlite3.connect(ANALYTICS_DB, check_same_thread=False)


def init_analytics():
    """建表，启动时调一次。"""
    os.makedirs(os.path.dirname(ANALYTICS_DB), exist_ok=True)
    conn = _conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cache_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            cache_name TEXT NOT NULL,
            hit INTEGER NOT NULL,
            response_time_ms INTEGER DEFAULT 0,
            model TEXT DEFAULT '',
            key_prefix TEXT DEFAULT ''
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON cache_events(ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_name ON cache_events(cache_name)")
    conn.commit()
    conn.close()


def record_event(cache_name, hit, response_time_ms=0, model="", key_prefix=""):
    """记一条缓存事件。非阻塞，写失败也不影响主流程。"""
    try:
        conn = _conn()
        conn.execute(
            "INSERT INTO cache_events (ts, cache_name, hit, response_time_ms, model, key_prefix) VALUES (?, ?, ?, ?, ?, ?)",
            (time.time(), cache_name, 1 if hit else 0, response_time_ms, model, key_prefix[:16]),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def get_summary():
    """总览数据：总请求数、命中数、未命中数、命中率、平均响应时间。"""
    with _lock:
        conn = _conn()
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT COUNT(*) as total, SUM(hit) as hits, AVG(response_time_ms) as avg_rt FROM cache_events"
        )
        row = cur.fetchone()
        conn.close()

    total = row["total"] or 0
    hits = row["hits"] or 0
    misses = total - hits
    avg_rt = row["avg_rt"] or 0

    # 按缓存类型拆分
    by_cache = _get_by_cache()

    return {
        "total": total,
        "hits": hits,
        "misses": misses,
        "hit_rate": hits / total if total > 0 else 0,
        "avg_response_time_ms": round(avg_rt, 1),
        "by_cache": by_cache,
    }


def _get_by_cache():
    """按缓存类型 (response / streaming) 分组统计。"""
    with _lock:
        conn = _conn()
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT cache_name, COUNT(*) as total, SUM(hit) as hits FROM cache_events GROUP BY cache_name"
        )
        rows = cur.fetchall()
        conn.close()

    result = {}
    for r in rows:
        name = r["cache_name"]
        total = r["total"] or 0
        hits = r["hits"] or 0
        result[name] = {
            "total": total,
            "hits": hits,
            "misses": total - hits,
            "hit_rate": hits / total if total > 0 else 0,
        }
    return result


def get_trend(seconds=3600, bucket=60):
    """按时间桶聚合，画趋势图用。

    seconds: 回溯多少秒
    bucket: 每个桶多少秒
    """
    cutoff = time.time() - seconds

    with _lock:
        conn = _conn()
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            """
            SELECT
                CAST((ts - ?) / ? AS INTEGER) as bucket_idx,
                COUNT(*) as total,
                SUM(hit) as hits,
                AVG(response_time_ms) as avg_rt
            FROM cache_events
            WHERE ts >= ?
            GROUP BY bucket_idx
            ORDER BY bucket_idx
            """,
            (cutoff, bucket, cutoff),
        )
        rows = cur.fetchall()
        conn.close()

    trend = []
    for r in rows:
        t = r["total"] or 0
        h = r["hits"] or 0
        trend.append({
            "time": cutoff + r["bucket_idx"] * bucket,
            "total": t,
            "hits": h,
            "misses": t - h,
            "hit_rate": h / t if t > 0 else 0,
            "avg_rt": round(r["avg_rt"] or 0, 1),
        })
    return trend


def get_recent(limit=50):
    """最近 N 条事件。"""
    with _lock:
        conn = _conn()
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT ts, cache_name, hit, response_time_ms, model, key_prefix FROM cache_events ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        rows = cur.fetchall()
        conn.close()

    return [
        {
            "time": r["ts"],
            "cache": r["cache_name"],
            "hit": bool(r["hit"]),
            "rt_ms": r["response_time_ms"],
            "model": r["model"],
            "key": r["key_prefix"],
        }
        for r in rows
    ]


def cleanup(days=7):
    """清掉 N 天前的数据。"""
    cutoff = time.time() - days * 86400
    with _lock:
        conn = _conn()
        cur = conn.execute("DELETE FROM cache_events WHERE ts < ?", (cutoff,))
        conn.commit()
        n = cur.rowcount
        conn.close()
    return n
