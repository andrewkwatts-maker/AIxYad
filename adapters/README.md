# AIxYad Adapters

Adapters are named by the **wire protocol** they implement, not by any specific provider.
This keeps the engine fully vendor-neutral — you bring your own providers via CLI or App.

---

## Adapter Types

| Adapter type | Protocol | No API key needed? |
|---|---|---|
| `http_chat_completions` | `POST /v1/chat/completions` with Bearer token | No — requires key |
| `http_generate_content` | `POST /:model:generateContent?key=X` | No — requires key |
| `http_local_chat` | `POST /api/chat` (Ollama-style), no auth | Yes — local only |
| `subprocess_cli` | Shells out to a CLI binary | Depends on tool |

---

## Adding Providers

### Via CLI

```
aixyad --ListProviders
aixyad --AddProvider "My Provider" --ProviderType <type> --ProviderUrl <url> --ProviderModel <model> --ProviderKeyEnv <ENV_VAR>
aixyad --RemoveProvider "My Provider"
```

### Via App

Launch the app → Settings → AI Providers → **+ Add**, or use the first-run setup wizard.

---

## 1. How to Add an `http_chat_completions` Provider

Use this for any provider whose API accepts `POST /v1/chat/completions` with a Bearer token.
Many commercial and self-hosted inference services use this format.

**Step 1 — Set your API key as an environment variable:**

```batch
:: Windows CMD
set MY_PROVIDER_API_KEY=your_key_here

:: Windows PowerShell
$env:MY_PROVIDER_API_KEY = "your_key_here"

:: Linux / macOS
export MY_PROVIDER_API_KEY=your_key_here
```

**Step 2 — Register the provider:**

```
aixyad --AddProvider "My Provider" \
    --ProviderType http_chat_completions \
    --ProviderUrl   https://api.example.com \
    --ProviderModel model-name-here \
    --ProviderKeyEnv MY_PROVIDER_API_KEY
```

**Example — a provider that uses this format (xAI Grok):**
```
aixyad --AddProvider "Grok" \
    --ProviderType http_chat_completions \
    --ProviderUrl   https://api.x.ai \
    --ProviderModel grok-3 \
    --ProviderKeyEnv GROK_API_KEY
```

**Example — OpenAI-format API:**
```
aixyad --AddProvider "OpenAI" \
    --ProviderType http_chat_completions \
    --ProviderUrl   https://api.openai.com \
    --ProviderModel gpt-4o \
    --ProviderKeyEnv OPENAI_API_KEY
```

**Example — DeepSeek:**
```
aixyad --AddProvider "DeepSeek" \
    --ProviderType http_chat_completions \
    --ProviderUrl   https://api.deepseek.com \
    --ProviderModel deepseek-chat \
    --ProviderKeyEnv DEEPSEEK_API_KEY
```

> Any service with `/v1/chat/completions` — Mistral, Together.ai, OpenRouter,
> LM Studio, Groq, Perplexity, and many others — works with this adapter.

---

## 2. How to Add an `http_generate_content` Provider

Use this for providers that expose a `generateContent` endpoint with a query-param API key.

**Step 1 — Set your API key:**

```batch
:: Windows CMD
set GENERATE_CONTENT_API_KEY=your_key_here
```

**Step 2 — Register the provider:**

```
aixyad --AddProvider "My GenContent Provider" \
    --ProviderType http_generate_content \
    --ProviderUrl   https://generativelanguage.googleapis.com/v1beta/models \
    --ProviderModel model-name-here \
    --ProviderKeyEnv GENERATE_CONTENT_API_KEY
```

**Example — Google Gemini:**
```
set GEMINI_API_KEY=your_key_here

aixyad --AddProvider "Gemini" \
    --ProviderType http_generate_content \
    --ProviderUrl   https://generativelanguage.googleapis.com/v1beta/models \
    --ProviderModel gemini-2.0-flash \
    --ProviderKeyEnv GEMINI_API_KEY
```

---

## 3. How to Add an `http_local_chat` Provider

Use this for local inference servers running on your machine.
No API key required — just a running server on localhost.

**Step 1 — Start your local server** (e.g. run `ollama serve`).

**Step 2 — Register the provider:**

```
aixyad --AddProvider "Local Model" \
    --ProviderType http_local_chat \
    --ProviderUrl   http://localhost:11434 \
    --ProviderModel model-name-here
```

**Example — Ollama (open-source local runner):**
```
aixyad --AddProvider "Ollama" \
    --ProviderType http_local_chat \
    --ProviderUrl   http://localhost:11434 \
    --ProviderModel llama3.2:3b
```

> Any server that implements the Ollama `/api/chat` protocol works here,
> including LM Studio in Ollama-compat mode and similar tools.

---

## 4. How to Add a `subprocess_cli` Provider

Use this for any AI tool that ships a CLI binary.
The full prompt is passed as the last argument; the binary must print JSON to stdout.

**Step 1 — Ensure the CLI binary is on your PATH.**

**Step 2 — Register the provider:**

```
aixyad --AddProvider "My CLI Tool" \
    --ProviderType subprocess_cli \
    --ProviderUrl   "" \
    --ProviderModel ""
```

Then set the `executable` and optional `args` fields in the provider's config JSON
via the App (Settings → AI Providers → edit).

**Example — Claude CLI (Anthropic's `claude` command-line tool):**
```
aixyad --AddProvider "Claude CLI" \
    --ProviderType subprocess_cli
```
Then in the App, set `executable = claude` and `args = ["--output-format", "json"]`
in the provider's config.

---

## Adding a New Adapter Protocol

1. Create `adapters/<your_adapter>.py` — subclass `BaseAdapter`, implement `send(request) -> dict`.
2. Add one import + one entry in `adapters/__init__.py`'s `_ADAPTER_MAP`.
3. No other file needs to change.

See `adapters/_base.py` for the `BaseAdapter` interface and response schema.
