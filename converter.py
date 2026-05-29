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
    system_text = ""
    system = data.get("system")
    if system:
        if isinstance(system, list):
            system_text = "\n".join(b.get("text", "") for b in system if b.get("type") == "text")
        else:
            system_text = str(system)
        if system_text:
            messages.append({"role": "system", "content": system_text})

    # --- messages ------------------------------------------------------
    for msg in data.get("messages", []):
        role = msg["role"]
        content = msg.get("content")

        # Skip all system role messages — will be re-inserted at index 0
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
        if role == "assistant":
            # One assistant message: text + tool_calls merged
            assistant_msg: dict[str, Any] = {"role": "assistant"}
            if text_parts:
                assistant_msg["content"] = "\n".join(text_parts)
            if tool_calls_oa:
                assistant_msg["tool_calls"] = tool_calls_oa
            messages.append(assistant_msg)
        else:
            # User: text first, then tool results
            if text_parts:
                messages.append({"role": "user", "content": "\n".join(text_parts)})
            for tr in tool_results_oa:
                messages.append(tr)

    # --- Sanitize: strip ALL system role, re-add at index 0 -------------
    messages = [m for m in messages if m.get("role") != "system"]
    if system_text:
        messages.insert(0, {"role": "system", "content": system_text})

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

    def _emit(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    # ---- message_start ----
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

    # Track open content blocks: None = no block open, "text" = text block open,
    # int ≥ 1 = tool block index open
    open_index: int | None = None
    finished = False
    output_tokens = 0

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

        choices = chunk.get("choices") or []
        if not choices:
            continue
        choice = choices[0]
        delta = choice.get("delta", {})
        finish_reason = choice.get("finish_reason")

        # --- text content ------------------------------------------------
        text_delta = delta.get("content")
        if text_delta:
            if open_index is None:
                open_index = 0
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
        for tc_delta in (delta.get("tool_calls") or []):
            tc_idx = tc_delta.get("index", 0)
            func = tc_delta.get("function") or {}

            if "id" in tc_delta:
                anthropic_idx = tc_idx + 1
                # Close any open block before starting tool block
                if open_index is not None:
                    yield _emit("content_block_stop", {"type": "content_block_stop", "index": open_index})
                open_index = anthropic_idx
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

            args = func.get("arguments", "")
            if args:
                if open_index is None:
                    open_index = tc_idx + 1
                yield _emit("content_block_delta", {
                    "type": "content_block_delta",
                    "index": open_index,
                    "delta": {"type": "input_json_delta", "partial_json": args},
                })

        # --- finish_reason ------------------------------------------------
        if finish_reason and not finished:
            finished = True
            if open_index is not None:
                yield _emit("content_block_stop", {"type": "content_block_stop", "index": open_index})
                open_index = None

            oa_usage = chunk.get("usage") or {}
            output_tokens = oa_usage.get("completion_tokens", 0) or output_tokens

            yield _emit("message_delta", {
                "type": "message_delta",
                "delta": {
                    "stop_reason": _stop_reason_map(finish_reason),
                    "stop_sequence": None,
                },
                "usage": {"output_tokens": output_tokens},
            })

    # --- cleanup -------------------------------------------------------
    if open_index is not None:
        yield _emit("content_block_stop", {"type": "content_block_stop", "index": open_index})

    if not finished:
        yield _emit("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": output_tokens},
        })

    yield _emit("message_stop", {"type": "message_stop"})