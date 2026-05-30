"""Response cache for the Claude Code proxy.

Caches upstream responses based on a hash of the request payload,
enabling identical requests to be served from cache without calling
the upstream API again.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("proxy.cache")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CACHE_ENABLED = os.getenv("CACHE_ENABLED", "false").lower() in ("true", "1", "yes")
CACHE_TTL = int(os.getenv("CACHE_TTL", "3600"))  # seconds
CACHE_DIR = os.getenv("CACHE_DIR", ".cache")
CACHE_DB = str(Path(CACHE_DIR) / "responses.db")


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class CacheEntry:
    key: str
    value: bytes  # JSON serialized response
    created_at: float
    expires_at: float
    hit_count: int = 0

    def is_expired(self) -> bool:
        return time.time() > self.expires_at


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    inserts: int = 0
    evictions: int = 0
    total_response_time_saved_ms: float = 0.0


# ---------------------------------------------------------------------------
# Cache backend
# ---------------------------------------------------------------------------

class ResponseCache:
    """Thread-safe SQLite-backed response cache."""

    def __init__(self, db_path: str = CACHE_DB, ttl: int = CACHE_TTL, enabled: bool = CACHE_ENABLED):
        self.db_path = db_path
        self.ttl = ttl
        self.enabled = enabled
        self._stats = CacheStats()
        self._lock = threading.RLock()
        self._init_db()

    def _init_db(self) -> None:
        """Initialize the SQLite database and create tables."""
        if not self.enabled:
            logger.info("Cache disabled via CACHE_ENABLED=false")
            return

        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS responses (
                key TEXT PRIMARY KEY,
                value BLOB NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                hit_count INTEGER DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_expires_at ON responses(expires_at)
        """)
        conn.commit()
        conn.close()
        logger.info("Cache initialized at %s (TTL=%ds)", self.db_path, self.ttl)

    def _generate_key(self, request_body: dict, model: str) -> str:
        """
        Generate a cache key from the request body and model.

        Key = SHA256(model + sorted(json(request_body)))
        We exclude fields that make requests unique but shouldn't affect
        caching (e.g., temperature randomness, but NOT when seed is used).
        """
        # Create a copy for hashing - exclude certain fields
        cacheable_body = self._get_cacheable_body(request_body)

        # Serialize and hash
        canonical = json.dumps(cacheable_body, sort_keys=True, ensure_ascii=False)
        key_input = f"{model}:{canonical}"
        return hashlib.sha256(key_input.encode()).hexdigest()

    def _get_cacheable_body(self, body: dict) -> dict:
        """Extract cacheable parts of request body, excluding volatile fields."""
        cacheable = {}
        for key, value in body.items():
            # Exclude fields that change per request but don't affect semantics
            if key in ("stream", "temperature", "top_p", "stop_sequences"):
                continue
            # max_tokens doesn't affect response content for caching purposes
            if key == "max_tokens" and value is not None:
                continue
            cacheable[key] = value
        return cacheable

    def get(self, request_body: dict, model: str) -> tuple[dict | None, int]:
        """
        Retrieve a cached response if available and not expired.

        Returns:
            (cached_response_dict, response_time_ms_saved) or (None, 0) if not cached.
        """
        if not self.enabled:
            return None, 0

        key = self._generate_key(request_body, model)

        with self._lock:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                "SELECT value, created_at, expires_at, hit_count FROM responses WHERE key = ?",
                (key,),
            )
            row = cursor.fetchone()
            conn.close()

            if row is None:
                self._stats.misses += 1
                logger.debug("Cache MISS for key %s", key[:16])
                return None, 0

            expires_at = row["expires_at"]
            if time.time() > expires_at:
                self._evict_key(key)
                self._stats.misses += 1
                logger.debug("Cache EXPIRED for key %s", key[:16])
                return None, 0

            # Update hit count
            with self._lock:
                conn = sqlite3.connect(self.db_path, check_same_thread=False)
                conn.execute(
                    "UPDATE responses SET hit_count = hit_count + 1 WHERE key = ?",
                    (key,),
                )
                conn.commit()
                conn.close()

            self._stats.hits += 1
            response_time_saved = (expires_at - time.time()) * 1000  # ms
            self._stats.total_response_time_saved_ms += response_time_saved
            logger.info("Cache HIT for key %s (saved ~%.0fms)", key[:16], response_time_saved)

            try:
                cached = json.loads(row["value"])
                return cached, int(response_time_saved)
            except (json.JSONDecodeError, TypeError):
                self._evict_key(key)
                return None, 0

    def set(self, request_body: dict, model: str, response: dict) -> None:
        """Store a response in the cache with TTL."""
        if not self.enabled:
            return

        key = self._generate_key(request_body, model)
        now = time.time()
        expires_at = now + self.ttl

        value_bytes = json.dumps(response, ensure_ascii=False).encode()

        with self._lock:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.execute(
                """INSERT OR REPLACE INTO responses (key, value, created_at, expires_at, hit_count)
                   VALUES (?, ?, ?, ?, 0)""",
                (key, value_bytes, now, expires_at),
            )
            conn.commit()
            conn.close()

        self._stats.inserts += 1
        logger.debug("Cache INSERT for key %s (expires in %ds)", key[:16], self.ttl)

    def _evict_key(self, key: str) -> None:
        """Remove a specific key from the cache."""
        with self._lock:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.execute("DELETE FROM responses WHERE key = ?", (key,))
            conn.commit()
            conn.close()
        self._stats.evictions += 1

    def clear_expired(self) -> int:
        """Remove all expired entries. Returns count of evicted entries."""
        if not self.enabled:
            return 0

        now = time.time()
        with self._lock:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            cursor = conn.execute("DELETE FROM responses WHERE expires_at < ?", (now,))
            conn.commit()
            conn.close()
        evicted = cursor.rowcount
        if evicted > 0:
            logger.info("Evicted %d expired cache entries", evicted)
        return evicted

    def clear_all(self) -> int:
        """Remove all entries from the cache. Returns count of evicted entries."""
        if not self.enabled:
            return 0

        with self._lock:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            cursor = conn.execute("DELETE FROM responses")
            conn.commit()
            conn.close()
        evicted = cursor.rowcount
        logger.info("Cleared all %d cache entries", evicted)
        return evicted

    def stats(self) -> dict:
        """Return current cache statistics."""
        with self._lock:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            cursor = conn.execute("SELECT COUNT(*) as total, SUM(hit_count) as hits FROM responses")
            row = cursor.fetchone()
            conn.close()

        return {
            "enabled": self.enabled,
            "ttl_seconds": self.ttl,
            "total_entries": row["total"] if row else 0,
            "hits": self._stats.hits,
            "misses": self._stats.misses,
            "inserts": self._stats.inserts,
            "evictions": self._stats.evictions,
            "total_response_time_saved_ms": self._stats.total_response_time_saved_ms,
            "hit_rate": (
                self._stats.hits / (self._stats.hits + self._stats.misses)
                if (self._stats.hits + self._stats.misses) > 0
                else 0.0
            ),
        }


