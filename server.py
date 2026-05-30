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
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
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

from converter import convert_request, convert_response, convert_stream

# ---------------------------------------------------------------------------
# Auto-fix ~/.claude/settings.json
# ---------------------------------------------------------------------------

def _fix_claude_settings() -> None:
    """Read ~/.claude/settings.json and set ANTHROPIC_MODEL to Opus 4.8[1m]"""
    settings_path = os.path.expanduser("~/.claude/settings.json")
    if not os.path.exists(settings_path):
        logger.info("Settings file not found: %s", settings_path)
        return

    try:
        with open(settings_path, "r", encoding="utf-8") as f:
            settings = json.load(f)

        # Ensure env section exists
        if "env" not in settings:
            settings["env"] = {}

        # Set ANTHROPIC_MODEL
        old_model = settings["env"].get("ANTHROPIC_MODEL", "")
        settings["env"]["ANTHROPIC_MODEL"] = "Opus 4.8[1m]"

        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2, ensure_ascii=False)

        if old_model != "Opus 4.8[1m]":
            logger.info("Fixed ANTHROPIC_MODEL: %s → Opus 4.8[1m]", old_model)
        else:
            logger.info("ANTHROPIC_MODEL already set to Opus 4.8[1m]")

    except Exception as e:
        logger.error("Failed to fix settings: %s", e)

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


def _read_settings_env(key: str) -> str:
    """Read a key from the env section of ~/.claude/settings.json."""
    settings_path = os.path.expanduser("~/.claude/settings.json")
    if not os.path.exists(settings_path):
        return ""
    try:
        with open(settings_path, "r", encoding="utf-8") as f:
            settings = json.load(f)
        return settings.get("env", {}).get(key, "").strip()
    except Exception:
        return ""


def _resolve_disguise_model() -> str:
    """Resolve disguise model: DISGUISE_MODEL env > settings.json ANTHROPIC_MODEL > empty."""
    env_val = _env("DISGUISE_MODEL")
    if env_val:
        return env_val
    return _read_settings_env("ANTHROPIC_MODEL")


def _resolve_disguise_api_base() -> str:
    """Resolve disguise API base: DISGUISE_API_BASE env > settings.json ANTHROPIC_BASE_URL > default."""
    env_val = _env("DISGUISE_API_BASE")
    if env_val:
        return env_val
    settings_val = _read_settings_env("ANTHROPIC_BASE_URL")
    if settings_val:
        return settings_val
    return "https://api.anthropic.com"


@dataclass(frozen=True)
class TargetConfig:
    api_key: str   = _field(default_factory=lambda: _env("TARGET_API_KEY"))
    api_base: str  = _field(default_factory=lambda: _env("TARGET_API_BASE", "https://api.openai.com/v1"))
    model: str     = _field(default_factory=lambda: _env("TARGET_MODEL", "gpt-4o"))
    disguise_model: str = _field(default_factory=_resolve_disguise_model)
    disguise_api_base: str = _field(default_factory=_resolve_disguise_api_base)
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

# 多线程接收 token 的线程池
token_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="token-receiver")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global target_cfg, proxy_cfg, http_client
    args = _parse_args()
    target_cfg, proxy_cfg = _load_config(args)
    _fix_claude_settings()
    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(target_cfg.timeout, connect=10),
        limits=httpx.Limits(max_connections=64, max_keepalive_connections=16),
        verify=False,
    )
    logger.info(
        "Proxy ready  %s:%s  →  %s  model=%s  disguise=%s",
        proxy_cfg.host, proxy_cfg.port, target_cfg.api_base, target_cfg.model, target_cfg.disguise_model,
    )
    yield
    await http_client.aclose()
    # 关闭线程池，等待所有线程完成
    token_executor.shutdown(wait=True)
    logger.info("Token receiver thread pool shut down")


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

    # Debug: log original request
    import json as _json
    logger.debug("[%s] RAW BODY: %s", req_id[:12], _json.dumps(body, ensure_ascii=False)[:2000])

    oa_body = convert_request(body, target_cfg.model)

    # Attach disguise values using Anthropic official keys
    oa_body["ANTHROPIC_BASE_URL"] = "https://api.anthropic.com"
    oa_body["ANTHROPIC_MODEL"] = target_cfg.disguise_model

    # Debug: log converted request
    logger.debug("[%s] OA BODY: %s", req_id[:12], _json.dumps(oa_body, ensure_ascii=False)[:2000])
    oa_body.setdefault("stream", stream)

    headers = {
        "Authorization": f"Bearer {target_cfg.api_key}",
        "Content-Type": "application/json",
    }
    # Forward anthropic-version if present (some SDKs check it)
    if v := request.headers.get("anthropic-version"):
        headers["anthropic-version"] = v

        verify=False,
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
    return JSONResponse(anthropic_resp, headers={
        "x-request-id": req_id,
        "anthropic-version": "2023-06-01",
    })


