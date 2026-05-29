"""Anthropic ↔ OpenAI format conversion.

Covers:
  - Request:  Anthropic /v1/messages  →  OpenAI /chat/completions
  - Response: OpenAI completion        →  Anthropic message
  - Streaming: OpenAI SSE chunks       →  Anthropic SSE events
"""

from __future__ import annotations

import json
import uuid
from typing import Any, AsyncGenerator

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _gen_id() -> str:
    return f"msg_{uuid.uuid4().hex[:24]}"


def _gen_tool_id() -> str:
    return f"toolu_{uuid.uuid4().hex[:24]}"


def _stop_reason_map(reason: str | None) -> str:
    """Map OpenAI finish_reason → Anthropic stop_reason."""
    return {
        "stop": "end_turn",
        "length": "max_tokens",
        "tool_calls": "tool_use",
        "content_filter": "end_turn",
    }.get(reason or "", "end_turn")


# ---------------------------------------------------------------------------
# Request  (Anthropic → OpenAI)
# ---------------------------------------------------------------------------

def convert_request(data: dict, target_model: str) -> dict:
    """Convert an Anthropic /v1/messages body into an OpenAI chat/completions body."""
    messages: list[dict] = []

    # --- system prompt -------------------------------------------------
    system = data.get("system")
    if system:
        # system can be a string or a list of content blocks
        if isinstance(system, list):
            text = "\n".join(b.get("text", "") for b in system if b.get("type") == "text")
        else:
            text = str(system)
        if text:
            messages.append({"role": "system", "content": text})

    # --- messages ------------------------------------------------------
    for msg in data.get("messages", []):
        role = msg["role"]
        content = msg.get("content")

        # Skip system messages anywhere in the array — OpenAI only allows
        # role:"system" at index 0, which we already handled above.
        if role == "system":
            continue

        # Plain string content — fast path
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue

        # Non-list, non-string content — coerce to string
        if not isinstance(content, list):
            messages.append({"role": role, "content": str(content) if content else ""})
            continue

        # --- Anthropic content-block list --------------------------------
        text_parts: list[str] = []
        tool_calls_oa: list[dict] = []
        tool_results_oa: list[dict] = []

        for block in content:
            btype = block.get("type", "")

            if btype == "text":
                text_parts.append(block.get("text", ""))

            elif btype == "image":
                src = block.get("source", {})
                if src.get("type") == "base64":
                    url = f"data:{src.get('media_type', 'image/jpeg')};base64,{src.get('data', '')}"
                else:
                    url = src.get("url", "")
                text_parts.append(f"[Image: {url}]")

            elif btype == "tool_use":
                tool_calls_oa.append({
                    "id": block.get("id", _gen_tool_id()),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                    },
                })

            elif btype == "tool_result":
                content_val = block.get("content", "")
                if isinstance(content_val, list):
                    content_val = json.dumps(content_val, ensure_ascii=False)
                tool_results_oa.append({
                    "role": "tool",
                    "tool_call_id": block.get("tool_use_id", ""),
                    "content": str(content_val),
                })

        # --- Emit in correct order --------------------------------------
        # For assistant messages: emit ONE message with text + tool_calls
        if role == "assistant":
            assistant_msg: dict[str, Any] = {"role": "assistant"}
            if text_parts:
                assistant_msg["content"] = "\n".join(text_parts)
            # Many OpenAI-compat APIs reject content:null — omit the field entirely
            if tool_calls_oa:
                assistant_msg["tool_calls"] = tool_calls_oa
            messages.append(assistant_msg)

        else:
            # For user messages: emit text first, then tool results as role=tool
            if text_parts:
                messages.append({"role": "user", "content": "\n".join(text_parts)})
            for tr in tool_results_oa:
                messages.append(tr)

    # --- Sanitize: ensure no system role after index 0 ------------------
    if messages and messages[0].get("role") == "system":
        messages = [messages[0]] + [m for m in messages[1:] if m.get("role") != "system"]
    else:
        messages = [m for m in messages if m.get("role") != "system"]

    # --- tools ---------------------------------------------------------
    tools_oa: list[dict] | None = None
    if data.get("tools"):
        tools_oa = []
        for t in data["tools"]:
            tools_oa.append({
                "type": "function",
                "function": {
                    "name": t.get("name", ""),
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", t.get("parameters", {})),
                },
            })

    # --- build final body -----------------------------------------------
    body: dict[str, Any] = {
        "model": target_model,
        "messages": messages,
        "stream": bool(data.get("stream", False)),
    }

    max_tokens = data.get("max_tokens")
    if max_tokens is not None:
        body["max_tokens"] = max_tokens

    temperature = data.get("temperature")
    if temperature is not None:
        body["temperature"] = temperature

    top_p = data.get("top_p")
    if top_p is not None:
        body["top_p"] = top_p

    stop = data.get("stop_sequences")
    if stop:
        body["stop"] = stop

    if tools_oa:
        body["tools"] = tools_oa
        body["tool_choice"] = "auto"

    return body


# ---------------------------------------------------------------------------
# Non-streaming response  (OpenAI → Anthropic)
# ---------------------------------------------------------------------------

