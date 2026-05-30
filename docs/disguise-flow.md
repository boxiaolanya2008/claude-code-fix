# Disguise Flow Chart

## 1. Startup Phase — Config Resolution

```
┌─────────────────────────────────────────────────────────────────────┐
│                        server.py 启动                               │
│                        lifespan()                                   │
└───────────────────────────┬─────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│  _load_config(args)                                                 │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │  TargetConfig 构建                                             │  │
│  │                                                                │  │
│  │  api_key    ← CLI --api-key  > .env TARGET_API_KEY            │  │
│  │  api_base   ← CLI --api-base > .env TARGET_API_BASE           │  │
│  │  model      ← CLI --model    > .env TARGET_MODEL > "gpt-4o"   │  │
│  │                                                                │  │
│  │  disguise_model    ← _resolve_disguise_model()                │  │
│  │  disguise_api_base ← _resolve_disguise_api_base()             │  │
│  └───────────────────────────────────────────────────────────────┘  │
└───────────────────────────┬─────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│  _resolve_disguise_model()                                          │
│                                                                     │
│   ① 读 .env: DISGUISE_MODEL  ──── 有值? ──YES──▶  返回该值         │
│           │                                                         │
│          NO                                                         │
│           ▼                                                         │
│   ② 读 ~/.claude/settings.json                                     │
│      settings["env"]["ANTHROPIC_MODEL"] ── 有值? ─YES─▶ 返回该值   │
│           │                                                         │
│          NO                                                         │
│           ▼                                                         │
│   ③ 返回 "" (空字符串)                                              │
├─────────────────────────────────────────────────────────────────────┤
│  _resolve_disguise_api_base()                                       │
│                                                                     │
│   ① 读 .env: DISGUISE_API_BASE ── 有值? ──YES──▶  返回该值         │
│           │                                                         │
│          NO                                                         │
│           ▼                                                         │
│   ② 读 ~/.claude/settings.json                                     │
│      settings["env"]["ANTHROPIC_BASE_URL"] ── 有值? ─YES─▶ 返回   │
│           │                                                         │
│          NO                                                         │
│           ▼                                                         │
│   ③ 返回 "https://api.anthropic.com"                               │
└───────────────────────────┬─────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│  _fix_claude_settings()                                             │
│                                                                     │
│  强制写入 ~/.claude/settings.json:                                  │
│    env.ANTHROPIC_MODEL = "Opus 4.8[1m]"                             │
│                                                                     │
│  效果: 确保 Claude Code 客户端认为自己在用 Opus 4.8[1m]             │
└───────────────────────────┬─────────────────────────────────────────┘
                            │
                            ▼
                    Proxy Ready
        model=TARGET_MODEL
        disguise=disguise_model
        api_base=disguise_api_base
```

## 2. Request Phase — Per-Request Disguise

```
┌──────────────────┐
│   Claude Code    │
│                  │
│  发送 Anthropic  │
│  格式请求:       │
│  POST /v1/messages
│  model: "Opus 4.8[1m]"
│  (from settings) │
└────────┬─────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Proxy: POST /v1/messages                                           │
│                                                                     │
│  ① 认证检查: _check_auth(request)                                   │
│  ② 解析 JSON body                                                   │
│  ③ 日志: 原始 model → 目标 model → 伪装 model                       │
└────────┬────────────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────────────┐
│  伪装第一步: 请求格式转换 + model 替换                               │
│  converter.py: convert_request(body, TARGET_MODEL)                  │
│                                                                     │
│  ┌─────────────────────┐      ┌─────────────────────────┐          │
│  │   Anthropic 格式     │ ───▶ │    OpenAI 格式           │          │
│  │                      │      │                         │          │
│  │  model:              │      │  model:                 │          │
│  │    "Opus 4.8[1m]"    │ ───▶ │    TARGET_MODEL          │          │
│  │                      │      │    (e.g. "deepseek-chat")│          │
│  │  system: [...]       │ ───▶ │  messages[0]:            │          │
│  │                      │      │    role: "system"        │          │
│  │  tools[].input_schema│ ───▶ │  tools[].parameters     │          │
│  │                      │      │                         │          │
│  │  content:            │      │  content:               │          │
│  │   tool_use blocks    │ ───▶ │   tool_calls array      │          │
│  │   tool_result blocks │ ───▶ │   role:"tool" messages  │          │
│  │   image blocks       │ ───▶ │   image_url format      │          │
│  └─────────────────────┘      └─────────────────────────┘          │
│                                                                     │
│  关键: model 字段被替换为真实目标模型名                               │
└────────┬────────────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────────────┐
│  发送到第三方 API                                                   │
│  POST {TARGET_API_BASE}/chat/completions                            │
│  Authorization: Bearer {TARGET_API_KEY}                             │
│  Body: OpenAI 格式, model=TARGET_MODEL                              │
│                                                                     │
│  第三方 API 完全不知道 Claude / Anthropic 的存在                     │
└────────┬────────────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────────────┐
│  伪装第二步: 响应格式转换 + model 替换                               │
│                                                                     │
│  ┌─── 非流式 ────────────────────────────────────────────────┐      │
│  │  converter.py: convert_response(oa_resp, DISGUISE_MODEL)  │      │
│  │                                                            │      │
│  │  OpenAI 响应                          Anthropic 响应       │      │
│  │  ─────────────                        ────────────────     │      │
│  │  model: "deepseek-chat" ────────────▶ model: DISGUISE_MODEL│     │
│  │  choices[0].message.content ────────▶ content[0].text      │      │
│  │  choices[0].message.tool_calls ─────▶ content[].tool_use   │      │
│  │  finish_reason: "stop" ─────────────▶ stop_reason: "end_turn"│    │
│  │  finish_reason: "length" ───────────▶ stop_reason: "max_tokens"│  │
│  │  finish_reason: "tool_calls" ───────▶ stop_reason: "tool_use"│    │
│  │  usage.prompt_tokens ───────────────▶ usage.input_tokens   │      │
│  │  usage.completion_tokens ───────────▶ usage.output_tokens  │      │
│  └────────────────────────────────────────────────────────────┘      │
│                                                                     │
│  ┌─── 流式 ──────────────────────────────────────────────────┐      │
│  │  converter.py: convert_stream(oa_stream, DISGUISE_MODEL)  │      │
│  │                                                            │      │
│  │  OpenAI SSE                          Anthropic SSE         │      │
│  │  ──────────                          ─────────────         │      │
│  │  (无) ──────────────────────────────▶ message_start        │      │
│  │                          (model=DISGUISE_MODEL)            │      │
│  │  data: {"choices":[{ ──────────────▶ content_block_start   │      │
│  │    "delta":{"content":"Hi"}}}]}                            │      │
│  │                             ───────▶ content_block_delta   │      │
│  │                             ───────▶ ... (逐 token)        │      │
│  │  data: {"choices":[{ ──────────────▶ content_block_start   │      │
│  │    "delta":{"tool_calls":          │   (type=tool_use)     │      │
│  │      [{...}]}}]}                    │                       │      │
│  │                             ───────▶ input_json_delta      │      │
│  │  data: [DONE] ────────────────────▶ content_block_stop     │      │
│  │                             ───────▶ message_delta         │      │
│  │                                    │  (stop_reason)        │      │
│  │                             ───────▶ message_stop          │      │
│  └────────────────────────────────────────────────────────────┘      │
│                                                                     │
│  关键: model 字段被替换为 DISGUISE_MODEL                             │
│  + HTTP Header 添加 anthropic-version: 2023-06-01                    │
└────────┬────────────────────────────────────────────────────────────┘
         │
         ▼
┌──────────────────┐
│   Claude Code    │
│                  │
│  收到 Anthropic  │
│  格式响应:       │
│  model:          │
│  "Opus 4.8[1m]" │
│                  │
│  以为自己在和    │
│  Anthropic API   │
│  通信 ✓          │
└──────────────────┘
```

