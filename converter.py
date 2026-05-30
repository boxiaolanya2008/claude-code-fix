"""Anthropic 和 OpenAI 格式互转。

三个核心功能：
  请求转换：Anthropic /v1/messages → OpenAI /chat/completions
  响应转换：OpenAI 完整响应 → Anthropic message
  流式转换：OpenAI SSE chunks → Anthropic SSE events
"""

from __future__ import annotations

import json
import uuid
from typing import Any, AsyncGenerator


def _gen_id():
    return f"msg_{uuid.uuid4().hex[:24]}"


def _gen_tool_id():
    return f"toolu_{uuid.uuid4().hex[:24]}"


def _stop_reason_map(reason):
    """OpenAI finish_reason → Anthropic stop_reason。"""
    mapping = {
        "stop": "end_turn",
        "length": "max_tokens",
        "tool_calls": "tool_use",
        "content_filter": "end_turn",
    }
    if result := mapping.get(reason or ""):
        return result
    return "end_turn"


def _parse_image_source(src):
    """Anthropic 图片 source → OpenAI image_url。"""
    media_type = src.get("media_type", "image/jpeg")
    img_type = src.get("type", "")

    if img_type == "base64":
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{media_type};base64,{src.get('data', '')}"}
        }
    else:
        return {
            "type": "image_url",
            "image_url": {"url": src.get("url", "")}
        }


# 请求转换：Anthropic → OpenAI

def convert_request(data, target_model):
    """把 Anthropic /v1/messages 请求体转成 OpenAI /chat/completions 格式。"""
    messages = []

    # 先处理 system prompt
    system_text = ""
    system = data.get("system")
    if system:
        if isinstance(system, list):
            system_text = "\n".join(b.get("text", "") for b in system if b.get("type") == "text")
        else:
            system_text = str(system)
        if system_text:
            messages.append({"role": "system", "content": system_text})

    # 逐条消息转换
    for msg in data.get("messages", []):
        role = msg["role"]
        content = msg.get("content")

        # system 消息跳过，最后统一插最前面
        if role == "system":
            continue

        # 纯字符串直接用
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue

        # 非字符串非列表，转成字符串
        if not isinstance(content, list):
            messages.append({"role": role, "content": str(content) if content else ""})
            continue

        # Anthropic content block 列表，逐个处理
        content_parts = []
        tool_calls_oa = []
        tool_results_oa = []

        for block in content:
            btype = block.get("type", "")

            if btype == "text":
                text = block.get("text", "")
                if text:
                    content_parts.append({"type": "text", "text": text})

            elif btype == "image":
                image_part = _parse_image_source(block.get("source", {}))
                content_parts.append(image_part)

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

        # 按正确顺序输出
        if role == "assistant":
            # assistant：文本和 tool_calls 合一条消息
            assistant_msg = {"role": "assistant"}
            if content_parts:
                if len(content_parts) == 1 and content_parts[0]["type"] == "text":
                    assistant_msg["content"] = content_parts[0]["text"]
                else:
                    assistant_msg["content"] = content_parts
            if tool_calls_oa:
                assistant_msg["tool_calls"] = tool_calls_oa
            messages.append(assistant_msg)
        else:
            # user：先放内容，再放 tool 结果
            if content_parts:
                if len(content_parts) == 1 and content_parts[0]["type"] == "text":
                    messages.append({"role": "user", "content": content_parts[0]["text"]})
                else:
                    messages.append({"role": "user", "content": content_parts})
            for tr in tool_results_oa:
                messages.append(tr)

    # 清掉所有 system 消息，重新插到最前面
    messages = [m for m in messages if m.get("role") != "system"]
    if system_text:
        messages.insert(0, {"role": "system", "content": system_text})

    # 转换 tools 定义
    tools_oa = None
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

    # 组装最终请求体
    body = {
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


# 响应转换：OpenAI → Anthropic（非流式）

def convert_response(oa_resp, model, request_id=None):
    """把 OpenAI /chat/completions 完整响应转成 Anthropic message 格式。"""
    msg_id = request_id or _gen_id()
    choice = (oa_resp.get("choices") or [{}])[0]
    oa_msg = choice.get("message", {})
    finish = choice.get("finish_reason")

    # 内容块
    content = []

    raw_content = oa_msg.get("content", "")
    if isinstance(raw_content, str):
        if raw_content:
            content.append({"type": "text", "text": raw_content})
    elif isinstance(raw_content, list):
        for part in raw_content:
            if part.get("type") == "text":
                text = part.get("text", "")
                if text:
                    content.append({"type": "text", "text": text})
            elif part.get("type") == "image_url":
                url_data = part.get("image_url", {}).get("url", "")
                content.append({"type": "text", "text": f"[Image response: {url_data[:50]}...]"})

    # tool_calls
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


# 流式转换：OpenAI SSE → Anthropic SSE

async def convert_stream(oa_stream, model, request_id=None):
    """从 OpenAI SSE 流读 chunks，输出 Anthropic 格式的 SSE 事件。"""
    msg_id = request_id or _gen_id()

    def _emit(event, data):
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    # 开始消息
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

    # 当前打开的内容块索引，None=没开，0=text，>=1=tool
    open_index = None
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

        # 文本内容
        raw_content = delta.get("content")
        if raw_content:
            if isinstance(raw_content, str):
                text_parts = [raw_content]
            else:
                text_parts = []
                if isinstance(raw_content, list):
                    for part in raw_content:
                        if part.get("type") == "text":
                            text_parts.append(part.get("text", ""))

            for text_delta in text_parts:
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

        # tool calls
        for tc_delta in (delta.get("tool_calls") or []):
            tc_idx = tc_delta.get("index", 0)
            func = tc_delta.get("function") or {}

            if "id" in tc_delta:
                anthropic_idx = tc_idx + 1
                # 开新 tool 之前先关上一个
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

        # 流结束
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

    # 兜底：流结束但没发 finish_reason
    if open_index is not None:
        yield _emit("content_block_stop", {"type": "content_block_stop", "index": open_index})

    if not finished:
        yield _emit("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": output_tokens},
        })

    yield _emit("message_stop", {"type": "message_stop"})