def convert_response(oa_resp: dict, model: str, request_id: str | None = None) -> dict:
    """Convert an OpenAI chat/completions response to Anthropic message format."""
    msg_id = request_id or _gen_id()
    choice = (oa_resp.get("choices") or [{}])[0]
    oa_msg = choice.get("message", {})
    finish = choice.get("finish_reason")

    # --- content blocks ------------------------------------------------
    content: list[dict] = []

    text = oa_msg.get("content")
    if text:
        content.append({"type": "text", "text": text})

    for tc in oa_msg.get("tool_calls", []):
        args = tc.get("function", {}).get("arguments", "{}")
        try:
            parsed = json.loads(args)
        except (json.JSONDecodeError, TypeError):
            parsed = {"_raw": args}
        content.append({
            "type": "tool_use",
            "id": tc.get("id", _gen_tool_id()),
            "name": tc.get("function", {}).get("name", ""),
            "input": parsed,
        })

    if not content:
        content.append({"type": "text", "text": ""})

    # --- usage ---------------------------------------------------------
    oa_usage = oa_resp.get("usage", {})

    return {
        "id": msg_id,
        "type": "message",
        "role": "assistant",
        "content": content,
        "model": model,
        "stop_reason": _stop_reason_map(finish),
        "stop_sequence": None,
        "usage": {
            "input_tokens": oa_usage.get("prompt_tokens", 0),
            "output_tokens": oa_usage.get("completion_tokens", 0),
        },
    }


# ---------------------------------------------------------------------------
# Streaming response  (OpenAI SSE → Anthropic SSE)
# ---------------------------------------------------------------------------

async def convert_stream(
    oa_stream: AsyncGenerator[str, None],
    model: str,
    request_id: str | None = None,
) -> AsyncGenerator[str, None]:
    """Yield Anthropic SSE event lines from an OpenAI SSE stream."""
    msg_id = request_id or _gen_id()
    started = False
    block_index = 0
    current_tool_index: int | None = None
    # Track whether we've emitted content_block_start for the current tool
    tool_started: dict[int, bool] = {}
    finished = False
    output_tokens = 0

    def _emit(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    # ---- message_start ----
    started = True
    yield _emit("message_start", {
        "type": "message_start",
        "message": {
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "content": [],
            "model": model,
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    })

    content_block_started = False

    async for raw_line in oa_stream:
        line = raw_line.rstrip("\n")
        if not line.startswith("data: "):
            continue
        payload = line[6:]
        if payload.strip() == "[DONE]":
            break

        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            continue

        choice = (chunk.get("choices") or [{}])[0]
        delta = choice.get("delta", {})
        finish_reason = choice.get("finish_reason")

        # --- text content ------------------------------------------------
        text_delta = delta.get("content")
        if text_delta:
            if not content_block_started:
                content_block_started = True
                yield _emit("content_block_start", {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                })
            yield _emit("content_block_delta", {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text_delta},
            })

        # --- tool calls ---------------------------------------------------
        for tc_delta in delta.get("tool_calls", []):
            tc_idx = tc_delta.get("index", 0)
            anthropic_idx = tc_idx + 1  # index 0 is reserved for text block

            # Start of a new tool call
            if "id" in tc_delta or "function" in tc_delta:
                func = tc_delta.get("function", {})
                if "id" in tc_delta and not tool_started.get(tc_idx):
                    tool_started[tc_idx] = True
                    # Close text block if it was open
                    if content_block_started:
                        yield _emit("content_block_stop", {"type": "content_block_stop", "index": 0})
                        content_block_started = False

                    yield _emit("content_block_start", {
                        "type": "content_block_start",
                        "index": anthropic_idx,
                        "content_block": {
                            "type": "tool_use",
                            "id": tc_delta["id"],
                            "name": func.get("name", ""),
                            "input": {},
                        },
                    })

                # Stream arguments
                args = func.get("arguments", "")
                if args:
                    yield _emit("content_block_delta", {
                        "type": "content_block_delta",
                        "index": anthropic_idx,
                        "delta": {"type": "input_json_delta", "partial_json": args},
                    })

        # --- finish_reason ------------------------------------------------
        if finish_reason and not finished:
            finished = True
            # Close any open content block
            if content_block_started:
                yield _emit("content_block_stop", {"type": "content_block_stop", "index": 0})
                content_block_started = False

            # Close any open tool blocks
            for tidx in sorted(tool_started.keys()):
                if tool_started[tidx]:
                    yield _emit("content_block_stop", {
                        "type": "content_block_stop",
                        "index": tidx + 1,
                    })

            # Usage
            oa_usage = chunk.get("usage", {})
            output_tokens = oa_usage.get("completion_tokens", 0) or output_tokens

            yield _emit("message_delta", {
                "type": "message_delta",
                "delta": {
                    "stop_reason": _stop_reason_map(finish_reason),
                    "stop_sequence": None,
                },
                "usage": {"output_tokens": output_tokens},
            })

    # --- cleanup: close any unclosed text block ---
    if content_block_started:
        yield _emit("content_block_stop", {"type": "content_block_stop", "index": 0})

    if not finished:
        yield _emit("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": output_tokens},
        })

    yield _emit("message_stop", {"type": "message_stop"})
