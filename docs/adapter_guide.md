# Automatica Adapter Guide

Adapters are the bridge between the sprint runner and AI providers. Each adapter type maps to a different HTTP API protocol.

---

## Adapter Types

### `http_local_chat` (alias: `ollama`)

Local Ollama instance using the `/api/chat` endpoint.

**When to use:** Local development with Ollama models (gemma3, llama3, deepseek-coder, phi4, etc.)

**Config keys:**

| Key | Required | Default | Description |
|-----|----------|---------|-------------|
| `endpoint` | Yes | — | Ollama base URL, e.g. `http://localhost:11434` |
| `model` | Yes | — | Model tag, e.g. `gemma3:4b` |
| `num_predict` | No | 2048 | Max output tokens |
| `temperature` | No | 0.2 | Sampling temperature (0=deterministic) |
| `action_filter` | No | all | List of allowed action names for this model |
| `loop_delay_s` | No | 0.5 | Seconds to sleep between agent iterations |

**Example `settings.json`:**
```json
"ollama": {
  "endpoint": "http://localhost:11434",
  "model": "gemma3:4b",
  "num_predict": 4096,
  "temperature": 0.1,
  "action_filter": ["read_file", "write_file", "edit_file", "list_files", "step_done"]
}
```

---

### `http_chat_completions` (aliases: `openai`, `openai_compatible`, `grok`)

OpenAI-compatible `/v1/chat/completions` endpoint. Works with OpenAI, Groq, Together, local OpenAI-compat servers.

**When to use:** OpenAI API, Groq, or any server that speaks the OpenAI chat completions protocol.

**Config keys:**

| Key | Required | Default | Description |
|-----|----------|---------|-------------|
| `endpoint` | Yes | — | Base URL, e.g. `https://api.openai.com` |
| `api_key` | Yes | — | API key |
| `model` | Yes | — | Model ID, e.g. `gpt-4o` |
| `max_tokens` | No | 4096 | Max output tokens |
| `temperature` | No | 0.2 | Sampling temperature |

**Example `settings.json`:**
```json
"openai": {
  "endpoint": "https://api.openai.com",
  "api_key": "sk-...",
  "model": "gpt-4o",
  "max_tokens": 4096,
  "temperature": 0.2
}
```

---

### `http_generate_content` (alias: `gemini`)

Google Gemini via the `generateContent` REST API.

**When to use:** Google AI Studio / Vertex AI models.

**Config keys:**

| Key | Required | Default | Description |
|-----|----------|---------|-------------|
| `endpoint` | Yes | — | Gemini base URL |
| `api_key` | Yes | — | Google AI API key |
| `model` | Yes | — | Model name, e.g. `gemini-1.5-pro` |
| `max_output_tokens` | No | 4096 | Max output tokens |

**Example `settings.json`:**
```json
"gemini": {
  "endpoint": "https://generativelanguage.googleapis.com",
  "api_key": "AIza...",
  "model": "gemini-1.5-pro",
  "max_output_tokens": 4096
}
```

---

### `subprocess_cli` (alias: `claude`)

Runs a CLI command as a subprocess and reads stdout. Used for Claude via the `claude` CLI.

**When to use:** Anthropic Claude via the `claude` CLI tool (Claude Code).

**Config keys:**

| Key | Required | Default | Description |
|-----|----------|---------|-------------|
| `command` | Yes | — | CLI command array, e.g. `["claude", "--print"]` |
| `model` | No | — | Model flag passed to CLI |
| `timeout` | No | 120 | Subprocess timeout in seconds |

**Example `settings.json`:**
```json
"claude": {
  "command": ["claude", "--print", "--model", "claude-sonnet-4-6"],
  "timeout": 120
}
```

---

## Choosing a Provider

| Scenario | Recommended adapter |
|----------|-------------------|
| Fast local iteration | `ollama` with `gemma3:4b` |
| Best local quality | `ollama` with `gemma3:12b` or `phi4` |
| Production / complex tasks | `claude` or `openai` |
| Google cloud | `gemini` |
| Custom OpenAI-compat server | `http_chat_completions` |

---

## Registering a Provider

```bash
python run_autonomous.py \
  --AddProvider "MyProvider" \
  --ProviderType http_local_chat \
  --ProviderUrl http://localhost:11434 \
  --ProviderModel gemma3:4b
```

List registered providers:
```bash
python run_autonomous.py --ListProviders
```