# ---- streaming -----------------------------------------------------------

async def _handle_stream(req_id: str, oa_body: dict, headers: dict) -> StreamingResponse:
    # 在线程中使用同步 httpx.Client 发送流式请求
    queue: asyncio.Queue[str | None] = asyncio.Queue()
    loop = asyncio.get_event_loop()

    def read_upstream():
        """在线程中用同步 Client 读取上游数据，完成后自动废弃线程"""
        sync_client = httpx.Client(
            timeout=httpx.Timeout(target_cfg.timeout, connect=10),
            verify=False,
        )
        try:
            with sync_client.stream(
                "POST", target_cfg.chat_url, json=oa_body, headers=headers,
            ) as resp:
                if resp.status_code != 200:
                    body_text = resp.read().decode(errors="replace")[:500]
                    loop.call_soon_threadsafe(
                        queue.put_nowait,
                        f"__ERROR__{resp.status_code}__{body_text}",
                    )
                    return
                for line in resp.iter_lines():
                    if line:
                        loop.call_soon_threadsafe(queue.put_nowait, line)
        except Exception as e:
            logger.error("[%s] thread read error: %s", req_id[:12], e)
            loop.call_soon_threadsafe(
                queue.put_nowait, f"__EXCEPTION__{e}",
            )
        finally:
            sync_client.close()
            loop.call_soon_threadsafe(queue.put_nowait, None)
            logger.debug("[%s] token receiver thread exiting", req_id[:12])

    future = token_executor.submit(read_upstream)

    async def sse_generator() -> AsyncGenerator[str, None]:
        def _sse(event: str, data: dict) -> str:
            return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

        def _is_error(line: str) -> str | None:
            """检测线程错误标记，返回错误消息或 None"""
            if line.startswith("__ERROR__"):
                parts = line.split("__", 2)
                status_code = int(parts[1])
                body_text = parts[2] if len(parts) > 2 else ""
                return f"Upstream {status_code}: {body_text[:300]}"
            if line.startswith("__EXCEPTION__"):
                return line[len("__EXCEPTION__"):]
            return None

        try:
            async def raw_upstream_lines() -> AsyncGenerator[str, None]:
                while True:
                    line = await queue.get()
                    if line is None:
                        break
                    yield line

            first_line = None
            async for line in raw_upstream_lines():
                first_line = line
                break

            if first_line is None:
                yield _sse("error", {
                    "type": "error",
                    "error": {"type": "api_error", "message": "Upstream returned empty response"},
                })
                return

            error_msg = _is_error(first_line)
            if error_msg:
                if first_line.startswith("__ERROR__"):
                    logger.warning("[%s] %s", req_id[:12], error_msg)
                else:
                    logger.error("[%s] %s", req_id[:12], error_msg)
                yield _sse("error", {
                    "type": "error",
                    "error": {"type": "api_error", "message": error_msg},
                })
                return

            # 正常流 — 回放 first_line 后继续
            async def replay() -> AsyncGenerator[str, None]:
                yield first_line
                async for line in raw_upstream_lines():
                    yield line

            async for event in convert_stream(replay(), target_cfg.disguise_model, request_id=req_id):
                yield event
        finally:
            try:
                future.result(timeout=5)
            except Exception:
                pass
            logger.debug("[%s] token receiver thread completed and discarded", req_id[:12])

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
# GET /v1/models  (for compatibility)
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
