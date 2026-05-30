"""FastAPI 代理服务器，暴露 Anthropic 兼容的 /v1/messages 接口。

Claude Code 把这个服务器当 api.anthropic.com，请求到这里转成 OpenAI 格式，
转发给第三方 API，响应回来再转回 Anthropic 格式。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
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
from fastapi.responses import JSONResponse, StreamingResponse, HTMLResponse

load_dotenv()

from converter import convert_request, convert_response, convert_stream
from cache import init_caches, get_response_cache, get_streaming_cache, extract_upstream_cache_info

try:
    from analytics import init_analytics, record_event, get_summary, get_trend, get_recent
except ImportError:
    init_analytics = record_event = get_summary = get_trend = get_recent = None


# 启动时修 settings.json，确保 ANTHROPIC_MODEL 是对的

def _fix_claude_settings():
    """读 ~/.claude/settings.json，把 ANTHROPIC_MODEL 设成 Opus 4.8[1m]。

    Claude Code 启动时会读这个值，值不对就报 400。
    """
    settings_path = os.path.expanduser("~/.claude/settings.json")
    if not os.path.exists(settings_path):
        logger.info("settings 文件不在: %s", settings_path)
        return

    try:
        with open(settings_path, "r", encoding="utf-8") as f:
            settings = json.load(f)

        if "env" not in settings:
            settings["env"] = {}

        old = settings["env"].get("ANTHROPIC_MODEL", "")
        settings["env"]["ANTHROPIC_MODEL"] = "Opus 4.8[1m]"

        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2, ensure_ascii=False)

        if old != "Opus 4.8[1m]":
            logger.info("修好了 ANTHROPIC_MODEL: %s → Opus 4.8[1m]", old)
        else:
            logger.info("ANTHROPIC_MODEL 已经是 Opus 4.8[1m]，不用改")
    except Exception as e:
        logger.error("修 settings 失败: %s", e)


# 命令行参数

def _parse_args():
    p = argparse.ArgumentParser(description="Claude Code → 第三方模型代理")
    p.add_argument("-m", "--model",      help="目标模型名 (默认读 .env)")
    p.add_argument("-b", "--api-base",   help="目标 API 地址")
    p.add_argument("-k", "--api-key",    help="目标 API 密钥")
    p.add_argument("-p", "--port",       type=int, help="监听端口")
    p.add_argument("-H", "--host",       help="监听地址")
    return p.parse_args()


# 配置

from dataclasses import dataclass, field as _field  # noqa: E402

def _env(key, default=""):
    return os.getenv(key, default).strip()


def _read_settings_env(key):
    """从 ~/.claude/settings.json 的 env 字段读值。"""
    settings_path = os.path.expanduser("~/.claude/settings.json")
    if not os.path.exists(settings_path):
        return ""
    try:
        with open(settings_path, "r", encoding="utf-8") as f:
            settings = json.load(f)
        return settings.get("env", {}).get(key, "").strip()
    except Exception:
        return ""


def _resolve_disguise_model():
    """伪装模型名：优先 .env 的 DISGUISE_MODEL，没有就读 settings.json。"""
    env_val = _env("DISGUISE_MODEL")
    if env_val:
        return env_val
    return _read_settings_env("ANTHROPIC_MODEL")


def _resolve_disguise_api_base():
    """伪装 API 地址：优先 .env，其次 settings.json，默认 anthropic。"""
    env_val = _env("DISGUISE_API_BASE")
    if env_val:
        return env_val
    settings_val = _read_settings_env("ANTHROPIC_BASE_URL")
    if settings_val:
        return settings_val
    return "https://api.anthropic.com"


@dataclass(frozen=True)
class TargetConfig:
    """目标 API 配置，从 .env 读。"""
    api_key: str   = _field(default_factory=lambda: _env("TARGET_API_KEY"))
    api_base: str  = _field(default_factory=lambda: _env("TARGET_API_BASE", "https://api.openai.com/v1"))
    model: str     = _field(default_factory=lambda: _env("TARGET_MODEL", "gpt-4o"))
    disguise_model: str = _field(default_factory=_resolve_disguise_model)
    disguise_api_base: str = _field(default_factory=_resolve_disguise_api_base)
    max_retries: int = _field(default_factory=lambda: int(_env("TARGET_MAX_RETRIES", "2")))
    timeout: float = _field(default_factory=lambda: float(_env("TARGET_TIMEOUT", "300")))

    @property
    def chat_url(self):
        return f"{self.api_base.rstrip('/')}/chat/completions"

    @property
    def models_url(self):
        return f"{self.api_base.rstrip('/')}/models"

    def validate(self):
        if not self.api_key:
            raise ValueError("TARGET_API_KEY 必须配置")


@dataclass(frozen=True)
class ProxyConfig:
    """代理服务器自己的配置。"""
    host: str     = _field(default_factory=lambda: _env("PROXY_HOST", "0.0.0.0"))
    port: int     = _field(default_factory=lambda: int(_env("PROXY_PORT", "8080")))
    auth_key: str = _field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    log_level: str = _field(default_factory=lambda: _env("LOG_LEVEL", "info"))


def _load_config(args):
    """.env 优先，命令行能覆盖。"""
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


# 日志

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("proxy")


# 全局状态

target_cfg: TargetConfig
proxy_cfg: ProxyConfig
http_client: httpx.AsyncClient

# 流式请求用线程池同步读上游，绕过 async httpx 的各种坑
token_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="token-receiver")

response_cache = None
streaming_cache = None


# 生命周期

@asynccontextmanager
async def lifespan(app: FastAPI):
    global target_cfg, proxy_cfg, http_client, response_cache, streaming_cache
    args = _parse_args()
    target_cfg, proxy_cfg = _load_config(args)
    _fix_claude_settings()
    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(target_cfg.timeout, connect=10),
        limits=httpx.Limits(max_connections=64, max_keepalive_connections=16),
        verify=False,
    )
    response_cache, streaming_cache = init_caches()
    if init_analytics:
        init_analytics()
    cache_st = "开了" if response_cache and response_cache.enabled else "没开"
    logger.info(
        "代理就绪  %s:%s  →  %s  model=%s  disguise=%s  cache=%s",
        proxy_cfg.host, proxy_cfg.port, target_cfg.api_base,
        target_cfg.model, target_cfg.disguise_model, cache_st,
    )
    yield
    await http_client.aclose()
    token_executor.shutdown(wait=True)
    logger.info("线程池已关闭")


# FastAPI

app = FastAPI(title="Claude Code Proxy", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# 鉴权

def _extract_api_key(request):
    """从请求头拿 key，支持 x-api-key 和 Authorization Bearer。"""
    key = request.headers.get("x-api-key", "")
    if not key:
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            key = auth[7:]
    return key


def _check_auth(request):
    """校验 API key，没配 auth_key 就直接放行。"""
    if not proxy_cfg.auth_key:
        return None
    if _extract_api_key(request) == proxy_cfg.auth_key:
        return None
    return "Invalid API key"


# POST /v1/messages 核心入口

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

    logger.debug("[%s] 原始请求: %s", req_id[:12], json.dumps(body, ensure_ascii=False)[:2000])

    # Anthropic → OpenAI 格式
    oa_body = convert_request(body, target_cfg.model)

    # 塞伪装字段，有些 SDK 会检查这俩
    oa_body["ANTHROPIC_BASE_URL"] = "https://api.anthropic.com"
    oa_body["ANTHROPIC_MODEL"] = target_cfg.disguise_model

    logger.debug("[%s] 转换后: %s", req_id[:12], json.dumps(oa_body, ensure_ascii=False)[:2000])
    oa_body.setdefault("stream", stream)

    headers = {
        "Authorization": f"Bearer {target_cfg.api_key}",
        "Content-Type": "application/json",
    }
    if v := request.headers.get("anthropic-version"):
        headers["anthropic-version"] = v

    logger.info(
        "[%s] %s → %s  disguise=%s  (stream=%s)",
        req_id[:12], body.get("model", "?"),
        target_cfg.model, target_cfg.disguise_model, stream,
    )

    # 非流式才走缓存
    cache = get_response_cache()
    if cache and not stream:
        cached_resp, time_saved = cache.get(body, target_cfg.model)
        if cached_resp:
            logger.info("[%s] 缓存命中，省了 %dms", req_id[:12], time_saved)
            if record_event:
                record_event("response", True, time_saved, target_cfg.model, cache._gen_key(body, target_cfg.model))
            anthropic_resp = convert_response(cached_resp, target_cfg.disguise_model, request_id=req_id)
            return JSONResponse(anthropic_resp, headers={
                "x-request-id": req_id,
                "anthropic-version": "2023-06-01",
                "x-cache-hit": "true",
                "x-response-time-saved-ms": str(time_saved),
                "x-tokens-reset": "true",
            })

    # 走到这里说明缓存没命中或缓存关了，记录 miss
    if record_event:
        record_event("response", False, 0, target_cfg.model, "")

    if stream:
        return await _handle_stream(req_id, oa_body, headers, body)
    else:
        return await _handle_non_stream(req_id, oa_body, headers, body)


# 非流式处理

async def _handle_non_stream(req_id, oa_body, headers, original_body):
    try:
        resp = await http_client.post(target_cfg.chat_url, json=oa_body, headers=headers)
    except httpx.HTTPError as exc:
        logger.error("[%s] 上游出错: %s", req_id[:12], exc)
        return _upstream_error(str(exc))

    if resp.status_code != 200:
        logger.warning("[%s] 上游返回 %d: %s", req_id[:12], resp.status_code, resp.text[:300])
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
        return _upstream_error("上游返回的不是 JSON")

    # 追踪上游缓存指标
    upstream_info = extract_upstream_cache_info(oa_resp)
    if upstream_info["cached_tokens"] > 0:
        logger.info(
            "[%s] 上游缓存命中: %d/%d tokens (%.0f%%)",
            req_id[:12], upstream_info["cached_tokens"],
            upstream_info["total_input_tokens"],
            upstream_info["cache_ratio"] * 100,
        )

    # 存缓存
    cache = get_response_cache()
    if cache:
        cache.set(original_body, target_cfg.model, oa_resp)

    anthropic_resp = convert_response(oa_resp, target_cfg.disguise_model, request_id=req_id)
    return JSONResponse(anthropic_resp, headers={
        "x-request-id": req_id,
        "anthropic-version": "2023-06-01",
    })


# 流式处理

async def _handle_stream(req_id, oa_body, headers, original_body):
    # 流式缓存
    cache = get_streaming_cache()
    if cache:
        cached_events, time_saved = cache.get_events(original_body, target_cfg.model)
        if cached_events:
            logger.info("[%s] 流式缓存命中，省了 %dms", req_id[:12], time_saved)
            if record_event:
                record_event("streaming", True, time_saved, target_cfg.model, cache._gen_key(original_body, target_cfg.model))

            async def cached_sse_generator():
                # 模拟真实生成节奏，避免客户端因秒回而行为异常
                import asyncio as _aio
                for ev in cached_events:
                    yield ev
                    # content_block_delta 之间加微延迟，模拟打字效果
                    if "content_block_delta" in ev:
                        await _aio.sleep(0.02)
                    elif "content_block_start" in ev or "content_block_stop" in ev:
                        await _aio.sleep(0.01)

            return StreamingResponse(
                cached_sse_generator(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                    "x-request-id": req_id,
                    "anthropic-version": "2023-06-01",
                    "x-cache-hit": "true",
                    "x-response-time-saved-ms": str(time_saved),
                    "x-tokens-reset": "true",
                },
            )

    # 流式缓存没命中，记录 miss
    if record_event:
        record_event("streaming", False, 0, target_cfg.model, "")

    # 线程池同步读上游，通过 Queue 传给 async
    queue: asyncio.Queue[str | None] = asyncio.Queue()
    loop = asyncio.get_event_loop()
    collected_events: list[str] = []

    def read_upstream():
        """同步读上游 SSE 流，读完线程自动退出。"""
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
            logger.error("[%s] 线程读取出错: %s", req_id[:12], e)
            loop.call_soon_threadsafe(queue.put_nowait, f"__EXCEPTION__{e}")
        finally:
            sync_client.close()
            loop.call_soon_threadsafe(queue.put_nowait, None)

    future = token_executor.submit(read_upstream)

    async def sse_generator():
        def _sse(event, data):
            return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

        def _is_error(line):
            """检测线程传过来的错误标记。"""
            if line.startswith("__ERROR__"):
                parts = line.split("__", 2)
                status_code = int(parts[1])
                body_text = parts[2] if len(parts) > 2 else ""
                return f"Upstream {status_code}: {body_text[:300]}"
            if line.startswith("__EXCEPTION__"):
                return line[len("__EXCEPTION__"):]
            return None

        try:
            async def raw_upstream_lines():
                while True:
                    line = await queue.get()
                    if line is None:
                        break
                    yield line

            # 先拿第一行，判断是正常还是出错
            first_line = None
            async for line in raw_upstream_lines():
                first_line = line
                break

            if first_line is None:
                yield _sse("error", {
                    "type": "error",
                    "error": {"type": "api_error", "message": "上游返回空响应"},
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

            # 正常流，把第一行塞回去继续转
            async def replay():
                yield first_line
                async for line in raw_upstream_lines():
                    yield line

            async for event in convert_stream(replay(), target_cfg.disguise_model, request_id=req_id):
                collected_events.append(event)
                yield event
        finally:
            try:
                future.result(timeout=5)
            except Exception:
                pass
            # 流结束存缓存
            if collected_events:
                cache = get_streaming_cache()
                if cache:
                    cache.set_events(original_body, target_cfg.model, collected_events)
                # 从最后一个 chunk 提取上游缓存指标
                for ev in reversed(collected_events):
                    if "message_delta" in ev and "usage" in ev:
                        try:
                            start = ev.find("data: ") + 6
                            end = ev.find("\n\n", start)
                            if end == -1:
                                end = len(ev)
                            delta_obj = json.loads(ev[start:end])
                            upstream_usage = delta_obj.get("usage", {})
                            cached = upstream_usage.get("cached_tokens", 0)
                            if cached > 0:
                                logger.info("[%s] 上游流式缓存命中: %d tokens", req_id[:12], cached)
                        except Exception:
                            pass
                        break

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


# GET /v1/models 兼容

@app.get("/v1/models")
async def handle_models():
    return {
        "data": [{
            "id": target_cfg.disguise_model,
            "object": "model",
            "created": int(time.time()),
            "owned_by": "proxy",
        }],
        "object": "list",
    }


# 健康检查和缓存管理

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/cache/stats")
async def cache_stats():
    rc = get_response_cache()
    sc = get_streaming_cache()
    return {
        "response_cache": rc.stats() if rc else {"enabled": False},
        "streaming_cache": sc.stats() if sc else {"enabled": False},
    }


@app.post("/cache/clear")
async def cache_clear():
    """全清。"""
    rc = get_response_cache()
    sc = get_streaming_cache()
    res = {}
    if rc:
        res["response_cache_cleared"] = rc.clear_all()
    if sc:
        res["streaming_cache_cleared"] = sc.clear_all()
    return {"status": "ok", "cleared": res}


@app.post("/cache/clear-expired")
async def cache_clear_expired():
    """只清过期的。"""
    rc = get_response_cache()
    sc = get_streaming_cache()
    res = {}
    if rc:
        res["response_cache_evicted"] = rc.clear_expired()
    if sc:
        res["streaming_cache_evicted"] = sc.clear_expired()
    return {"status": "ok", "evicted": res}


# 仪表盘

@app.get("/dashboard")
async def dashboard():
    """缓存监控仪表盘。"""
    if not get_summary:
        return HTMLResponse("<h1>analytics 模块未安装</h1>", status_code=503)
    html_path = os.path.join(os.path.dirname(__file__), "static", "dashboard.html")
    if not os.path.exists(html_path):
        return HTMLResponse("<h1>仪表盘文件不存在</h1>", status_code=404)
    with open(html_path, encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/dashboard/summary")
async def dashboard_summary():
    if not get_summary:
        return {"error": "analytics not available"}
    return get_summary()


@app.get("/dashboard/trend")
async def dashboard_trend(seconds: int = 3600, bucket: int = 60):
    if not get_trend:
        return []
    return get_trend(seconds=seconds, bucket=bucket)


@app.get("/dashboard/events")
async def dashboard_events(limit: int = 50):
    if not get_recent:
        return []
    return get_recent(limit=limit)


@app.get("/")
async def root():
    return {
        "status": "ok",
        "proxy": "claude-code-proxy",
        "model": target_cfg.disguise_model,
        "api": target_cfg.disguise_api_base,
    }


# 工具函数

def _upstream_error(msg):
    return JSONResponse(
        {"type": "error", "error": {"type": "api_error", "message": msg}},
        502,
    )


# 启动入口

if __name__ == "__main__":
    args = _parse_args()
    host = args.host or _env("PROXY_HOST", "0.0.0.0")
    port = args.port or int(_env("PROXY_PORT", "8080"))
    uvicorn.run(app, host=host, port=port, log_level=_env("LOG_LEVEL", "info"))
