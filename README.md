# Claude Code Proxy

让 Claude Code 使用任意第三方模型的代理服务器。

## 原理

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

```
-m, --model     目标模型名 (覆盖 .env 中的 TARGET_MODEL)
-b, --api-base  目标 API 地址 (覆盖 .env 中的 TARGET_API_BASE)
-k, --api-key   目标 API 密钥 (覆盖 .env 中的 TARGET_API_KEY)
-p, --port      代理监听端口 (默认 8080)
-H, --host      代理监听地址 (默认 0.0.0.0)
```

## 使用 Claude Code 连接代理

启动代理后，设置环境变量让 Claude Code 指向代理：

```bash
export ANTHROPIC_BASE_URL=http://localhost:8080
export ANTHROPIC_API_KEY=placeholder  # 任意值，代理默认不校验

claude
```

或者在 PowerShell 中：

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

| 服务 | TARGET_API_BASE | TARGET_MODEL |
|------|-----------------|--------------|
| OpenAI | `https://api.openai.com/v1` | `gpt-4o` |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| Ollama (本地) | `http://localhost:11434/v1` | `llama3` |
| vLLM (本地) | `http://localhost:8000/v1` | `your-model` |
| 任意 OpenAI 兼容 | 对应地址 | 对应模型名 |

## 功能覆盖

- [x] 普通文本对话 (stream / non-stream)
- [x] Tool Use (工具调用)
- [x] System Prompt
- [x] Temperature / Top-P / Max-Tokens
- [x] Stop Sequences
- [x] 流式响应 (SSE)
- [x] 模型列表接口
- [ ] 图片输入 (简化处理)

## 文件结构

```
├── .env.example    # 环境变量模板
├── .env            # 实际配置 (cp .env.example .env 后编辑)
├── converter.py    # Anthropic ↔ OpenAI 格式转换
├── server.py       # FastAPI 代理服务 (含 CLI 参数解析 + 配置)
└── requirements.txt
```
