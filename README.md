# Claude Code Proxy

让 Claude Code 使用任意第三方模型的代理服务器。

```
Claude Code  ──(Anthropic格式)──▶  Proxy Server  ──(OpenAI格式)──▶  第三方 API
                                       │
                                   从 .env / CLI 读取
                                   真实的 API Key / Model
```

Claude Code 以为自己在和 Anthropic 官方通信，实际上请求被转发到你配置的第三方 API。

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 启动代理 (三种方式任选)

# 方式 A：命令行参数 (最灵活，推荐)
python server.py -k "sk-xxxx" -m "deepseek-chat" -b "https://api.deepseek.com/v1"

# 方式 B：.env 文件
cp .env.example .env
# 编辑 .env，填入 TARGET_API_KEY / TARGET_MODEL / TARGET_API_BASE
python server.py

# 方式 C：混合 (CLI 覆盖 .env)
# .env 中配了 OpenAI，临时想用 DeepSeek：
python server.py -k "sk-deepseek" -m "deepseek-chat" -b "https://api.deepseek.com/v1"
```

## 命令行参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `-m, --model` | 目标模型名 | `.env` 中的 `TARGET_MODEL` |
| `-b, --api-base` | 目标 API 地址 | `.env` 中的 `TARGET_API_BASE` |
| `-k, --api-key` | 目标 API 密钥 | `.env` 中的 `TARGET_API_KEY` |
| `-p, --port` | 代理监听端口 | `8080` |
| `-H, --host` | 代理监听地址 | `0.0.0.0` |

## 使用 Claude Code 连接代理

启动代理后，设置环境变量让 Claude Code 指向代理：

```bash
# Linux / macOS
export ANTHROPIC_BASE_URL=http://localhost:8080
export ANTHROPIC_API_KEY=placeholder  # 任意值，代理默认不校验

claude
```

PowerShell：

```powershell
$env:ANTHROPIC_BASE_URL = "http://localhost:8080"
$env:ANTHROPIC_API_KEY = "placeholder"

claude
```

## 常用示例

```bash
# DeepSeek
python server.py -k "sk-xxx" -m "deepseek-chat" -b "https://api.deepseek.com/v1"

# 通义千问
python server.py -k "sk-xxx" -m "qwen-plus" -b "https://dashscope.aliyuncs.com/compatible-mode/v1"

# Ollama 本地
python server.py -k "ollama" -m "llama3" -b "http://localhost:11434/v1"

# OpenAI GPT-4o
python server.py -k "sk-xxx" -m "gpt-4o" -b "https://api.openai.com/v1"
```

## 支持的后端

| 服务 | `TARGET_API_BASE` | `TARGET_MODEL` |
|------|-------------------|----------------|
| OpenAI | `https://api.openai.com/v1` | `gpt-4o` |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| Ollama (本地) | `http://localhost:11434/v1` | `llama3` |
| vLLM (本地) | `http://localhost:8000/v1` | `your-model` |
| 任意 OpenAI 兼容 API | 对应地址 | 对应模型名 |

## 功能覆盖

- [x] 普通文本对话 (stream / non-stream)
- [x] Tool Use (工具调用)
- [x] System Prompt
- [x] Temperature / Top-P / Max-Tokens
- [x] Stop Sequences
- [x] 流式响应 (SSE)
- [x] 模型列表接口 (`GET /v1/models`)
- [x] 请求/响应日志
- [x] 代理鉴权 (可选)
- [x] 图片输入 (base64 / URL)

## 文件结构

```
.
├── .env.example       # 环境变量模板
├── .env               # 实际配置 (cp .env.example .env 后编辑)
├── converter.py       # Anthropic ↔ OpenAI 格式转换
├── server.py          # FastAPI 代理服务 (含 CLI 参数解析 + 配置)
├── start.bat          # Windows 启动脚本
├── settings-example.json  # Claude Code 设置示例
├── requirements.txt
└── README.md
```

## API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/v1/messages` | 主接口，Claude Code 发送消息到此 |
| GET | `/v1/models` | 模型列表 (兼容性) |
| GET | `/health` | 健康检查 |
| GET | `/` | 服务信息和运行状态 |

## 配置说明

### 环境变量 (`.env`)

| 变量 | 说明 | 必填 |
|------|------|------|
| `TARGET_API_KEY` | 目标 API 密钥 | 是 |
| `TARGET_API_BASE` | 目标 API 基础地址 | 否 (默认 OpenAI) |
| `TARGET_MODEL` | 实际调用使用的模型 | 否 (默认 gpt-4o) |
| `DISGUISE_MODEL` | 伪装模型名 (Claude Code 显示) | 否 |
| `DISGUISE_API_BASE` | 伪装 API 地址 | 否 |
| `TARGET_TIMEOUT` | 超时秒数 | 否 (默认 300) |
| `PROXY_HOST` | 代理监听地址 | 否 (默认 0.0.0.0) |
| `PROXY_PORT` | 代理监听端口 | 否 (默认 8080) |
| `ANTHROPIC_API_KEY` | 代理鉴权密钥 (留空跳过) | 否 |
| `LOG_LEVEL` | 日志级别 | 否 (默认 info) |

## 工作原理

代理服务器扮演"中间人"角色：

1. **接收请求** — Claude Code 以 Anthropic 格式发送请求到 `/v1/messages`
2. **格式转换** — `converter.py` 将 Anthropic 格式转换为 OpenAI 格式
3. **转发请求** — 代理将请求发送到配置的第三方 API
4. **响应转换** — 将 OpenAI 格式的响应转回 Anthropic 格式
5. **返回响应** — Claude Code 收到格式与官方 API 完全一致的响应

### 请求转换示例

Anthropic 格式 (Claude Code → 代理)：

```json
{
  "model": "claude-opus-4",
  "messages": [{"role": "user", "content": "你好"}],
  "max_tokens": 1024
}
```

OpenAI 格式 (代理 → 第三方 API)：

```json
{
  "model": "deepseek-chat",
  "messages": [{"role": "user", "content": "你好"}],
  "max_tokens": 1024
}
```

## 安全建议

- **不要提交 `.env` 文件** — 已配置 `.gitignore`，确保 `TARGET_API_KEY` 不会泄露
- **生产环境启用鉴权** — 设置 `ANTHROPIC_API_KEY`，要求 Claude Code 携带相同的 key
- **使用代理访问控制** — 配合防火墙限制代理端口访问范围