## 3. Disguise Value Resolution Summary

```
  ┌──────────────────────────────────────────────────────────────────┐
  │              disguise_model 解析 (优先级从左到右)                  │
  │                                                                  │
  │  .env DISGUISE_MODEL  >  settings.json ANTHROPIC_MODEL  >  ""   │
  │                                                                  │
  │              disguise_api_base 解析 (优先级从左到右)               │
  │                                                                  │
  │  .env DISGUISE_API_BASE  >  settings.json ANTHROPIC_BASE_URL    │
  │                                                     >            │
  │                                 "https://api.anthropic.com"      │
  └──────────────────────────────────────────────────────────────────┘
```

以你的环境为例:

```
  settings.json:
    ANTHROPIC_MODEL    = "Opus 4.8[1m]"
    ANTHROPIC_BASE_URL = "http://0.0.0.0:8080"

  .env:  (DISGUISE_MODEL / DISGUISE_API_BASE 均未设置)

  解析结果:
    disguise_model    = "Opus 4.8[1m]"          (来自 settings.json)
    disguise_api_base = "http://0.0.0.0:8080"   (来自 settings.json)
```

```
  ┌─────────────────────────────────────────────────────────────────┐
  │                  请求生命周期中的变换                             │
  │                                                                 │
  │  Claude Code        Proxy              Third-party API          │
  │  ──────────         ─────              ───────────────          │
  │                                                                 │
  │  model:             model:             model:                   │
  │  "Opus 4.8[1m]" ──▶ "deepseek-chat" ──▶ "deepseek-chat"        │
  │                     (TARGET_MODEL)      (真实调用)               │
  │                                                                 │
  │  "Opus 4.8[1m]" ◄── "Opus 4.8[1m]" ◄── "deepseek-chat"        │
  │                     (DISGUISE_MODEL)    (OpenAI响应)            │
  │  (看到伪装名)       (回写伪装名)                                 │
  └─────────────────────────────────────────────────────────────────┘
```

## 4. Key Disguise Points

| 位置 | 文件:行号 | 伪装动作 |
|------|----------|---------|
| model 解析 | `server.py:105-110` | `_resolve_disguise_model()` 从 settings.json 读取伪装名 |
| api_base 解析 | `server.py:113-121` | `_resolve_disguise_api_base()` 从 settings.json 读取伪装地址 |
| 通用读取 | `server.py:92-102` | `_read_settings_env(key)` 读取 settings.json env 区任意字段 |
| 强制写入 | `server.py:38-66` | `_fix_claude_settings()` 确保 settings.json 中 ANTHROPIC_MODEL 一致 |
| 请求出站 | `converter.py:179` | `body["model"] = target_model` 替换为真实模型 |
| 非流式回站 | `server.py:325` | `convert_response(oa_resp, disguise_model)` 回写伪装名 |
| 流式回站 | `server.py:424` | `convert_stream(replay(), disguise_model)` 回写伪装名 |
| 流式 message_start | `converter.py:286-298` | SSE 首包 model=disguise_model |
| 模型列表 | `server.py:455` | `/v1/models` 返回 disguise_model |
| 根路由 | `server.py:476` | `/` 返回 disguise_model + disguise_api_base |
| HTTP 头 | `server.py:328,441` | 添加 `anthropic-version: 2023-06-01` |
