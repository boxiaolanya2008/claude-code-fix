"""缓存层，支持 SQLite 和 Redis 两种后端。

CACHE_BACKEND 控制后端：
  sqlite  — 默认，本地文件，单机够用
  redis   — 需要装 redis 依赖，适合多实例部署

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

# analytics 模块可选依赖
try:
    from analytics import record_event as _record_event
except ImportError:
    _record_event = None


def _record(cache_name, hit, response_time_ms=0, model="", key_prefix=""):
    """记缓存事件，写失败也不影响主流程。"""
    if _record_event:
        try:
            _record_event(cache_name, hit, response_time_ms, model, key_prefix)
        except Exception:
            pass


# 配置从 .env 读
CACHE_ENABLED = os.getenv("CACHE_ENABLED", "false").lower() in ("true", "1", "yes")
CACHE_TTL = int(os.getenv("CACHE_TTL", "3600"))
CACHE_DIR = os.getenv("CACHE_DIR", ".cache")
CACHE_DB = str(Path(CACHE_DIR) / "responses.db")
CACHE_KEY_MODE = os.getenv("CACHE_KEY_MODE", "prefix").strip().lower()
# sqlite 或 redis
CACHE_BACKEND = os.getenv("CACHE_BACKEND", "sqlite").strip().lower()
# redis 配置
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
REDIS_PREFIX = os.getenv("REDIS_PREFIX", "ccproxy:")


def _env(key, default=""):
    return os.getenv(key, default).strip()


# key 生成工具函数

def _extract_text_content(content):
    """把 Anthropic content 字段统一提成纯字符串。

    只提取 type=text 的内容块，跳过 tool_result 和 tool_use。
    Claude Code 会把 tool_result（文件内容、命令输出）和 text 混在同一条消息里，
    如果把 tool_result 也塞进 key，每次请求的内容都不同，缓存永远不命中。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts) if parts else ""
    return str(content) if content else ""


def _extract_system_text(system):
    """把 system prompt 统一提成纯字符串。"""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        return "\n".join(b.get("text", "") for b in system if isinstance(b, dict) and b.get("type") == "text")
    return str(system) if system else ""


def _get_prefix_body(req_body):
    """提取缓存前缀：system + 最后一条 user 消息 + tools + model。

    Claude Code 每轮都发完整对话历史，messages 越来越长，
    如果 hash 全部内容，几乎永远不命中。
    用 prefix 模式只看"系统设定 + 当前问题 + 工具定义"，
    相同问题在不同对话轮次里都能命中。
    """
    prefix = {}
    # system prompt，统一提成字符串
    system = req_body.get("system")
    if system:
        prefix["system"] = _extract_system_text(system)
    # tools 定义
    if tools := req_body.get("tools"):
        prefix["tools"] = tools
    # 最后一条 user 消息，统一提成字符串
    msgs = req_body.get("messages", [])
    for msg in reversed(msgs):
        if msg.get("role") == "user":
            prefix["last_user"] = _extract_text_content(msg.get("content", ""))
            break
    # max_tokens 纳入 key，防止截断响应被错误返回给更大的 max_tokens 请求
    max_tokens = req_body.get("max_tokens")
    if max_tokens is not None:
        prefix["max_tokens"] = max_tokens
    # model 和 stop_sequences
    for k in ("model", "stop_sequences"):
        if v := req_body.get(k):
            prefix[k] = v
    return prefix