# ---------------------------------------------------------------------------
# Streaming cache
# ---------------------------------------------------------------------------

class StreamingCache:
    """Cache for streaming responses. Stores complete SSE event sequences."""

    def __init__(self, db_path: str = CACHE_DB, ttl: int = CACHE_TTL, enabled: bool = CACHE_ENABLED):
        self.db_path = db_path
        self.ttl = ttl
        self.enabled = enabled
        self._stats = CacheStats()
        self._lock = threading.RLock()
        self._init_db()

    def _init_db(self) -> None:
        """Initialize the streaming cache database."""
        if not self.enabled:
            return

        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS streaming_responses (
                key TEXT PRIMARY KEY,
                events BLOB NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                hit_count INTEGER DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_stream_expires ON streaming_responses(expires_at)
        """)
        conn.commit()
        conn.close()

    def _generate_key(self, request_body: dict, model: str) -> str:
        """Generate cache key for streaming responses."""
        cacheable_body = {}
        for key, value in request_body.items():
            if key in ("stream",):
                continue
            cacheable_body[key] = value

        canonical = json.dumps(cacheable_body, sort_keys=True, ensure_ascii=False)
        key_input = f"{model}:{canonical}"
        return hashlib.sha256(key_input.encode()).hexdigest()

    def get_events(self, request_body: dict, model: str) -> tuple[list[str] | None, int]:
        """
        Retrieve cached streaming events if available and not expired.

        Returns:
            (list_of_sse_event_strings, response_time_ms_saved) or (None, 0).
        """
        if not self.enabled:
            return None, 0

        key = self._generate_key(request_body, model)

        with self._lock:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                "SELECT events, created_at, expires_at, hit_count FROM streaming_responses WHERE key = ?",
                (key,),
            )
            row = cursor.fetchone()
            conn.close()

        if row is None:
            self._stats.misses += 1
            logger.debug("Streaming cache MISS for key %s", key[:16])
            return None, 0

        if time.time() > row["expires_at"]:
            self._evict_key(key)
            self._stats.misses += 1
            logger.debug("Streaming cache EXPIRED for key %s", key[:16])
            return None, 0

        # Update hit count
        with self._lock:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.execute(
                "UPDATE streaming_responses SET hit_count = hit_count + 1 WHERE key = ?",
                (key,),
            )
            conn.commit()
            conn.close()

        self._stats.hits += 1
        response_time_saved = (row["expires_at"] - time.time()) * 1000
        self._stats.total_response_time_saved_ms += response_time_saved
        logger.info("Streaming cache HIT for key %s (saved ~%.0fms)", key[:16], response_time_saved)

        try:
            events = json.loads(row["events"])
            return events, int(response_time_saved)
        except (json.JSONDecodeError, TypeError):
            self._evict_key(key)
            return None, 0

    def set_events(self, request_body: dict, model: str, events: list[str]) -> None:
        """Store streaming events in cache."""
        if not self.enabled:
            return

        key = self._generate_key(request_body, model)
        now = time.time()
        expires_at = now + self.ttl
        events_bytes = json.dumps(events, ensure_ascii=False).encode()

        with self._lock:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.execute(
                """INSERT OR REPLACE INTO streaming_responses (key, events, created_at, expires_at, hit_count)
                   VALUES (?, ?, ?, ?, 0)""",
                (key, events_bytes, now, expires_at),
            )
            conn.commit()
            conn.close()

        self._stats.inserts += 1
        logger.debug("Streaming cache INSERT for key %s", key[:16])

    def _evict_key(self, key: str) -> None:
        """Remove a specific key from the streaming cache."""
        with self._lock:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.execute("DELETE FROM streaming_responses WHERE key = ?", (key,))
            conn.commit()
            conn.close()
        self._stats.evictions += 1

    def stats(self) -> dict:
        """Return current streaming cache statistics."""
        with self._lock:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            cursor = conn.execute("SELECT COUNT(*) as total FROM streaming_responses")
            row = cursor.fetchone()
            conn.close()

        return {
            "enabled": self.enabled,
            "ttl_seconds": self.ttl,
            "total_entries": row["total"] if row else 0,
            "hits": self._stats.hits,
            "misses": self._stats.misses,
            "inserts": self._stats.inserts,
            "evictions": self._stats.evictions,
            "total_response_time_saved_ms": self._stats.total_response_time_saved_ms,
            "hit_rate": (
                self._stats.hits / (self._stats.hits + self._stats.misses)
                if (self._stats.hits + self._stats.misses) > 0
                else 0.0
            ),
        }


# ---------------------------------------------------------------------------
# Global cache instances
# ---------------------------------------------------------------------------

# Global cache instances - initialized in server.py lifespan
response_cache: ResponseCache | None = None
streaming_cache: StreamingCache | None = None


def init_caches() -> tuple[ResponseCache, StreamingCache]:
    """Initialize both response and streaming caches."""
    global response_cache, streaming_cache
    response_cache = ResponseCache()
    streaming_cache = StreamingCache()
    return response_cache, streaming_cache


def get_response_cache() -> ResponseCache | None:
    return response_cache


def get_streaming_cache() -> StreamingCache | None:
    return streaming_cache