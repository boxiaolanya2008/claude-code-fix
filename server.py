"""FastAPI proxy server — exposes an Anthropic-compatible /v1/messages endpoint.

Claude Code talks to this server as if it were api.anthropic.com.
Requests are forwarded (with format conversion) to the upstream target API
configured via .env.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from converter import convert_request, convert_response, convert_stream

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
# Config dataclasses (lightweight — avoid extra import for CLI override)
# ---------------------------------------------------------------------------

from dataclasses import dataclass, field as _field  # noqa: E402

def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


@dataclass(frozen=True)
class TargetConfig:
    api_key: str   = _field(default_factory=lambda: _env("TARGET_API_KEY"))
    api_base: str  = _field(default_factory=lambda: _env("TARGET_API_BASE", "https://api.openai.com/v1"))
    model: str     = _field(default_factory=lambda: _env("TARGET_MODEL", "gpt-4o"))
    max_retries: int = _field(default_factory=lambda: int(_env("TARGET_MAX_RETRIES", "2")))
    timeout: float = _field(default_factory=lambda: float(_env("TARGET_TIMEOUT", "300")))

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
    # Target — start from env, override with CLI
    t = TargetConfig(
        api_key=args.api_key  or _env("TARGET_API_KEY"),
        api_base=args.api_base or _env("TARGET_API_BASE", "https://api.openai.com/v1"),
        model=args.model      or _env("TARGET_MODEL", "gpt-4o"),
    )
    t.validate()

    # Proxy — start from env, override with CLI
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
# State
# ---------------------------------------------------------------------------

target_cfg: TargetConfig
proxy_cfg: ProxyConfig
http_client: httpx.AsyncClient


@asynccontextmanager
async def lifespan(app: FastAPI):
    global target_cfg, proxy_cfg, http_client
    args = _parse_args()
    target_cfg, proxy_cfg = _load_config(args)
    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(target_cfg.timeout, connect=10),
        limits=httpx.Limits(max_connections=64, max_keepalive_connections=16),
    )
    logger.info(
        "Proxy ready  %s:%s  →  %s  model=%s",
        proxy_cfg.host, proxy_cfg.port, target_cfg.api_base, target_cfg.model,
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
    oa_body = convert_request(body, target_cfg.model)
    oa_body.setdefault("stream", stream)

    headers = {
        "Authorization": f"Bearer {target_cfg.api_key}",
        "Content-Type": "application/json",
    }
    # Forward anthropic-version if present (some SDKs check it)
    if v := request.headers.get("anthropic-version"):
        headers["anthropic-version"] = v

    logger.info(
        "[%s] %s → %s  (stream=%s)",
        req_id[:12],
        body.get("model", "?"),
        target_cfg.model,
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

    anthropic_resp = convert_response(oa_resp, target_cfg.model, request_id=req_id)
    return JSONResponse(anthropic_resp)


# ---- streaming -----------------------------------------------------------

async def _handle_stream(req_id: str, oa_body: dict, headers: dict) -> StreamingResponse:
    # Use httpx streaming
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

    async def sse_generator() -> AsyncGenerator[str, None]:
        try:
            async def raw_lines() -> AsyncGenerator[str, None]:
                async for line in upstream_resp.aiter_lines():
                    yield line

            async for event in convert_stream(raw_lines(), target_cfg.model, request_id=req_id):
                yield event
        finally:
            await upstream_resp.aclose()

    return StreamingResponse(
        sse_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# GET /v1/models  (for compatibility)
# ---------------------------------------------------------------------------

@app.get("/v1/models")
async def handle_models():
    return {
        "data": [
            {
                "id": target_cfg.model,
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
    return {"status": "ok", "proxy": "claude-code-proxy", "target": target_cfg.model}


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
