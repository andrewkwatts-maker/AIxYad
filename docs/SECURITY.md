# Automatica Security Guide

## Responsible Disclosure

If you discover a security vulnerability in Automatica, please:

1. **Do not** open a public GitHub issue.
2. Email the maintainer directly (see GitHub profile for contact).
3. Include: description, reproduction steps, potential impact, and suggested fix.
4. Allow up to 14 days for an initial response and 90 days for a fix before public disclosure.

We will acknowledge valid reports and credit researchers in release notes (unless anonymity is requested).

---

## API Key Management

### Environment Variable Overrides (Highest Priority)

Set these before starting the server or client; they are never written to disk:

| Variable               | Purpose                              |
|------------------------|--------------------------------------|
| `AUTOMATICA_API_KEY`   | API key for server authentication    |
| `AUTOMATICA_SERVER_URL`| Override server URL in client mode   |
| `AUTOMATICA_SMTP_PASSWORD` | Override SMTP password           |

```bash
# Example — Linux/macOS
export AUTOMATICA_API_KEY="my-secret-key"
automatica --Serve

# Example — Windows PowerShell
$env:AUTOMATICA_API_KEY = "my-secret-key"
python -m automatica --Serve
```

### Key Rotation

1. Open the Settings screen in the Automatica App.
2. Go to **Connection → Rotate Key**.
3. Click **Rotate Key** — a new UUID4 key is generated and shown.
4. Copy it with the copy button, then click **Confirm**.
5. The new key is saved (encrypted at rest) and the client reconnects automatically.
6. Update any other clients or scripts that use the old key.

### Encryption at Rest

API keys and SMTP passwords are stored using XOR obfuscation with a machine-derived key (SHA-256 of hostname + UID). Encrypted values are prefixed with `enc:` in settings files. This prevents:
- Casual shoulder-surfing of settings files
- Accidental exposure in logs or backups

**Note:** This is obfuscation, not cryptographic-strength encryption. Do not rely on it as your sole security control. Use environment variables for high-security deployments.

---

## Network Security

### HTTPS Setup

To enable HTTPS, set these keys in `~/.automatica/settings.json` (or via the Security tab):

```json
{
  "ssl_certfile": "/path/to/cert.pem",
  "ssl_keyfile": "/path/to/key.pem"
}
```

Or generate a self-signed certificate for local use:

```bash
openssl req -x509 -newkey rsa:4096 -keyout key.pem -out cert.pem \
    -days 365 -nodes -subj "/CN=localhost"
```

### CORS Configuration

By default, the API server only allows requests from `http://localhost` and `http://127.0.0.1`. To allow additional origins, add to `settings.json`:

```json
{
  "allowed_origins": [
    "http://localhost",
    "http://127.0.0.1",
    "https://your-domain.example.com"
  ]
}
```

### Bearer Token Authentication

The API uses standard HTTP `Authorization: Bearer <token>` headers. Configure:

1. Set `AUTOMATICA_API_KEY` env var or save an API key in Settings.
2. All clients must include: `Authorization: Bearer <your-key>`.
3. WebSocket connections accept the key via `Authorization: Bearer <token>` header.

### Session Tokens

Clients can obtain a session token (1-hour TTL) via:

```
POST /api/auth/login  {"api_key": "<your-key>"}
→ {"token": "...", "expires_in": 3600}
```

Use the returned token as the Bearer token. Logout via `POST /api/auth/logout`.

### Rate Limiting

The server enforces 60 requests per minute per client IP. Exceeding this returns HTTP 429. To adjust, modify `_RATE_LIMIT` and `_RATE_WINDOW` in `api_server.py`.

---

## Database Security

### WAL Mode and Hardening

All SQLite databases use:
- `PRAGMA journal_mode=WAL` — concurrent readers, atomic writes
- `PRAGMA busy_timeout=5000` — 5-second wait instead of SQLITE_BUSY error
- `PRAGMA foreign_keys=ON` — referential integrity enforced
- `PRAGMA synchronous=NORMAL` — good durability with reasonable performance

### Integrity Checks

On first open per process, each database runs `PRAGMA integrity_check`. A warning is issued if the check fails; this does not prevent operation but signals corruption.

### File Permissions

The `~/.automatica/` directory is set to mode `700` (owner-only) on Unix-like systems after creation. On Windows, filesystem ACLs apply instead; `chmod` is silently skipped.

### Audit Log

Security-relevant actions (create/delete agent, create/delete provider) are recorded in the `audit_log` table in the global database:

```sql
SELECT timestamp, action, actor, resource, ip_address FROM audit_log ORDER BY timestamp DESC;
```

---

## Secret Management Best Practices

1. **Use environment variables** for secrets in CI/CD and production.
2. **Never commit** `settings.json`, `connection.json`, or any file containing a real API key.
3. **Rotate keys** regularly using the Settings UI or by setting a new env var.
4. **Review logs** — the `SecretFilter` redacts secrets, but verify no plaintext keys appear in `logs/`.
5. **Restrict network access** — run the API server behind a firewall or VPN for sensitive deployments.
6. **Back up the DB** to a location with appropriate access controls; the DB contains project history but not secrets (keys are stored encrypted, or not at all if env vars are used).

---

## Known Limitations and Mitigations

| Limitation | Mitigation |
|---|---|
| XOR obfuscation is not cryptographic | Use env vars (`AUTOMATICA_API_KEY`) in production |
| Rate limiter is in-memory (resets on restart) | Use a reverse proxy (nginx) for persistent rate limiting |
| Session tokens are in-memory (lost on restart) | Re-authenticate after server restart |
| No RBAC (all authenticated requests have full access) | Planned for Sprint D |
| CORS origins loaded at startup (require restart to update) | Edit `settings.json` and restart the server |
| Self-signed TLS certs trigger browser warnings | Use a CA-signed cert or add to trusted roots |
| Windows: directory permissions via `chmod` silently skipped | Rely on Windows ACLs and NTFS permissions |

---

## Security Audit

Run the included security audit script to check for known vulnerabilities:

```bash
pip install -r requirements-security.txt
python scripts/security_audit.py --output security-report.txt
```

This runs:
- **pip-audit** — checks installed packages against the OSV / PyPA advisory databases
- **bandit** — static analysis for common Python security anti-patterns
