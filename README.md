# Claude Code Proxy

> English | [中文](./docs/README.zh-CN.md)

A proxy server that enables Claude Code to use any third-party OpenAI-compatible model.

```
Claude Code  ──(Anthropic format)──▶  Proxy Server  ──(OpenAI format)──▶  Third-party API
                                          │
                                      Read from .env / CLI
                                      actual API Key / Model
```

Claude Code thinks it's talking to Anthropic's official API, but requests are transparently forwarded to your configured third-party API.

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Start the proxy (choose one)

# Option A: CLI arguments (most flexible, recommended)
python server.py -k "sk-xxxx" -m "deepseek-chat" -b "https://api.deepseek.com/v1"

# Option B: .env file
cp .env.example .env
# Edit .env, fill in TARGET_API_KEY / TARGET_MODEL / TARGET_API_BASE
python server.py

# Option C: Mix (CLI overrides .env)
# .env has OpenAI configured, but you want to use DeepSeek temporarily:
python server.py -k "sk-deepseek" -m "deepseek-chat" -b "https://api.deepseek.com/v1"
```

## Command Line Arguments

| Argument | Description | Default |
|----------|-------------|---------|
| `-m, --model` | Target model name | `TARGET_MODEL` from `.env` |
| `-b, --api-base` | Target API base URL | `TARGET_API_BASE` from `.env` |
| `-k, --api-key` | Target API key | `TARGET_API_KEY` from `.env` |
| `-p, --port` | Proxy listen port | `8080` |
| `-H, --host` | Proxy listen address | `0.0.0.0` |

## Connecting Claude Code to the Proxy

After starting the proxy, set environment variables to point Claude Code to the proxy:

```bash
# Linux / macOS
export ANTHROPIC_BASE_URL=http://localhost:8080
export ANTHROPIC_API_KEY=placeholder  # Any value, proxy skips validation by default

claude
```

PowerShell:

```powershell
$env:ANTHROPIC_BASE_URL = "http://localhost:8080"
$env:ANTHROPIC_API_KEY = "placeholder"

claude
```

## Common Examples

```bash
# DeepSeek
python server.py -k "sk-xxx" -m "deepseek-chat" -b "https://api.deepseek.com/v1"

# Qwen (Alibaba)
python server.py -k "sk-xxx" -m "qwen-plus" -b "https://dashscope.aliyuncs.com/compatible-mode/v1"

# Ollama (local)
python server.py -k "ollama" -m "llama3" -b "http://localhost:11434/v1"

# OpenAI GPT-4o
python server.py -k "sk-xxx" -m "gpt-4o" -b "https://api.openai.com/v1"
```

## Supported Backends

| Service | `TARGET_API_BASE` | `TARGET_MODEL` |
|---------|-------------------|----------------|
| OpenAI | `https://api.openai.com/v1` | `gpt-4o` |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| Qwen (Alibaba) | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| Ollama (local) | `http://localhost:11434/v1` | `llama3` |
| vLLM (local) | `http://localhost:8000/v1` | `your-model` |
| Any OpenAI-compatible API | corresponding URL | corresponding model |

## Feature Support

- [x] Text chat (stream / non-stream)
- [x] Tool Use (function calling)
- [x] System Prompt
- [x] Temperature / Top-P / Max-Tokens
- [x] Stop Sequences
- [x] Streaming response (SSE)
- [x] Model list endpoint (`GET /v1/models`)
- [x] Request/response logging
- [x] Proxy authentication (optional)
- [x] Image input (base64 / URL)

## Project Structure

```
.
├── .env.example           # Environment variable template
├── .env                   # Actual config (after cp .env.example .env)
├── converter.py           # Anthropic ↔ OpenAI format conversion
├── server.py              # FastAPI proxy server (CLI + config)
├── start.bat              # Windows startup script
├── settings-example.json  # Claude Code settings example
├── requirements.txt
└── README.md
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/v1/messages` | Main endpoint, Claude Code sends messages here |
| GET | `/v1/models` | Model list (for compatibility) |
| GET | `/health` | Health check |
| GET | `/` | Service info and status |

## Configuration

### Environment Variables (`.env`)

| Variable | Description | Required |
|----------|-------------|----------|
| `TARGET_API_KEY` | Target API key | Yes |
| `TARGET_API_BASE` | Target API base URL | No (default: OpenAI) |
| `TARGET_MODEL` | Actual model to call | No (default: gpt-4o) |
| `DISGUISE_MODEL` | Disguised model name (shown to Claude Code) | No |
| `DISGUISE_API_BASE` | Disguised API base (in response) | No |
| `TARGET_TIMEOUT` | Request timeout in seconds | No (default: 300) |
| `PROXY_HOST` | Proxy listen address | No (default: 0.0.0.0) |
| `PROXY_PORT` | Proxy listen port | No (default: 8080) |
| `ANTHROPIC_API_KEY` | Proxy auth key (empty to skip) | No |
| `LOG_LEVEL` | Log level | No (default: info) |

## How It Works

The proxy acts as an intermediary:

1. **Receive** — Claude Code sends requests to `/v1/messages` in Anthropic format
2. **Convert** — `converter.py` transforms Anthropic format to OpenAI format
3. **Forward** — Proxy sends the request to the configured third-party API
4. **Convert Back** — OpenAI-format responses are converted back to Anthropic format
5. **Return** — Claude Code receives responses in the exact official API format

### Request Conversion Example

Anthropic format (Claude Code → Proxy):

```json
{
  "model": "claude-opus-4",
  "messages": [{"role": "user", "content": "Hello"}],
  "max_tokens": 1024
}
```

OpenAI format (Proxy → Third-party API):

```json
{
  "model": "deepseek-chat",
  "messages": [{"role": "user", "content": "Hello"}],
  "max_tokens": 1024
}
```

## Security Notes

- **Never commit `.env`** — `.gitignore` is configured to prevent `TARGET_API_KEY` leakage
- **Enable auth in production** — Set `ANTHROPIC_API_KEY` to require Claude Code to send the matching key
- **Restrict proxy access** — Use firewall rules to limit access to the proxy port

## License

MIT