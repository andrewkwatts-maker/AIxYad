# Complexity-Based Provider Routing

Automatica can automatically route tasks to different AI providers based on their complexity level. This allows you to use fast, local models for simple tasks and reserve more powerful (and potentially expensive) models for complex tasks.

## Default Routing

The default routing configuration is:

| Complexity | Provider | Model | Purpose |
|------------|----------|-------|---------|
| 1-3 | Gemma3-4b | gemma3:4b | Fast local model for simple tasks |
| 4-8 | Gemma3-12b | gemma3:12b | Better local model for moderate complexity |
| 9+ | Phi4 | phi4 | Powerful local model for complex tasks |
| 9+ (fallback) | Claude | claude-sonnet | Cloud model fallback if Phi4 unavailable |

## How It Works

1. When a task is assigned to an agent, Automatica looks up the task's complexity level (Fibonacci scale: 1, 2, 3, 5, 8, 13, 21, 34, 55, 89)

2. The system queries the `complexity_routing` table to find a matching provider:
   - First checks for project-specific routing rules
   - Falls back to global routing rules (project_name = '*')
   - Selects the provider with the lowest priority value

3. If no routing rule matches, falls back to provider performance history

4. If no performance data exists, uses the first enabled provider

## Setting Up Routing

### Automatic Setup

Run the setup script to configure default routing:

```bash
python scripts/setup_routing.py
```

For project-specific routing:

```bash
python scripts/setup_routing.py --project myproject
```

### Manual Setup

Use the Python API:

```python
from automatica_db import init_global_db, setup_complexity_routing_by_name

conn = init_global_db()

# Configure routing
routing_config = [
    {'min_complexity': 1, 'max_complexity': 3, 'provider_name': 'Gemma3-4b'},
    {'min_complexity': 4, 'max_complexity': 8, 'provider_name': 'Gemma3-12b'},
    {'min_complexity': 9, 'max_complexity': 89, 'provider_name': 'Phi4'},
]

result = setup_complexity_routing_by_name(conn, routing_config, project_name='*')
print(result)
```

### Via SQL

Direct SQL access for advanced users:

```sql
-- Add routing rule
INSERT INTO complexity_routing 
  (project_name, min_complexity, max_complexity, provider_id, priority, enabled)
VALUES ('*', 1, 3, 
  (SELECT id FROM providers WHERE name = 'Gemma3-4b'), 
  0, 1);

-- View current routing
SELECT cr.*, p.name as provider_name
FROM complexity_routing cr
JOIN providers p ON cr.provider_id = p.id
ORDER BY cr.min_complexity, cr.priority;

-- Disable a routing rule
UPDATE complexity_routing SET enabled = 0 WHERE id = ?;

-- Delete a routing rule
DELETE FROM complexity_routing WHERE id = ?;
```

## Priority System

When multiple providers can handle a complexity range, the `priority` field determines which is used:

- **Priority 0** = first choice
- **Priority 1** = second choice (fallback)
- Lower priority values are preferred

Example: For complexity 13, if both Phi4 (priority 0) and Claude (priority 1) are configured, Phi4 will be used first.

## Project-Specific Routing

You can override global routing for specific projects:

```python
# Global default: use Gemma3-4b for complexity 1-3
setup_complexity_routing_by_name(conn, [
    {'min_complexity': 1, 'max_complexity': 3, 'provider_name': 'Gemma3-4b'}
], project_name='*')

# For 'critical-project': use Phi4 even for low complexity
setup_complexity_routing_by_name(conn, [
    {'min_complexity': 1, 'max_complexity': 3, 'provider_name': 'Phi4'}
], project_name='critical-project')
```

## Provider Setup

Ensure providers are configured before setting up routing:

```bash
# Check available providers
sqlite3 automatica.db "SELECT id, name, model_name, enabled FROM providers;"

# Add a provider if missing
# (Use the Automatica UI or CLI)
```

## Testing

Run the routing tests:

```bash
python tests/test_complexity_routing.py
```

## Database Schema

The `complexity_routing` table structure:

```sql
CREATE TABLE complexity_routing (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_name    TEXT NOT NULL DEFAULT '*',
    min_complexity  INTEGER NOT NULL DEFAULT 1,
    max_complexity  INTEGER NOT NULL DEFAULT 89,
    provider_id     INTEGER NOT NULL REFERENCES providers(id),
    priority        INTEGER NOT NULL DEFAULT 0,
    enabled         INTEGER NOT NULL DEFAULT 1,
    UNIQUE(project_name, provider_id, min_complexity, max_complexity)
);
```

## Troubleshooting

**Routing not working?**
- Verify providers are enabled: `SELECT * FROM providers WHERE enabled = 1;`
- Check routing rules exist: `SELECT * FROM complexity_routing WHERE enabled = 1;`
- Ensure provider models are available (e.g., `ollama list` for Ollama models)

**Provider not found?**
- Check provider name exactly matches what's in the database
- Provider names are case-sensitive
- Use `SELECT name FROM providers;` to see exact names

**Want to reset routing?**
```sql
DELETE FROM complexity_routing;
```
Then run `python scripts/setup_routing.py` again.
