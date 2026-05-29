"""FastAPI proxy server — exposes an Anthropic-compatible /v1/messages endpoint.

Claude Code talks to this server as if it were api.anthropic.com.
Requests are forwarded (with format conversion) to the upstream target API
configured via .env.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

# 加载 .env 文件
load_dotenv()

from converter import convert_request, convert_response

# ---------------------------------------------------------------------------
# CLI arguments
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Claude Code → third-party model proxy")
    p.add_argument("-m", "--model",      help="Target model name (default: from .env)")
    p.add_argument("-b", "--api-base",   help="Target API base URL")
    p.add_argument("-k", "--api-key",    help="Target API key")
    p.add_argument("-p", "--port",       type=int, help="Proxy listen port")
    p.add_argument("-H", "--host",       help="Proxy listen host")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Config dataclasses
# ---------------------------------------------------------------------------

from dataclasses import dataclass, field as _field

def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


@dataclass(frozen=True)
class TargetConfig:
    api_key: str   = _field(default_factory=lambda: _env("TARGET_API_KEY"))
    api_base: str  = _field(default_factory=lambda: _env("TARGET_API_BASE", "https://api.openai.com/v1"))
    model: str     = _field(default_factory=lambda: _env("TARGET_MODEL", "gpt-4o"))
    disguise_model: str = _field(default_factory=lambda: _env("DISGUISE_MODEL"))
    disguise_api_base: str = _field(default_factory=lambda: _env("DISGUISE_API_BASE", "https://api.anthropic.com"))
    max_retries: int = _field(default_factory=lambda: int(_env("TARGET_MAX_RETRIES", "2")))
    timeout: float = _field(default_factory=lambda: float(_env("TARGET_TIMEOUT", "300")))
    warm_connections: int = _field(default_factory=lambda: int(_env("WARM_CONNECTIONS", "3")))

    @property
    def chat_url(self) -> str:
        return f"{self.api_base.rstrip('/')}/chat/completions"

    @property
    def models_url(self) -> str:
        return f"{self.api_base.rstrip('/')}/models"

    def validate(self) -> None:
        if not self.api_key:
            raise ValueError("TARGET_API_KEY is required (set via --api-key or .env)")


@dataclass(frozen=True)
class ProxyConfig:
    host: str     = _field(default_factory=lambda: _env("PROXY_HOST", "0.0.0.0"))
    port: int     = _field(default_factory=lambda: int(_env("PROXY_PORT", "8080")))
    auth_key: str = _field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    log_level: str = _field(default_factory=lambda: _env("LOG_LEVEL", "info"))


def _load_config(args: argparse.Namespace) -> tuple[TargetConfig, ProxyConfig]:
    """Build configs from .env, then override with CLI arguments."""
    t = TargetConfig(
        api_key=args.api_key  or _env("TARGET_API_KEY"),
        api_base=args.api_base or _env("TARGET_API_BASE", "https://api.openai.com/v1"),
        model=args.model      or _env("TARGET_MODEL", "gpt-4o"),
    )
    t.validate()

    p = ProxyConfig(
        host=args.host or _env("PROXY_HOST", "0.0.0.0"),
        port=args.port or int(_env("PROXY_PORT", "8080")),
    )
    return t, p


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("proxy")


# ---------------------------------------------------------------------------
# Connection Pool Manager - 预热连接池
# ---------------------------------------------------------------------------

class ConnectionPool:
    """预热连接池：保持多个预建连接，复用减少首字延迟"""

    def __init__(self, config: TargetConfig):
        self.config = config
        self._pool: asyncio.Queue = asyncio.Queue()
        self._lock = asyncio.Lock()
        self._http_client: httpx.AsyncClient | None = None
        self._warming = False

    async def init(self, http_client: httpx.AsyncClient):
        """初始化：创建 http_client 并预热连接"""
        self._http_client = http_client
        await self._warm_pool(self.config.warm_connections)

    async def _warm_pool(self, count: int):
        """预热指定数量的连接"""
        logger.info(f"预热 {count} 个连接...")
        tasks = [self._create_warm_connection() for _ in range(count)]
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info(f"连接预热完成，池中可用连接: {self._pool.qsize()}")

    async def _create_warm_connection(self):
        """创建一个预热连接并放入池中"""
        try:
            # 发送一个极简请求预热连接（不关心响应）
            warm_request = {
                "model": self.config.model,
                "messages": [{"role": "user", "content": "."}],
                "max_tokens": 1,
                "stream": True,
            }
            headers = {
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
            }
            req = self._http_client.build_request(
                "POST", self.config.chat_url, json=warm_request, headers=headers
            )
            resp = await self._http_client.send(req, stream=True)
            # 立即关闭，不等待完整响应（只是预热 TCP+TLS）
            await resp.aclose()
            await self._pool.put(True)  # 占位，表示连接就绪
            logger.debug("连接预热成功")
        except Exception as e:
            logger.debug(f"预热连接失败: {e}")

    async def get_connection(self) -> bool:
        """获取一个预热连接（从池中取）"""
        try:
            item = self._pool.get_nowait()
            return item
        except asyncio.QueueEmpty:
            return False

    async def release_and_refill(self):
        """归还连接后异步补充新连接"""
        if not self._warming:
            self._warming = True
            asyncio.create_task(self._refill())
            self._warming = False

    async def _refill(self):
        """异步补充连接池"""
        try:
            await asyncio.sleep(0.1)  # 短暂延迟，避免过度占用
            await self._create_warm_connection()
        except Exception:
            pass

    async def warm_up(self):
        """主动触发连接池预热（可在响应后调用）"""
        if self._pool.qsize() < self.config.warm_connections:
            asyncio.create_task(self._warm_pool(
                self.config.warm_connections - self._pool.qsize()
            ))


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

target_cfg: TargetConfig
proxy_cfg: ProxyConfig
http_client: httpx.AsyncClient
conn_pool: ConnectionPool


@asynccontextmanager
async def lifespan(app: FastAPI):
    global target_cfg, proxy_cfg, http_client, conn_pool
    args = _parse_args()
    target_cfg, proxy_cfg = _load_config(args)

    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(target_cfg.timeout, connect=10),
        limits=httpx.Limits(max_connections=64, max_keepalive_connections=16),
    )

    # 初始化连接池并预热
    conn_pool = ConnectionPool(target_cfg)
    await conn_pool.init(http_client)

    logger.info(
        "Proxy ready  %s:%s  →  %s  model=%s  disguise=%s  warm_conn=%d",
        proxy_cfg.host, proxy_cfg.port, target_cfg.api_base, target_cfg.model,
        target_cfg.disguise_model, target_cfg.warm_connections,
    )
    yield

    await http_client.aclose()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Claude Code Proxy", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------

def _extract_api_key(request: Request) -> str:
    """Pull the API key from x-api-key or Authorization header."""
    key = request.headers.get("x-api-key", "")
    if not key:
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            key = auth[7:]
    return key


def _check_auth(request: Request) -> str | None:
    """Return error message if auth fails, else None."""
    if not proxy_cfg.auth_key:
        return None
    if _extract_api_key(request) == proxy_cfg.auth_key:
        return None
    return "Invalid API key"


# ---------------------------------------------------------------------------
# POST /v1/messages
# ---------------------------------------------------------------------------

@app.post("/v1/messages")
async def handle_messages(request: Request):
    err = _check_auth(request)
    if err:
        return JSONResponse({"type": "error", "error": {"type": "authentication_error", "message": err}}, 401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"type": "error", "error": {"type": "invalid_request_error", "message": "Invalid JSON"}},
            400,
        )

    req_id = f"msg_{uuid.uuid4().hex[:24]}"
    stream = body.get("stream", False)

    import json as _json
    logger.debug("[%s] RAW BODY: %s", req_id[:12], _json.dumps(body, ensure_ascii=False)[:2000])

    oa_body = convert_request(body, target_cfg.model)

    logger.debug("[%s] OA BODY: %s", req_id[:12], _json.dumps(oa_body, ensure_ascii=False)[:2000])
    oa_body.setdefault("stream", stream)

    headers = {
        "Authorization": f"Bearer {target_cfg.api_key}",
        "Content-Type": "application/json",
    }
    if v := request.headers.get("anthropic-version"):
        headers["anthropic-version"] = v

    logger.info(
        "[%s] %s → %s  disguise=%s  (stream=%s)",
        req_id[:12],
        body.get("model", "?"),
        target_cfg.model,
        target_cfg.disguise_model,
        stream,
    )

    if stream:
        return await _handle_stream(req_id, oa_body, headers)
    else:
        return await _handle_non_stream(req_id, oa_body, headers)


# ---- non-streaming -------------------------------------------------------

async def _handle_non_stream(req_id: str, oa_body: dict, headers: dict) -> Response:
    try:
        resp = await http_client.post(target_cfg.chat_url, json=oa_body, headers=headers)
    except httpx.HTTPError as exc:
        logger.error("[%s] upstream error: %s", req_id[:12], exc)
        return _upstream_error(str(exc))

    if resp.status_code != 200:
        logger.warning("[%s] upstream %d: %s", req_id[:12], resp.status_code, resp.text[:300])
        return JSONResponse(
            {
                "type": "error",
                "error": {
                    "type": "api_error",
                    "message": f"Upstream returned {resp.status_code}: {resp.text[:500]}",
                },
            },
            min(resp.status_code, 500),
        )

    try:
        oa_resp = resp.json()
    except Exception:
        return _upstream_error("Invalid JSON from upstream")

    anthropic_resp = convert_response(oa_resp, target_cfg.disguise_model, request_id=req_id)

    # 响应完成后异步补充连接池
    asyncio.create_task(conn_pool.warm_up())

    return JSONResponse(anthropic_resp, headers={
        "x-request-id": req_id,
        "anthropic-version": "2023-06-01",
    })


# ---- streaming -----------------------------------------------------------

async def _handle_stream(req_id: str, oa_body: dict, headers: dict) -> StreamingResponse:
    try:
        req = http_client.build_request("POST", target_cfg.chat_url, json=oa_body, headers=headers)
        upstream_resp = await http_client.send(req, stream=True)
    except httpx.HTTPError as exc:
        logger.error("[%s] upstream stream error: %s", req_id[:12], exc)
        return _upstream_error(str(exc))

    if upstream_resp.status_code != 200:
        body_text = await upstream_resp.aread()
        await upstream_resp.aclose()
        logger.warning("[%s] upstream %d: %s", req_id[:12], upstream_resp.status_code, body_text[:300])
        return JSONResponse(
            {
                "type": "error",
                "error": {
                    "type": "api_error",
                    "message": f"Upstream returned {upstream_resp.status_code}: {body_text[:500].decode(errors='replace')}",
                },
            },
            min(upstream_resp.status_code, 500),
        )

    # 流结束后补充连接池
    async def sse_generator() -> AsyncGenerator[str, None]:
        try:
            async for raw_bytes in upstream_resp.aiter_bytes():
                text = raw_bytes.decode(errors="replace")
                if text.startswith("data: "):
                    yield text
                elif text.strip() == "data: [DONE]":
                    yield "data: [DONE]\n\n"
        finally:
            await upstream_resp.aclose()
            asyncio.create_task(conn_pool.warm_up())

    return StreamingResponse(
        sse_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "x-request-id": req_id,
            "anthropic-version": "2023-06-01",
        },
    )


# ---------------------------------------------------------------------------
# GET /v1/models
# ---------------------------------------------------------------------------

@app.get("/v1/models")
async def handle_models():
    return {
        "data": [
            {
                "id": target_cfg.disguise_model,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "proxy",
            }
        ],
        "object": "list",
    }


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/")
async def root():
    return {"status": "ok", "proxy": "claude-code-proxy", "model": target_cfg.disguise_model, "api": target_cfg.disguise_api_base}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _upstream_error(msg: str) -> JSONResponse:
    return JSONResponse(
        {
            "type": "error",
            "error": {"type": "api_error", "message": msg},
        },
        502,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    args = _parse_args()
    host = args.host or _env("PROXY_HOST", "0.0.0.0")
    port = args.port or int(_env("PROXY_PORT", "8080"))
    uvicorn.run(app, host=host, port=port, log_level=_env("LOG_LEVEL", "info"))