"""Seed Automatica-App backlog with audit-driven tasks."""
import sqlite3
from datetime import datetime, timezone

APP_TASKS = [
    ("APP-AUDIT-1",
     "Replace silent except-pass blocks with logged warnings across all screens",
     "Audit found 50+ bare except Exception pass blocks across screens/ that swallow errors silently. "
     "Add a log_swallowed_exception(context, exc) helper in services/ and replace every silent catch site "
     "to log via Python logging at WARNING level. Keep behaviour identical. "
     "Files with most offenders: project_detail.py (~30), settings.py (11), management.py (9), "
     "pipeline.py (5), theme_picker.py (5), chat.py (8), standards.py (5)."),

    ("APP-OVERVIEW-1",
     "Overview: fix hardcoded zero-cost fallback and silent stats failures",
     "screens/overview.py:272 catches all exceptions silently when _load_stats fails; user sees ... forever. "
     "db_service.py:1122-1123 returns hardcoded {total_tokens:0, estimated_cost_usd:0.0} when "
     "adb.get_sprint_cost_summary missing. Implement get_sprint_cost_summary in automatica_db, "
     "surface failures via UI snackbar, replace stat placeholder with meaningful empty state."),

    ("APP-AGENTS-1",
     "Agents: wire orphaned export/import/perf methods to UI buttons",
     "screens/agents.py defines _show_export_dialog (line 1336), _show_import_dialog (line 1370), "
     "_show_agent_perf (line 1484) but no UI button calls them. Add Export/Import buttons to the "
     "agent list toolbar and a Performance button in the per-agent view. Verify methods still work."),

    ("APP-PIPELINE-1",
     "Pipeline: sync stages/steps to project DB instead of local JSON only",
     "screens/pipeline.py persists stage edits to ~/.automatica/pipelines.json (lines 96-124, 691, 771, "
     "839, 866, 872, 1020, 1048) but never updates project_settings.pipeline_config in the project DB. "
     "Edits do not reach the sprint runner. Wire all save paths through DBService.update_pipeline_config "
     "(project_id, cfg) so changes affect sprint behaviour."),

    ("APP-PIPELINE-2",
     "Pipeline: implement Triggers tab (currently pure stub)",
     "screens/pipeline.py:1064-1086 the Triggers sub-tab returns a hardcoded UI with zero interactivity "
     "or backend integration. Either remove the tab or implement: list triggers, create/edit/delete "
     "persisted via API, link to triggers.py model."),

    ("APP-TRIGGERS-1",
     "Triggers: replace local JSON persistence with REST API plus project DB",
     "screens/triggers.py persists to ~/.automatica/triggers.json or <project>/automatica/triggers.json "
     "(lines 173, 353, 555, 582, 921, 970). No backend endpoints exist; services/api_client.py has no "
     "trigger methods. Add /api/projects/{pid}/triggers CRUD endpoints in api_server.py, persist in "
     "project DB, add api_client.get_triggers/save_trigger/delete_trigger, switch screens/triggers.py."),

    ("APP-CONFIGS-1",
     "Configs: add API fallback for client mode (currently hard-fails)",
     "screens/configs.py:122-124 displays Database not available in client mode because it only uses "
     "db._global_conn (line 556 even bypasses DBService). Add api_client.get_configs/create_config/"
     "delete_config endpoints, update _load_configs to fall back to API when app.db is None, replace "
     "direct _global_conn.execute calls with DBService methods."),

    ("APP-PROJDETAIL-1",
     "Project detail: implement Logs and VCS sub-tabs (currently _placeholder stubs)",
     "screens/project_detail.py:760 has Logs tab as _placeholder('Logs'), line 765 VCS as "
     "_placeholder('VCS'). Wire Logs sub-tab to filter the global log feed by project_id; wire VCS "
     "sub-tab to show recent commits, branch info, uncommitted-files via gitpython or git CLI."),

    ("APP-LOGS-1",
     "Logs: switch _prepend_log_entry from full reload to true single-row append",
     "screens/logs.py:527 calls _render() (full re-render) on every WebSocket message, defeating the "
     "point of streaming. Implement true row-prepend that adds a single LogRow widget to the top "
     "without rebuilding the whole list."),

    ("APP-THEME-1",
     "Theme picker: log instead of swallow exceptions in callbacks",
     "screens/theme_picker.py callbacks (lines 422, 428, 438, 445, 462) all silently swallow "
     "exceptions. Replace with logged warnings; preview/apply failures should snack-notify the user."),

    ("APP-NET-1",
     "Server/client: add TLS support for non-localhost deployments",
     "Currently api_server.py binds 127.0.0.1 only. For internet usage, add HTTPS support (uvicorn "
     "--ssl-certfile/--ssl-keyfile), CORS allowlist config, and document a deployment recipe "
     "(self-signed for LAN, Lets Encrypt for WAN). Update api_client.py to use https URLs. "
     "New CLI flags: --SslCert, --SslKey, --AllowOrigins."),

    ("APP-NET-2",
     "Server: account system - register/login with bcrypt-hashed passwords",
     "api_server.py currently uses session tokens (_sessions dict) but no user accounts. Add a users "
     "table (id, username, email, password_hash, created_at), POST /api/auth/register (validates + "
     "bcrypts + creates), POST /api/auth/login (returns JWT or session token bound to user_id), "
     "middleware to attach user_id to requests. Bcrypt cost factor 12. Email validation + rate-limit "
     "registration endpoint."),

    ("APP-NET-3",
     "Server: per-user API keys with scoped permissions",
     "Add api_keys table (id, user_id, key_hash, name, scopes JSON, created_at, last_used_at, "
     "revoked_at). Endpoints: POST /api/auth/keys (issue), GET /api/auth/keys (list own), "
     "DELETE /api/auth/keys/{id} (revoke). Authorization header Bearer key alternative to session "
     "token. Scopes: read, write, admin. Default scope: read."),

    ("APP-NET-4",
     "Encrypted client/server replication: end-to-end encrypted task sync",
     "Beyond TLS in transit: add per-project AES-256-GCM payload encryption for sensitive fields "
     "(task descriptions, agent prompts, code diffs). Key derivation per user (PBKDF2 from password, "
     "or libsodium box). Server stores ciphertext and cannot decrypt; client decrypts on retrieval. "
     "Store encrypted_field_keys list in project_settings. Bypass for public/share-link projects."),

    ("APP-NET-5",
     "Client: secure credential storage (OS keystore - keyring library)",
     "services/api_client.py currently reads connection settings from ~/.automatica/connection.json "
     "(plaintext). Move API keys, passwords, encryption secrets to OS keystore: keyring lib "
     "(cross-platform). Migrate existing plaintext on first run. Offer master-password unlock "
     "for portable mode."),

    ("APP-NET-6",
     "Real-time replication: WebSocket sync of task/sprint/log state across clients",
     "Already have /ws/logs (logs.py:474). Extend to /ws/state - broadcast to all subscribers when "
     "any task/sprint/agent row changes. api_server.py: add SSE or WS event emitter that pushes "
     "deltas keyed by (project_id, table, row_id, op). Client subscribes per visible screen and "
     "patches local cache. Reduces polling load and keeps multiple clients in sync."),
]


def main():
    db_path = r"H:\Github\Automatica-App\.automatica\project.db"
    conn = sqlite3.connect(db_path)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(tasks)")]
    have_complexity = "complexity" in cols
    have_priority = "priority" in cols
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    inserted = 0
    for did, title, desc in APP_TASKS:
        if conn.execute("SELECT 1 FROM tasks WHERE display_id=?", (did,)).fetchone():
            continue
        fields = ["display_id", "title", "description", "status", "created_at", "updated_at"]
        values = [did, title, desc, "backlog", now, now]
        if have_complexity:
            fields.append("complexity"); values.append(8)
        if have_priority:
            fields.append("priority"); values.append("HIGH")
        placeholders = ",".join(["?"] * len(values))
        conn.execute(f"INSERT INTO tasks ({','.join(fields)}) VALUES ({placeholders})", values)
        inserted += 1

    conn.commit()
    print(f"Inserted {inserted}/{len(APP_TASKS)} tasks into Automatica-App backlog.")
    for row in conn.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status"):
        print(f"  {row[0]:14s} {row[1]:4}")
    conn.close()


if __name__ == "__main__":
    main()