def _gen_key_static(req_body, model):
    """静态方法版本的 key 生成，供外部调用。"""
    if CACHE_KEY_MODE == "prefix":
        body = _get_prefix_body(req_body)
    else:
        body = {}
        for k, v in req_body.items():
            if k in ("stream", "temperature", "top_p", "stop_sequences"):
                continue
            if k == "max_tokens" and v is not None:
                continue
            if k in ("ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL", "metadata"):
                continue
            body[k] = v
    canonical = json.dumps(body, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(f"{model}:{canonical}".encode()).hexdigest()


@dataclass
class CacheStats:
    hits = 0
    misses = 0
    inserts = 0
    evictions = 0
    saved_ms = 0.0


# 后端抽象层

class _SqliteBackend:
    """SQLite 存储后端。"""

    def __init__(self, db_path, table, col_value, create_sql):
        self._db_path = db_path
        self._table = table
        self._col_value = col_value
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.execute(create_sql)
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_expires ON {table}(expires_at)")
        conn.commit()
        conn.close()

    def _conn(self):
        return sqlite3.connect(self._db_path, check_same_thread=False)

    def get(self, key):
        with self._lock:
            conn = self._conn()
            conn.row_factory = sqlite3.Row
            cur = conn.execute(f"SELECT {self._col_value}, expires_at FROM {self._table} WHERE key = ?", (key,))
            row = cur.fetchone()
            if row is None:
                conn.close()
                return None, 0
            if time.time() > row["expires_at"]:
                conn.execute(f"DELETE FROM {self._table} WHERE key = ?", (key,))
                conn.commit()
                conn.close()
                return None, 0
            conn.execute(f"UPDATE {self._table} SET hit_count = hit_count + 1 WHERE key = ?", (key,))
            conn.commit()
            saved = int((row["expires_at"] - time.time()) * 1000)
            conn.close()
            return row[self._col_value], saved

    def set(self, key, value, ttl):
        now = time.time()
        with self._lock:
            conn = self._conn()
            conn.execute(
                f"INSERT OR REPLACE INTO {self._table} VALUES (?, ?, ?, ?, 0)",
                (key, value, now, now + ttl),
            )
            conn.commit()
            conn.close()

    def delete(self, key):
        with self._lock:
            conn = self._conn()
            conn.execute(f"DELETE FROM {self._table} WHERE key = ?", (key,))
            conn.commit()
            conn.close()

    def clear_expired(self):
        with self._lock:
            conn = self._conn()
            cur = conn.execute(f"DELETE FROM {self._table} WHERE expires_at < ?", (time.time(),))
            conn.commit()
            n = cur.rowcount
            conn.close()
        return n

    def clear_all(self):
        with self._lock:
            conn = self._conn()
            cur = conn.execute(f"DELETE FROM {self._table}")
            conn.commit()
            n = cur.rowcount
            conn.close()
        return n

    def count(self):
        with self._lock:
            conn = self._conn()
            cur = conn.execute(f"SELECT COUNT(*) as t FROM {self._table}")
            r = cur.fetchone()
            conn.close()
        return r["t"] if r else 0


class _RedisBackend:
    """Redis 存储后端，需要 redis 依赖。"""

    def __init__(self, url, prefix, ttl):
        import redis
        self._r = redis.from_url(url, decode_responses=False)
        self._prefix = prefix
        self._default_ttl = ttl
        # 测试连接
        self._r.ping()

    def get(self, key):
        raw = self._r.get(f"{self._prefix}{key}")
        if raw is None:
            return None, 0
        # redis 的 TTL 还剩多少秒
        ttl_left = self._r.ttl(f"{self._prefix}{key}")
        saved = max(ttl_left, 0) * 1000
        return raw, saved

    def set(self, key, value, ttl):
        self._r.setex(f"{self._prefix}{key}", ttl, value)

    def delete(self, key):
        self._r.delete(f"{self._prefix}{key}")

    def clear_expired(self):
        # redis 自动过期，这里扫描并手动删过期的
        n = 0
        for k in self._r.scan_iter(f"{self._prefix}*"):
            if self._r.ttl(k) <= 0:
                self._r.delete(k)
                n += 1
        return n

    def clear_all(self):
        n = 0
        for k in self._r.scan_iter(f"{self._prefix}*"):
            self._r.delete(k)
            n += 1
        return n

    def count(self):
        n = 0
        for _ in self._r.scan_iter(f"{self._prefix}*"):
            n += 1
        return n


def _make_backend(backend_type, db_path, table, col_value, create_sql):
    """根据配置创建存储后端。"""
    if backend_type == "redis":
        try:
            bk = _RedisBackend(REDIS_URL, f"{REDIS_PREFIX}{table}:", CACHE_TTL)
            logger.info("Redis 后端就绪: %s table=%s", REDIS_URL, table)
            return bk
        except Exception as e:
            logger.warning("Redis 连不上 (%s)，回退到 SQLite", e)

    # 默认 SQLite
    bk = _SqliteBackend(db_path, table, col_value, create_sql)
    logger.info("SQLite 后端就绪: %s table=%s", db_path, table)
    return bk


# 缓存类

class ResponseCache:
    """非流式响应缓存。"""

    def __init__(self, enabled=CACHE_ENABLED):
        self._name = "response"
        self.ttl = CACHE_TTL
        self.enabled = enabled
        self._stats = CacheStats()
        if not self.enabled:
            logger.info("缓存关着呢 (CACHE_ENABLED=false)")
            return
        self._bk = _make_backend(
            CACHE_BACKEND, CACHE_DB, "responses", "value",
            """CREATE TABLE IF NOT EXISTS responses (
                key TEXT PRIMARY KEY,
                value BLOB NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                hit_count INTEGER DEFAULT 0
            )""",
        )

    def _gen_key(self, req_body, model):
        return _gen_key_static(req_body, model)

    def get(self, req_body, model):
        if not self.enabled:
            return None, 0
        key = self._gen_key(req_body, model)
        try:
            raw, saved = self._bk.get(key)
        except Exception as e:
            logger.error("缓存读取出错: %s", e)
            self._stats.misses += 1
            return None, 0
        if raw is None:
            self._stats.misses += 1
            return None, 0
        try:
            cached = json.loads(raw) if isinstance(raw, bytes) else json.loads(raw)
            # token 清零，缓存的是旧提供商的数
            if "usage" in cached:
                cached["usage"] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            for ch in cached.get("choices", []):
                if "usage" in ch:
                    ch["usage"] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            self._stats.hits += 1
            self._stats.saved_ms += saved
            return cached, saved
        except (json.JSONDecodeError, TypeError):
            self._bk.delete(key)
            self._stats.misses += 1
            return None, 0

    def set(self, req_body, model, resp):
        if not self.enabled:
            return
        key = self._gen_key(req_body, model)
        val = json.dumps(resp, ensure_ascii=False).encode()
        try:
            self._bk.set(key, val, self.ttl)
            self._stats.inserts += 1
        except Exception as e:
            logger.error("缓存写入出错: %s", e)

    def clear_expired(self):
        if not self.enabled:
            return 0
        n = self._bk.clear_expired()
        if n > 0:
            logger.info("清掉 %d 条过期缓存", n)
        return n

    def clear_all(self):
        if not self.enabled:
            return 0
        n = self._bk.clear_all()
        logger.info("全部清空: %d 条", n)
        return n

    def stats(self):
        if not self.enabled:
            return {"enabled": False}
        total = self._stats.hits + self._stats.misses
        return {
            "enabled": self.enabled,
            "backend": CACHE_BACKEND,
            "ttl": self.ttl,
            "entries": self._bk.count(),
            "hits": self._stats.hits,
            "misses": self._stats.misses,
            "inserts": self._stats.inserts,
            "evictions": self._stats.evictions,
            "saved_ms": self._stats.saved_ms,
            "hit_rate": self._stats.hits / total if total > 0 else 0.0,
        }


class StreamingCache:
    """流式缓存，存完整 SSE 事件序列。"""

    def __init__(self, enabled=CACHE_ENABLED):
        self._name = "streaming"
        self.ttl = CACHE_TTL
        self.enabled = enabled
        self._stats = CacheStats()
        if not self.enabled:
            return
        self._bk = _make_backend(
            CACHE_BACKEND, CACHE_DB, "streaming_responses", "events",
            """CREATE TABLE IF NOT EXISTS streaming_responses (
                key TEXT PRIMARY KEY,
                events BLOB NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                hit_count INTEGER DEFAULT 0
            )""",
        )

    def _gen_key(self, req_body, model):
        return _gen_key_static(req_body, model)

    def get_events(self, req_body, model):
        if not self.enabled:
            return None, 0
        key = self._gen_key(req_body, model)
        try:
            raw, saved = self._bk.get(key)
        except Exception as e:
            logger.error("流式缓存读取出错: %s", e)
            self._stats.misses += 1
            _record("streaming", False, 0, model, key[:16])
            return None, 0
        if raw is None:
            self._stats.misses += 1
            _record("streaming", False, 0, model, key[:16])
            return None, 0
        try:
            events = json.loads(raw) if isinstance(raw, bytes) else json.loads(raw)
            events = _rst_tokens(events)
            self._stats.hits += 1
            self._stats.saved_ms += saved
            _record("streaming", True, saved, model, key[:16])
            return events, saved
        except (json.JSONDecodeError, TypeError):
            self._bk.delete(key)
            self._stats.misses += 1
            _record("streaming", False, 0, model, key[:16])
            return None, 0

    def set_events(self, req_body, model, events):
        if not self.enabled:
            return
        key = self._gen_key(req_body, model)
        val = json.dumps(events, ensure_ascii=False).encode()
        try:
            self._bk.set(key, val, self.ttl)
            self._stats.inserts += 1
        except Exception as e:
            logger.error("流式缓存写入出错: %s", e)

    def clear_expired(self):
        if not self.enabled:
            return 0
        return self._bk.clear_expired()

    def clear_all(self):
        if not self.enabled:
            return 0
        n = self._bk.clear_all()
        logger.info("流式缓存全部清空: %d 条", n)
        return n

    def stats(self):
        if not self.enabled:
            return {"enabled": False}
        total = self._stats.hits + self._stats.misses
        return {
            "enabled": self.enabled,
            "backend": CACHE_BACKEND,
            "ttl": self.ttl,
            "entries": self._bk.count(),
            "hits": self._stats.hits,
            "misses": self._stats.misses,
            "inserts": self._stats.inserts,
            "evictions": self._stats.evictions,
            "saved_ms": self._stats.saved_ms,
            "hit_rate": self._stats.hits / total if total > 0 else 0.0,
        }


# 语义缓存预留接口
# 集成 GPTCache 或自建 embedding + 向量搜索实现模糊匹配
# 适用于 FAQ 类重复问题、不同措辞但相同回答的场景
# 不适合代码生成（语义相似的 prompt 可能需要完全不同的代码）

class SemanticCache:
    """语义缓存桩，预留接口。

    用法（需安装额外依赖）:
      from gptcache import Cache
      from gptcache.embedding import Onnx

      cache = SemanticCache(
          embedding_model=Onnx(),
          similarity_threshold=0.90,
      )

    工作原理:
      1. 把 query 文本向量化
      2. 在向量库里搜最相似的已缓存 query
      3. 相似度超过阈值就返回缓存的响应
      4. 不够相似就正常调 LLM 并把 (query, embedding, response) 存进去
    """

    def __init__(self, embedding_model=None, similarity_threshold=0.90, enabled=False):
        self.enabled = enabled
        self._model = embedding_model
        self._threshold = similarity_threshold
        self._store = {}  # {embedding: (query, response)}
        if enabled and embedding_model is None:
            logger.warning("SemanticCache 开了但没给 embedding_model，用不了")
            self.enabled = False

    def get(self, query):
        """查语义缓存，命中返回响应，没命中返回 None。"""
        if not self.enabled or not self._model:
            return None
        try:
            q_emb = self._model.to_embeddings(query)
            best_sim = 0
            best_resp = None
            for emb, (cached_q, resp) in self._store.items():
                sim = self._similarity(q_emb, emb)
                if sim > best_sim:
                    best_sim = sim
                    best_resp = resp
            if best_sim >= self._threshold:
                return best_resp
        except Exception as e:
            logger.error("语义缓存查询出错: %s", e)
        return None

    def set(self, query, response):
        """存一条语义缓存。"""
        if not self.enabled or not self._model:
            return
        try:
            emb = self._model.to_embeddings(query)
            self._store[tuple(emb)] = (query, response)
        except Exception as e:
            logger.error("语义缓存写入出错: %s", e)

    @staticmethod
    def _similarity(a, b):
        """余弦相似度。"""
        import numpy as np
        a, b = np.asarray(a), np.asarray(b)
        norm_a = np.linalg.norm(a)
        norm_b = np.linalg.norm(b)
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return float(np.dot(a, b) / (norm_a * norm_b))

    def clear(self):
        self._store.clear()


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


# 上游缓存指标提取

def extract_upstream_cache_info(oa_resp):
    """从 OpenAI 响应里提取上游缓存指标。

    OpenAI 在 usage.prompt_tokens_details.cached_tokens 里告诉你
    有多少 input token 命中了它的自动前缀缓存。
    这个指标帮你判断上游缓存是否在帮你省钱。
    """
    usage = oa_resp.get("usage", {})
    details = usage.get("prompt_tokens_details", {})
    cached = details.get("cached_tokens", 0)
    total_input = usage.get("prompt_tokens", 0)
    return {
        "cached_tokens": cached,
        "total_input_tokens": total_input,
        "cache_ratio": cached / total_input if total_input > 0 else 0.0,
    }


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
