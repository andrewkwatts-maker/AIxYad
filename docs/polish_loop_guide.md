# Polish Loop Guide

`scripts/polish_loop.py` is an autonomous improvement daemon that continuously works through `POLISH_TODO.md`, running one task per cycle.

---

## Running It

```bash
# Full daemon — runs every 30 minutes until stopped with Ctrl+C
python scripts/polish_loop.py

# Custom interval
python scripts/polish_loop.py --interval 15

# Single cycle, then exit
python scripts/polish_loop.py --once

# Backup both repos and exit
python scripts/polish_loop.py --backup-only

# List pending TODO items
python scripts/polish_loop.py --list
```

---

## What Each Cycle Does

1. **Backup** — Zips both `Automatica/` and `Automatica-App/` to `../Automatica-Backups/` with a timestamp. Keeps the last 10 backups per repo.

2. **Provider check** — Registers the configured Ollama model in the global DB if not already present.

3. **Health check** — Pings `http://localhost:11434/api/tags`. Skips the sprint if Ollama is not running or the model is not installed.

4. **Pick task** — Reads `POLISH_TODO.md`, picks the highest-priority `[ ]` item (HIGH > MEDIUM > LOW).

5. **Execute** — For `Create file` tasks: calls Ollama `/api/generate` directly and writes the file (fast path, ~15s). For other tasks: adds the item to the project DB and runs a sprint via `run_autonomous.py`.

6. **Mark result** — Updates `POLISH_TODO.md`: `[x]` if at least one task completed, `[!]` if the sprint failed.

7. **Sleep** — Waits `INTERVAL_MIN` minutes, then repeats.

---

## POLISH_TODO.md Format

```
- [ ] TARGET | PRIORITY | DESCRIPTION
```

| Marker | Meaning |
|--------|---------|
| `[ ]` | Pending |
| `[~]` | In progress (set at cycle start, cleared at end) |
| `[x]` | Done |
| `[!]` | Failed |
| `[-]` | Skipped |

**TARGET:** `ENGINE` = `H:/Github/Automatica`, `UI` = `H:/Github/Automatica-App`

**PRIORITY:** `HIGH`, `MEDIUM`, or `LOW` — determines pick order within a cycle.

---

## Configuration (top of `polish_loop.py`)

| Variable | Default | Description |
|----------|---------|-------------|
| `INTERVAL_MIN` | `30` | Minutes between cycles |
| `MAX_BACKUPS` | `10` | Backups to retain per repo |
| `PROVIDER` | `"Ollama"` | Adapter type for sprint runner |
| `PROVIDER_DISPLAY_NAME` | `"Gemma3-4b"` | DB display name for `--AddProvider` |
| `MODEL` | `"gemma3:4b"` | Ollama model tag |
| `OLLAMA_URL` | `"http://localhost:11434"` | Ollama base URL |
| `SPRINT_MINS` | `30` | Max minutes per sprint |

---

## Troubleshooting

**"Ollama not running"**
Start Ollama: `ollama serve`

**"Model not installed"**
Install it: `ollama pull gemma3:4b`

**"Unknown adapter type 'Gemma3-4b'"**
The `PROVIDER` variable must be an adapter type key (`Ollama`, `http_local_chat`, `claude`, etc.), not the display name. The display name is only used for `--AddProvider` registration.

**"JSON parse fail" / sprint produces 0 tasks**
Small local models (7B) often fail to follow the strict JSON action-loop protocol. Options:
- Use `Create file` tasks (direct path, bypasses sprint runner)
- Switch to a larger model (gemma3:12b, phi4, or claude)
- Increase `num_predict` in `settings.json` to avoid truncation

**Item stuck at `[~]`**
The loop was killed mid-sprint. Reset manually:
```bash
# In POLISH_TODO.md, change [~] back to [ ]
```

**Logs**
Live log: `logs/polish_loop_YYYYMMDD.log`
