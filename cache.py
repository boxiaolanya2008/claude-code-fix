"""缓存层，SQLite 做后端。

不同提供商的 token 计数不一样，所以缓存只存响应内容。
命中时 token 全部归零，不带错数据。

CACHE_KEY_MODE 控制缓存粒度：
  full     — hash 整个请求体，精确匹配，命中率低但最准确
  prefix   — 只看 system + 最后一条 user 消息 + tools，忽略历史，命中率高
  none     — 关闭缓存
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("proxy.cache")

# 配置从 .env 读
CACHE_ENABLED = os.getenv("CACHE_ENABLED", "false").lower() in ("true", "1", "yes")
CACHE_TTL = int(os.getenv("CACHE_TTL", "3600"))
CACHE_DIR = os.getenv("CACHE_DIR", ".cache")
CACHE_DB = str(Path(CACHE_DIR) / "responses.db")
# full=精确匹配整个请求，prefix=只看system+最后一条user消息+tools，none=关缓存
CACHE_KEY_MODE = os.getenv("CACHE_KEY_MODE", "prefix").strip().lower()


def _env(key, default=""):
    return os.getenv(key, default).strip()


def _get_prefix_body(req_body):
    """提取缓存前缀：system + 最后一条 user 消息 + tools + model。

    Claude Code 每轮都发完整对话历史，messages 越来越长，
    如果 hash 全部内容，几乎永远不命中。
    用 prefix 模式只看"系统设定 + 当前问题 + 工具定义"，
    相同问题在不同对话轮次里都能命中。
    """
    prefix = {}
    # system prompt
    if system := req_body.get("system"):
        prefix["system"] = system
    # tools 定义
    if tools := req_body.get("tools"):
        prefix["tools"] = tools
    # 最后一条 user 消息
    msgs = req_body.get("messages", [])
    for msg in reversed(msgs):
        if msg.get("role") == "user":
            prefix["last_user"] = msg.get("content", "")
            break
    # model 和可选参数
    for k in ("model", "max_tokens", "stop_sequences"):
        if v := req_body.get(k):
            prefix[k] = v
    return prefix


@dataclass
class CacheStats:
    hits = 0
    misses = 0
    inserts = 0
    evictions = 0
    saved_ms = 0.0


class ResponseCache:
    """非流式响应缓存，SQLite 存储，线程安全。"""

    def __init__(self, db_path=CACHE_DB, ttl=CACHE_TTL, enabled=CACHE_ENABLED):
        self.db_path = db_path
        self.ttl = ttl
        self.enabled = enabled
        self._stats = CacheStats()
        self._lock = threading.RLock()
        if self.enabled:
            self._init_db()
        else:
            logger.info("缓存关着呢 (CACHE_ENABLED=false)")

    def _init_db(self):
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
        conn.execute("CREATE INDEX IF NOT EXISTS idx_expires_at ON responses(expires_at)")
        conn.commit()
        conn.close()
        logger.info("缓存就绪: %s (TTL=%ds, key=%s)", self.db_path, self.ttl, CACHE_KEY_MODE)

    def _gen_key(self, req_body, model):
        """根据 CACHE_KEY_MODE 生成缓存 key。"""
        if CACHE_KEY_MODE == "prefix":
            body = _get_prefix_body(req_body)
        else:
            body = {}
            for k, v in req_body.items():
                if k in ("stream", "temperature", "top_p", "stop_sequences"):
                    continue
                if k == "max_tokens" and v is not None:
                    continue
                if k in ("ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL"):
                    continue
                body[k] = v
        canonical = json.dumps(body, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(f"{model}:{canonical}".encode()).hexdigest()

    def get(self, req_body, model):
        """查缓存。命中返回 (响应, 省了多少ms)，没命中返回 (None, 0)。"""
        if not self.enabled:
            return None, 0

        key = self._gen_key(req_body, model)
        with self._lock:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT value, expires_at, hit_count FROM responses WHERE key = ?",
                (key,),
            )
            row = cur.fetchone()

            if row is None:
                conn.close()
                self._stats.misses += 1
                return None, 0

            if time.time() > row["expires_at"]:
                conn.execute("DELETE FROM responses WHERE key = ?", (key,))
                conn.commit()
                conn.close()
                self._stats.misses += 1
                return None, 0

            # 命中了，更新计数
            conn.execute("UPDATE responses SET hit_count = hit_count + 1 WHERE key = ?", (key,))
            conn.commit()
            conn.close()

            self._stats.hits += 1
            saved = int((row["expires_at"] - time.time()) * 1000)
            self._stats.saved_ms += saved

            try:
                cached = json.loads(row["value"])
                # token 清零，缓存的是旧提供商的数
                if "usage" in cached:
                    cached["usage"] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
                for ch in cached.get("choices", []):
                    if "usage" in ch:
                        ch["usage"] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
                return cached, saved
            except (json.JSONDecodeError, TypeError):
                c2 = sqlite3.connect(self.db_path, check_same_thread=False)
                c2.execute("DELETE FROM responses WHERE key = ?", (key,))
                c2.commit()
                c2.close()
                return None, 0

    def set(self, req_body, model, resp):
        """存缓存，带 TTL。"""
        if not self.enabled:
            return

        key = self._gen_key(req_body, model)
        now = time.time()
        val = json.dumps(resp, ensure_ascii=False).encode()

        with self._lock:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.execute(
                "INSERT OR REPLACE INTO responses VALUES (?, ?, ?, ?, 0)",
                (key, val, now, now + self.ttl),
            )
            conn.commit()
            conn.close()
        self._stats.inserts += 1

    def clear_expired(self):
        """清过期的。"""
        if not self.enabled:
            return 0
        with self._lock:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            cur = conn.execute("DELETE FROM responses WHERE expires_at < ?", (time.time(),))
            conn.commit()
            n = cur.rowcount
            conn.close()
        if n > 0:
            logger.info("清掉 %d 条过期缓存", n)
        return n

    def clear_all(self):
        """全清。"""
        if not self.enabled:
            return 0
        with self._lock:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            cur = conn.execute("DELETE FROM responses")
            conn.commit()
            n = cur.rowcount
            conn.close()
        logger.info("全部清空: %d 条", n)
        return n

    def stats(self):
        with self._lock:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            cur = conn.execute("SELECT COUNT(*) as t, SUM(hit_count) as h FROM responses")
            r = cur.fetchone()
            conn.close()
        total = self._stats.hits + self._stats.misses
        return {
            "enabled": self.enabled,
            "ttl": self.ttl,
            "entries": r["t"] if r else 0,
            "hits": self._stats.hits,
            "misses": self._stats.misses,
            "inserts": self._stats.inserts,
            "evictions": self._stats.evictions,
            "saved_ms": self._stats.saved_ms,
            "hit_rate": self._stats.hits / total if total > 0 else 0.0,
        }


class StreamingCache:
    """流式缓存，存完整 SSE 事件序列。"""

    def __init__(self, db_path=CACHE_DB, ttl=CACHE_TTL, enabled=CACHE_ENABLED):
        self.db_path = db_path
        self.ttl = ttl
        self.enabled = enabled
        self._stats = CacheStats()
        self._lock = threading.RLock()
        if self.enabled:
            self._init_db()

    def _init_db(self):
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
        conn.execute("CREATE INDEX IF NOT EXISTS idx_stream_expires ON streaming_responses(expires_at)")
        conn.commit()
        conn.close()

    def _gen_key(self, req_body, model):
        """跟 ResponseCache 用一样的 key 生成逻辑。"""
        if CACHE_KEY_MODE == "prefix":
            body = _get_prefix_body(req_body)
        else:
            body = {}
            for k, v in req_body.items():
                if k in ("stream", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL"):
                    continue
                body[k] = v
        canonical = json.dumps(body, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(f"{model}:{canonical}".encode()).hexdigest()

    def get_events(self, req_body, model):
        """查流式缓存，命中返回 (SSE 事件列表, 省了多少ms)。"""
        if not self.enabled:
            return None, 0

        key = self._gen_key(req_body, model)
        with self._lock:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT events, expires_at, hit_count FROM streaming_responses WHERE key = ?",
                (key,),
            )
            row = cur.fetchone()

            if row is None:
                conn.close()
                self._stats.misses += 1
                return None, 0

            if time.time() > row["expires_at"]:
                conn.execute("DELETE FROM streaming_responses WHERE key = ?", (key,))
                conn.commit()
                conn.close()
                self._stats.misses += 1
                return None, 0

            conn.execute("UPDATE streaming_responses SET hit_count = hit_count + 1 WHERE key = ?", (key,))
            conn.commit()
            conn.close()

            self._stats.hits += 1
            saved = int((row["expires_at"] - time.time()) * 1000)
            self._stats.saved_ms += saved

            try:
                events = json.loads(row["events"])
                events = _rst_tokens(events)
                return events, saved
            except (json.JSONDecodeError, TypeError):
                self._evict(key)
                return None, 0

    def set_events(self, req_body, model, events):
        """存 SSE 事件。"""
        if not self.enabled:
            return

        key = self._gen_key(req_body, model)
        now = time.time()
        val = json.dumps(events, ensure_ascii=False).encode()

        with self._lock:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.execute(
                "INSERT OR REPLACE INTO streaming_responses VALUES (?, ?, ?, ?, 0)",
                (key, val, now, now + self.ttl),
            )
            conn.commit()
            conn.close()
        self._stats.inserts += 1

    def _evict(self, key):
        with self._lock:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.execute("DELETE FROM streaming_responses WHERE key = ?", (key,))
            conn.commit()
            conn.close()
        self._stats.evictions += 1

    def stats(self):
        with self._lock:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            cur = conn.execute("SELECT COUNT(*) as t FROM streaming_responses")
            r = cur.fetchone()
            conn.close()
        total = self._stats.hits + self._stats.misses
        return {
            "enabled": self.enabled,
            "ttl": self.ttl,
            "entries": r["t"] if r else 0,
            "hits": self._stats.hits,
            "misses": self._stats.misses,
            "inserts": self._stats.inserts,
            "evictions": self._stats.evictions,
            "saved_ms": self._stats.saved_ms,
            "hit_rate": self._stats.hits / total if total > 0 else 0.0,
        }


def _rst_tokens(events):
    """把 SSE 事件里的 token 数清零。

    事件是字符串，要解析 JSON 才能改。
    只动 message_delta 里的 usage，其他原样返回。
    """
    out = []
    for line in events:
        if "message_delta" in line and "usage" in line:
            try:
                start = line.find("data: ") + 6
                end = line.find("\n\n", start)
                if end == -1:
                    end = len(line)
                obj = json.loads(line[start:end])
                if "usage" in obj:
                    obj["usage"]["output_tokens"] = 0
                    if "input_tokens" in obj["usage"]:
                        obj["usage"]["input_tokens"] = 0
                prefix = "event: message_delta\n" if "event:" not in line else ""
                line = f"{prefix}data: {json.dumps(obj, ensure_ascii=False)}\n\n"
            except (json.JSONDecodeError, ValueError):
                pass
        out.append(line)
    return out


# 全局实例，server.py 启动时 init_caches() 初始化

response_cache = None
streaming_cache = None


def init_caches():
    global response_cache, streaming_cache
    response_cache = ResponseCache()
    streaming_cache = StreamingCache()
    return response_cache, streaming_cache


def get_response_cache():
    return response_cache


def get_streaming_cache():
    return streaming_cache
