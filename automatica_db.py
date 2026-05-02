#!/usr/bin/env python3
"""Automatica Database — Dual SQLite database management.

Manages two databases:
  Global DB  (automatica.db, next to EXE)  — providers, agents, performance, usage
  Project DB ({project}/.automatica/project.db) — tasks, sprints, subtasks, history
"""

import sqlite3
import json
import re
import sys
import zlib
from pathlib import Path
from typing import Optional, Dict, List, Any, Tuple
from datetime import datetime, timezone

SCHEMA_VERSION = 6

# ── Global DB Schema ────────────────────────────────────────────────

GLOBAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS _schema_version (
    version   INTEGER NOT NULL,
    applied   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS providers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,
    provider_type TEXT NOT NULL DEFAULT 'api',
    api_key_env   TEXT DEFAULT '',
    base_url      TEXT DEFAULT '',
    model_name    TEXT DEFAULT '',
    enabled       INTEGER NOT NULL DEFAULT 1,
    max_tokens    INTEGER DEFAULT 4096,
    config_json   TEXT DEFAULT '{}',
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS agent_definitions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    slug          TEXT NOT NULL UNIQUE,
    display_name  TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT '',
    description   TEXT DEFAULT '',
    system_prompt TEXT DEFAULT '',
    provider_id   INTEGER DEFAULT NULL REFERENCES providers(id),
    max_complexity INTEGER DEFAULT 13,
    min_complexity INTEGER DEFAULT 1,
    permissions   TEXT DEFAULT '{}',
    enabled       INTEGER NOT NULL DEFAULT 1,
    action_filter TEXT DEFAULT '[]',    -- E-16: JSON array of allowed action names
    fallback_slug TEXT DEFAULT '',      -- E-07: slug of fallback agent on failure
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS provider_performance (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id   INTEGER NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
    complexity    INTEGER NOT NULL CHECK(complexity IN (1,2,3,5,8,13,21,34,55,89)),
    attempts      INTEGER NOT NULL DEFAULT 0,
    successes     INTEGER NOT NULL DEFAULT 0,
    failures      INTEGER NOT NULL DEFAULT 0,
    avg_duration  REAL DEFAULT 0.0,
    last_attempt  TEXT DEFAULT NULL,
    UNIQUE(provider_id, complexity)
);

-- Complexity-to-provider routing: which AI handles which complexity range
CREATE TABLE IF NOT EXISTS complexity_routing (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_name    TEXT NOT NULL DEFAULT '*',     -- '*' = global default, or specific project
    min_complexity  INTEGER NOT NULL DEFAULT 1,
    max_complexity  INTEGER NOT NULL DEFAULT 89,
    provider_id     INTEGER NOT NULL REFERENCES providers(id),
    priority        INTEGER NOT NULL DEFAULT 0,    -- lower = preferred (0 = first choice)
    enabled         INTEGER NOT NULL DEFAULT 1,
    UNIQUE(project_name, provider_id, min_complexity, max_complexity)
);
CREATE INDEX IF NOT EXISTS idx_cr_project ON complexity_routing(project_name);
CREATE INDEX IF NOT EXISTS idx_cr_complexity ON complexity_routing(min_complexity, max_complexity);

CREATE TABLE IF NOT EXISTS usage_limits (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id   INTEGER NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
    period        TEXT NOT NULL DEFAULT 'daily' CHECK(period IN ('hourly','daily','weekly','monthly')),
    max_requests  INTEGER DEFAULT NULL,
    max_tokens    INTEGER DEFAULT NULL,
    max_cost_usd  REAL DEFAULT NULL,
    UNIQUE(provider_id, period)
);

CREATE TABLE IF NOT EXISTS usage_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id   INTEGER NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
    agent_slug    TEXT DEFAULT '',
    request_tokens INTEGER DEFAULT 0,
    response_tokens INTEGER DEFAULT 0,
    cost_usd      REAL DEFAULT 0.0,
    duration_ms   INTEGER DEFAULT 0,
    success       INTEGER NOT NULL DEFAULT 1,
    error_message TEXT DEFAULT '',
    session_id    TEXT DEFAULT '',
    timestamp     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_usage_provider ON usage_log(provider_id);
CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_log(timestamp);

CREATE TABLE IF NOT EXISTS workflows (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,
    description   TEXT DEFAULT '',
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS workflow_steps (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id   INTEGER NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    step_order    INTEGER NOT NULL,
    step_type     TEXT NOT NULL,
    agent_slug    TEXT DEFAULT '',
    config_json   TEXT DEFAULT '{}',
    description   TEXT DEFAULT '',
    UNIQUE(workflow_id, step_order)
);

CREATE TABLE IF NOT EXISTS default_sprint_phases (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    phase_order   INTEGER NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    phase_type    TEXT NOT NULL DEFAULT 'agent'
                  CHECK(phase_type IN ('agent','cmd','builtin','review')),
    agent_slug    TEXT DEFAULT '',
    prompt        TEXT DEFAULT '',
    description   TEXT DEFAULT '',
    enabled       INTEGER NOT NULL DEFAULT 1,
    config_json   TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS global_settings (
    key           TEXT PRIMARY KEY,
    value         TEXT NOT NULL DEFAULT '',
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- S3-W07: provider health check log
CREATE TABLE IF NOT EXISTS provider_health_log (
    id            INTEGER PRIMARY KEY,
    provider_name TEXT,
    latency_ms    INTEGER,
    status        TEXT,
    checked_at    TEXT DEFAULT (datetime('now'))
);

-- ── Registry tables (schema v2) ─────────────────────────────────────

CREATE TABLE IF NOT EXISTS known_projects (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    name                 TEXT NOT NULL,
    directory            TEXT NOT NULL UNIQUE,
    goal                 TEXT DEFAULT '',
    empty_backlog_prompt TEXT DEFAULT '',
    empty_backlog_agent  TEXT DEFAULT 'scrum-master',
    empty_sprint_prompt  TEXT DEFAULT '',
    create_sprint_prompt TEXT DEFAULT '',
    config_id            INTEGER DEFAULT NULL,
    last_opened          TEXT DEFAULT NULL,
    created_at           TEXT NOT NULL DEFAULT (datetime('now')),
    sort_order           INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_kp_dir ON known_projects(directory);

CREATE TABLE IF NOT EXISTS configs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT NOT NULL,
    description      TEXT DEFAULT '',
    parent_config_id INTEGER DEFAULT NULL REFERENCES configs(id),
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS config_overrides (
    config_id   INTEGER NOT NULL REFERENCES configs(id) ON DELETE CASCADE,
    scope       TEXT NOT NULL CHECK(scope IN ('agents','standards','pipeline','settings')),
    key         TEXT NOT NULL,
    value       TEXT DEFAULT '',
    UNIQUE(config_id, scope, key)
);

CREATE TABLE IF NOT EXISTS managers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    goal        TEXT DEFAULT '',
    config_id   INTEGER DEFAULT NULL REFERENCES configs(id),
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS manager_projects (
    manager_id  INTEGER NOT NULL REFERENCES managers(id) ON DELETE CASCADE,
    project_dir TEXT NOT NULL,
    UNIQUE(manager_id, project_dir)
);

CREATE TABLE IF NOT EXISTS management_tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    manager_id   INTEGER NOT NULL REFERENCES managers(id) ON DELETE CASCADE,
    title        TEXT NOT NULL,
    description  TEXT DEFAULT '',
    priority     TEXT NOT NULL DEFAULT 'MEDIUM' CHECK(priority IN ('HIGH','MEDIUM','LOW')),
    status       TEXT NOT NULL DEFAULT 'backlog' CHECK(status IN ('backlog','in_progress','done')),
    agent_slug   TEXT DEFAULT '',
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT DEFAULT NULL
);

-- C-09: Security audit log — records create/delete actions for agents, providers, etc.
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY,
    timestamp   TEXT DEFAULT (datetime('now')),
    action      TEXT NOT NULL,   -- 'create_agent', 'delete_provider', etc.
    actor       TEXT DEFAULT 'system',  -- 'api', 'cli', or username
    resource    TEXT,            -- 'agent:developer', 'provider:ollama-1'
    details     TEXT,            -- JSON extra details
    ip_address  TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action);

-- Triggers for updated_at on global tables
CREATE TRIGGER IF NOT EXISTS trg_providers_updated AFTER UPDATE ON providers
BEGIN UPDATE providers SET updated_at = datetime('now') WHERE id = NEW.id; END;

CREATE TRIGGER IF NOT EXISTS trg_agent_defs_updated AFTER UPDATE ON agent_definitions
BEGIN UPDATE agent_definitions SET updated_at = datetime('now') WHERE id = NEW.id; END;

CREATE TRIGGER IF NOT EXISTS trg_global_settings_updated AFTER UPDATE ON global_settings
BEGIN UPDATE global_settings SET updated_at = datetime('now') WHERE key = NEW.key; END;

-- H2-01: Device registry and auth audit log
CREATE TABLE IF NOT EXISTS device_registry (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id    TEXT NOT NULL UNIQUE,
    device_name  TEXT NOT NULL DEFAULT '',
    platform     TEXT NOT NULL DEFAULT '',
    token_hash   TEXT NOT NULL,
    role         TEXT NOT NULL DEFAULT 'admin' CHECK(role IN ('admin','readonly')),
    paired_at    TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen    TEXT DEFAULT NULL,
    revoked      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_dev_token ON device_registry(token_hash);
CREATE INDEX IF NOT EXISTS idx_dev_revoked ON device_registry(revoked);

CREATE TABLE IF NOT EXISTS auth_audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    event        TEXT NOT NULL,
    device_id    TEXT DEFAULT '',
    device_name  TEXT DEFAULT '',
    ip_address   TEXT DEFAULT '',
    success      INTEGER NOT NULL DEFAULT 1,
    detail       TEXT DEFAULT '',
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_aal_event ON auth_audit_log(event);
CREATE INDEX IF NOT EXISTS idx_aal_created ON auth_audit_log(created_at);
"""

# ── Project DB Schema ───────────────────────────────────────────────

PROJECT_SCHEMA = """
CREATE TABLE IF NOT EXISTS _schema_version (
    version   INTEGER NOT NULL,
    applied   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS project_tasks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    display_id    TEXT NOT NULL UNIQUE,
    title         TEXT NOT NULL,
    goal          TEXT DEFAULT '',
    description   TEXT DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','paused','completed','archived')),
    priority      TEXT NOT NULL DEFAULT 'MEDIUM' CHECK(priority IN ('CRITICAL','HIGH','MEDIUM','LOW')),
    empty_backlog_prompt TEXT DEFAULT '',
    empty_backlog_agent  TEXT DEFAULT 'scrum-master',
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tasks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    display_id    TEXT NOT NULL UNIQUE,
    project_task_id INTEGER DEFAULT NULL REFERENCES project_tasks(id),
    title         TEXT NOT NULL,
    description   TEXT DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'backlog' CHECK(status IN
                    ('backlog','sprint_planned','in_progress','in_review','done','blocked','archived')),
    priority      TEXT NOT NULL DEFAULT 'MEDIUM' CHECK(priority IN ('CRITICAL','HIGH','MEDIUM','LOW')),
    complexity    INTEGER NOT NULL DEFAULT 3 CHECK(complexity IN (1,2,3,5,8,13,21,34,55,89)),
    agent_slug    TEXT DEFAULT '',
    provider_name TEXT DEFAULT '',
    model_name    TEXT DEFAULT '',
    guid          TEXT DEFAULT '',
    sort_order    INTEGER NOT NULL DEFAULT 0,
    parallel_ok   INTEGER NOT NULL DEFAULT 0,
    value         TEXT DEFAULT '',
    acceptance_criteria TEXT DEFAULT '',
    blocked_reason TEXT DEFAULT '',
    depends_on_task_id INTEGER DEFAULT NULL REFERENCES tasks(id),
    parent_task_id INTEGER DEFAULT NULL REFERENCES tasks(id),
    reserved_by   TEXT DEFAULT '',
    reserved_at   TEXT DEFAULT NULL,
    assigned_agent TEXT DEFAULT '',
    assigned_provider TEXT DEFAULT '',
    assigned_model TEXT DEFAULT '',
    assigned_at   TEXT DEFAULT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    failure_count INTEGER NOT NULL DEFAULT 0,
    last_failure_at TEXT DEFAULT NULL,
    updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
    started_at    TEXT DEFAULT NULL,
    completed_at  TEXT DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS sprints (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    sprint_number INTEGER NOT NULL UNIQUE,
    name          TEXT DEFAULT '',
    goal          TEXT DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'planning' CHECK(status IN ('planning','active','review','completed')),
    planned_points INTEGER DEFAULT 0,
    completed_points INTEGER DEFAULT 0,
    velocity      REAL DEFAULT 0.0,
    start_date    TEXT DEFAULT NULL,
    end_date      TEXT DEFAULT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sprint_tasks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    display_id    TEXT NOT NULL UNIQUE,
    sprint_id     INTEGER NOT NULL REFERENCES sprints(id) ON DELETE CASCADE,
    task_id       INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    status        TEXT NOT NULL DEFAULT 'todo' CHECK(status IN ('todo','in_progress','review','done','blocked')),
    assigned_agent TEXT DEFAULT '',
    assigned_provider TEXT DEFAULT '',
    assigned_model TEXT DEFAULT '',
    reserved_by   TEXT DEFAULT '',
    reserved_at   TEXT DEFAULT NULL,
    assigned_at   TEXT DEFAULT NULL,
    story_points  INTEGER DEFAULT NULL,
    notes         TEXT DEFAULT '',
    iterations    INTEGER DEFAULT 0,
    duration_ms   INTEGER DEFAULT 0,
    attempt_number INTEGER DEFAULT 1,
    total_tokens  INTEGER DEFAULT 0,
    handoff_json  TEXT DEFAULT NULL,
    added_at      TEXT NOT NULL DEFAULT (datetime('now')),
    started_at    TEXT DEFAULT NULL,
    completed_at  TEXT DEFAULT NULL,
    UNIQUE(sprint_id, task_id)
);

CREATE TABLE IF NOT EXISTS subtasks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    display_id    TEXT NOT NULL UNIQUE,
    task_id       INTEGER DEFAULT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    sprint_task_id INTEGER DEFAULT NULL REFERENCES sprint_tasks(id) ON DELETE CASCADE,
    title         TEXT NOT NULL,
    description   TEXT DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'todo' CHECK(status IN ('todo','in_progress','done','blocked')),
    sort_order    INTEGER NOT NULL DEFAULT 0,
    guid          TEXT DEFAULT '',
    assigned_agent TEXT DEFAULT '',
    assigned_provider TEXT DEFAULT '',
    assigned_model TEXT DEFAULT '',
    reserved_by   TEXT DEFAULT '',
    reserved_at   TEXT DEFAULT NULL,
    assigned_at   TEXT DEFAULT NULL,
    started_at    TEXT DEFAULT NULL,
    completed_at  TEXT DEFAULT NULL,
    completed_by  TEXT DEFAULT '',
    result        TEXT DEFAULT '',
    iterations    INTEGER DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sprint_ceremonies (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    sprint_id     INTEGER NOT NULL REFERENCES sprints(id) ON DELETE CASCADE,
    ceremony_type TEXT NOT NULL CHECK(ceremony_type IN ('planning','daily','review','retro')),
    notes         TEXT DEFAULT '',
    data_json     TEXT DEFAULT '{}',
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS escalation_log (
    id            INTEGER PRIMARY KEY,
    task_id       INTEGER REFERENCES tasks(id),
    sprint_id     INTEGER REFERENCES sprints(id),
    session_id    TEXT,
    iteration     INTEGER,
    reason        TEXT,
    context_snapshot TEXT,
    created_at    TEXT DEFAULT (datetime('now'))
);
"""


def log_escalation(
    conn: sqlite3.Connection,
    task_display_id: str,
    session_id: str,
    iteration: int,
    reason: str,
    context_snapshot: str,
) -> int:
    """Log an escalation event when a task needs help."""
    cur = conn.cursor()
    cur.execute("""
        SELECT t.id, st.sprint_id
        FROM tasks t
        LEFT JOIN sprint_tasks st ON st.task_id = t.id
        WHERE t.display_id = ?
        ORDER BY st.id DESC LIMIT 1
    """, (task_display_id,))
    row = cur.fetchone()
    task_id = row[0] if row else None
    sprint_id = row[1] if row and row[1] else None
    cur.execute("""
        INSERT INTO escalation_log (task_id, sprint_id, session_id, iteration, reason, context_snapshot)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (task_id, sprint_id, session_id, iteration, reason, context_snapshot[:5000]))
    conn.commit()
    return cur.lastrowid


# ── Project files (kept inside PROJECT_SCHEMA string above) ─────────────────
_PROJECT_TRIGGERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS triggers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    type          TEXT NOT NULL,
    config_json   TEXT DEFAULT '{}',
    enabled       INTEGER DEFAULT 1,
    created_at    TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at    TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_triggers_enabled ON triggers(enabled);
"""

_PROJECT_FILES_SCHEMA = """
CREATE TABLE IF NOT EXISTS project_files (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    path          TEXT NOT NULL UNIQUE,
    extension     TEXT DEFAULT '',
    language      TEXT DEFAULT '',
    size_bytes    INTEGER DEFAULT 0,
    line_count    INTEGER DEFAULT 0,
    last_hash     TEXT DEFAULT '',
    last_scanned  TEXT DEFAULT (datetime('now')),
    description   TEXT DEFAULT '',
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_files_ext ON project_files(extension);
CREATE INDEX IF NOT EXISTS idx_files_lang ON project_files(language);

CREATE TABLE IF NOT EXISTS entity_files (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type   TEXT NOT NULL CHECK(entity_type IN ('project_task','task','sprint_task','subtask')),
    entity_id     INTEGER NOT NULL,
    file_id       INTEGER NOT NULL REFERENCES project_files(id) ON DELETE CASCADE,
    relationship  TEXT DEFAULT 'related' CHECK(relationship IN
                    ('related','modified','created','reviewed','depends_on','output')),
    notes         TEXT DEFAULT '',
    added_by      TEXT DEFAULT '',
    added_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ef_entity ON entity_files(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_ef_file ON entity_files(file_id);

CREATE TABLE IF NOT EXISTS entity_modifications (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type   TEXT NOT NULL CHECK(entity_type IN ('project_task','task','sprint_task','subtask')),
    entity_id     INTEGER NOT NULL,
    file_id       INTEGER NOT NULL REFERENCES project_files(id),
    action        TEXT NOT NULL CHECK(action IN ('create','edit','delete')),
    line_start    INTEGER DEFAULT NULL,
    line_end      INTEGER DEFAULT NULL,
    before_hash   TEXT DEFAULT '',
    after_hash    TEXT DEFAULT '',
    before_snippet TEXT DEFAULT '',
    after_snippet TEXT DEFAULT '',
    session_id    TEXT DEFAULT '',
    timestamp     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_em_entity ON entity_modifications(entity_type, entity_id);

CREATE TABLE IF NOT EXISTS action_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    scope         TEXT NOT NULL CHECK(scope IN
                    ('project_task','task','sprint_task','subtask','sprint','project','file','checklist')),
    scope_id      INTEGER DEFAULT NULL,
    actor         TEXT NOT NULL DEFAULT '',
    action        TEXT NOT NULL,
    detail        TEXT DEFAULT '{}',
    session_id    TEXT DEFAULT '',
    timestamp     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_action_scope ON action_log(scope, scope_id);
CREATE INDEX IF NOT EXISTS idx_action_actor ON action_log(actor);
CREATE INDEX IF NOT EXISTS idx_action_ts ON action_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_action_session ON action_log(session_id);

CREATE TABLE IF NOT EXISTS file_symbols (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id       INTEGER NOT NULL REFERENCES project_files(id) ON DELETE CASCADE,
    symbol_type   TEXT NOT NULL CHECK(symbol_type IN
                    ('function','method','class','struct','enum','namespace',
                     'variable','constant','json_key','section','todo','fixme')),
    name          TEXT NOT NULL,
    signature     TEXT DEFAULT '',
    line_start    INTEGER NOT NULL,
    line_end      INTEGER DEFAULT NULL,
    parent_symbol_id INTEGER DEFAULT NULL REFERENCES file_symbols(id),
    visibility    TEXT DEFAULT '',
    description   TEXT DEFAULT '',
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_symbols_file ON file_symbols(file_id);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON file_symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_type ON file_symbols(symbol_type);

CREATE TABLE IF NOT EXISTS file_sections (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id       INTEGER NOT NULL REFERENCES project_files(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    line_start    INTEGER NOT NULL,
    line_end      INTEGER NOT NULL,
    content_hash  TEXT DEFAULT '',
    content_preview TEXT DEFAULT '',
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_sections_file ON file_sections(file_id);

CREATE TABLE IF NOT EXISTS prompt_responses (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT NOT NULL DEFAULT '',
    agent_slug    TEXT DEFAULT '',
    provider_name TEXT DEFAULT '',
    model_name    TEXT DEFAULT '',
    task_display_id TEXT DEFAULT '',
    prompt_json   TEXT DEFAULT '{}',
    response_json TEXT DEFAULT '{}',
    action        TEXT DEFAULT '',
    tokens_in     INTEGER DEFAULT 0,
    tokens_out    INTEGER DEFAULT 0,
    duration_ms   INTEGER DEFAULT 0,
    success       INTEGER NOT NULL DEFAULT 1,
    error_message TEXT DEFAULT '',
    timestamp     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_pr_session ON prompt_responses(session_id);
CREATE INDEX IF NOT EXISTS idx_pr_task ON prompt_responses(task_display_id);

CREATE TABLE IF NOT EXISTS pipeline_steps (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    step_order    INTEGER NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    phase_type    TEXT NOT NULL DEFAULT 'agent'
                  CHECK(phase_type IN ('agent','cmd','builtin','review')),
    agent_slug    TEXT DEFAULT '',
    prompt        TEXT DEFAULT '',
    description   TEXT DEFAULT '',
    enabled       INTEGER NOT NULL DEFAULT 1,
    is_default    INTEGER NOT NULL DEFAULT 0,
    default_phase_id INTEGER DEFAULT NULL,
    removed       INTEGER NOT NULL DEFAULT 0,
    config_json   TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS vcs_commits (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    commit_hash   TEXT NOT NULL,
    entity_type   TEXT DEFAULT NULL CHECK(entity_type IN ('project_task','task','sprint_task','subtask')),
    entity_id     INTEGER DEFAULT NULL,
    message       TEXT DEFAULT '',
    author        TEXT DEFAULT '',
    branch        TEXT DEFAULT '',
    files_changed INTEGER DEFAULT 0,
    insertions    INTEGER DEFAULT 0,
    deletions     INTEGER DEFAULT 0,
    session_id    TEXT DEFAULT '',
    committed_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_vcs_entity ON vcs_commits(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_vcs_hash ON vcs_commits(commit_hash);

CREATE TABLE IF NOT EXISTS project_settings (
    key           TEXT PRIMARY KEY,
    value         TEXT NOT NULL DEFAULT '',
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ── Display ID Triggers ────────────────────────────────────────────

CREATE TRIGGER IF NOT EXISTS trg_project_task_display_id AFTER INSERT ON project_tasks
WHEN NEW.display_id = ''
BEGIN
    UPDATE project_tasks SET display_id = 'PROJ-' || printf('%03d', NEW.id) WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_task_display_id AFTER INSERT ON tasks
WHEN NEW.display_id = ''
BEGIN
    UPDATE tasks SET display_id = 'TASK-' || printf('%03d', NEW.id) WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_sprint_task_display_id AFTER INSERT ON sprint_tasks
WHEN NEW.display_id = ''
BEGIN
    UPDATE sprint_tasks SET display_id = 'ST-' || printf('%03d', NEW.id) WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_subtask_display_id AFTER INSERT ON subtasks
WHEN NEW.display_id = ''
BEGIN
    UPDATE subtasks SET display_id = 'SUB-' || printf('%03d', NEW.id) WHERE id = NEW.id;
END;

-- ── Updated-at Triggers ────────────────────────────────────────────

CREATE TRIGGER IF NOT EXISTS trg_project_tasks_updated AFTER UPDATE ON project_tasks
BEGIN UPDATE project_tasks SET updated_at = datetime('now') WHERE id = NEW.id; END;

CREATE TRIGGER IF NOT EXISTS trg_tasks_updated AFTER UPDATE ON tasks
BEGIN UPDATE tasks SET updated_at = datetime('now') WHERE id = NEW.id; END;

CREATE TRIGGER IF NOT EXISTS trg_sprints_updated AFTER UPDATE ON sprints
BEGIN UPDATE sprints SET updated_at = datetime('now') WHERE id = NEW.id; END;

CREATE TRIGGER IF NOT EXISTS trg_project_settings_updated AFTER UPDATE ON project_settings
BEGIN UPDATE project_settings SET updated_at = datetime('now') WHERE key = NEW.key; END;

CREATE TABLE IF NOT EXISTS discarded_files (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    original_path TEXT NOT NULL,
    original_name TEXT NOT NULL,
    discard_name  TEXT NOT NULL,
    entity_type   TEXT DEFAULT '',
    entity_id     INTEGER DEFAULT NULL,
    discarded_by  TEXT DEFAULT '',
    reason        TEXT DEFAULT '',
    size_bytes    INTEGER DEFAULT 0,
    file_hash     TEXT DEFAULT '',
    discarded_at  TEXT NOT NULL DEFAULT (datetime('now')),
    restored_at   TEXT DEFAULT NULL,
    restored      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_discard_path ON discarded_files(original_path);
CREATE INDEX IF NOT EXISTS idx_discard_date ON discarded_files(discarded_at);

CREATE TABLE IF NOT EXISTS code_sections (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guid            TEXT NOT NULL UNIQUE,
    persistent_id   TEXT DEFAULT NULL,            -- stable identity GUID (survives renames)
    description     TEXT NOT NULL,
    section_type    TEXT NOT NULL CHECK(section_type IN
                      ('function','method','class','struct','enum','namespace',
                       'section','param','config','block')),
    file_path       TEXT DEFAULT '',
    language        TEXT DEFAULT '',
    line_start      INTEGER DEFAULT NULL,
    line_end        INTEGER DEFAULT NULL,
    last_verified   TEXT DEFAULT NULL,
    location_stale  INTEGER NOT NULL DEFAULT 0,
    replaced_by     TEXT DEFAULT NULL,
    replaces        TEXT DEFAULT NULL,
    revision        INTEGER NOT NULL DEFAULT 1,
    task_id         INTEGER DEFAULT NULL REFERENCES tasks(id),
    subtask_id      INTEGER DEFAULT NULL,
    sprint_id       INTEGER DEFAULT NULL REFERENCES sprints(id),
    created_by      TEXT DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_cs_guid ON code_sections(guid);
CREATE INDEX IF NOT EXISTS idx_cs_persistent ON code_sections(persistent_id);
CREATE INDEX IF NOT EXISTS idx_cs_file ON code_sections(file_path);
CREATE INDEX IF NOT EXISTS idx_cs_type ON code_sections(section_type);
CREATE INDEX IF NOT EXISTS idx_cs_replaced ON code_sections(replaced_by);
CREATE INDEX IF NOT EXISTS idx_cs_task ON code_sections(task_id);
CREATE INDEX IF NOT EXISTS idx_cs_stale ON code_sections(location_stale);

-- File locking: agents lock files while working, others must wait
CREATE TABLE IF NOT EXISTS file_locks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path       TEXT NOT NULL UNIQUE,
    locked_by       TEXT NOT NULL,                -- agent slug
    session_id      TEXT DEFAULT '',
    task_id         INTEGER DEFAULT NULL REFERENCES tasks(id),
    content_hash    TEXT DEFAULT '',               -- SHA-256 at lock time
    locked_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_fl_path ON file_locks(file_path);
CREATE INDEX IF NOT EXISTS idx_fl_agent ON file_locks(locked_by);

-- File content hashes: track what agents have read so we know if file changed
CREATE TABLE IF NOT EXISTS file_read_hashes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path       TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    content_hash    TEXT NOT NULL,                 -- SHA-256 when agent read it
    read_at         TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_frh_path ON file_read_hashes(file_path, session_id);

-- Note: tasks.status can be set to 'archived' for soft delete
-- For hard tracking, we use the discarded_files table for actual file deletions

-- Agent view state: tracks which paths/sections an agent has "opened" in the view
-- Scoped per session_id (cleared when step_done is called)
CREATE TABLE IF NOT EXISTS view_state (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    path       TEXT NOT NULL,            -- e.g. "src/main.py" or "src/main.py/Ball.__init__"
    is_open    INTEGER DEFAULT 1,
    line_start INTEGER DEFAULT NULL,     -- optional line range start
    line_end   INTEGER DEFAULT NULL,     -- optional line range end
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(session_id, path)
);
CREATE INDEX IF NOT EXISTS idx_vs_session ON view_state(session_id);
CREATE INDEX IF NOT EXISTS idx_vs_path ON view_state(path);

-- Agent conversation history: compressed storage for agent message history
CREATE TABLE IF NOT EXISTS agent_history (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id            INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    sprint_id          INTEGER DEFAULT NULL REFERENCES sprints(id) ON DELETE SET NULL,
    iteration          INTEGER NOT NULL DEFAULT 0,
    role               TEXT NOT NULL CHECK(role IN ('user','assistant','system')),
    content_compressed BLOB NOT NULL,
    token_estimate     INTEGER DEFAULT 0,
    created_at         TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ah_task ON agent_history(task_id);
CREATE INDEX IF NOT EXISTS idx_ah_sprint ON agent_history(sprint_id);
CREATE INDEX IF NOT EXISTS idx_ah_iteration ON agent_history(task_id, iteration);

-- D-18/D-22: Task comment thread
CREATE TABLE IF NOT EXISTS task_comments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    author      TEXT NOT NULL DEFAULT '',
    content     TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_tc_task ON task_comments(task_id);

-- D-19/D-23: Sprint retrospective
CREATE TABLE IF NOT EXISTS sprint_retrospective (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sprint_id   INTEGER NOT NULL UNIQUE REFERENCES sprints(id) ON DELETE CASCADE,
    went_well   TEXT NOT NULL DEFAULT '',
    to_improve  TEXT NOT NULL DEFAULT '',
    action_items TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_sr_sprint ON sprint_retrospective(sprint_id);

-- E-21: Agent execution history — one row per agent run on a task
CREATE TABLE IF NOT EXISTS agent_execution_history (
    id              INTEGER PRIMARY KEY,
    agent_slug      TEXT NOT NULL,
    task_display_id TEXT,
    sprint_id       INTEGER,
    session_id      TEXT,
    status          TEXT,  -- 'done', 'failed', 'blocked'
    duration_s      REAL,
    total_tokens    INTEGER DEFAULT 0,
    files_changed   INTEGER DEFAULT 0,
    created_at      TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_aeh_slug ON agent_execution_history(agent_slug);
CREATE INDEX IF NOT EXISTS idx_aeh_task ON agent_execution_history(task_display_id);

-- H3-21: TODO comment items extracted from source files
CREATE TABLE IF NOT EXISTS todos (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path    TEXT NOT NULL,
    line_num     INTEGER NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'TODO',
    text         TEXT NOT NULL DEFAULT '',
    tags         TEXT DEFAULT '[]',
    is_agent     INTEGER NOT NULL DEFAULT 0,
    priority     TEXT DEFAULT 'normal',
    section      TEXT DEFAULT '',
    resolved     INTEGER NOT NULL DEFAULT 0,
    task_id      INTEGER DEFAULT NULL REFERENCES tasks(id),
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_todos_file ON todos(file_path);
CREATE INDEX IF NOT EXISTS idx_todos_kind ON todos(kind);
CREATE INDEX IF NOT EXISTS idx_todos_resolved ON todos(resolved);
"""

# ═══════════════════════════════════════════════════════════════════
# Connection Management
# ═══════════════════════════════════════════════════════════════════


def _configure_connection(conn: sqlite3.Connection) -> sqlite3.Connection:
    """C-14: Apply security and performance PRAGMAs to every SQLite connection."""
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_connection(db_path: Path) -> sqlite3.Connection:
    """Create a connection with WAL mode, foreign keys, and row factory. (C-14)"""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _configure_connection(conn)
    return conn


def get_schema_version(conn: sqlite3.Connection) -> int:
    """Get current schema version. Returns 0 if table does not exist."""
    try:
        row = conn.execute(
            "SELECT MAX(version) FROM _schema_version"
        ).fetchone()
        return row[0] if row and row[0] is not None else 0
    except sqlite3.OperationalError:
        return 0


def _set_schema_version(conn: sqlite3.Connection, version: int):
    """Record a schema version."""
    conn.execute(
        "INSERT INTO _schema_version (version) VALUES (?)", (version,)
    )


# C-19: Track which DB paths have had their integrity checked this process lifetime
_integrity_checked: set = set()


def _check_integrity(conn: sqlite3.Connection, db_path: Path):
    """C-19: Run PRAGMA integrity_check once per DB path per process start."""
    path_key = str(db_path)
    if path_key in _integrity_checked:
        return
    _integrity_checked.add(path_key)
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()
        if result and result[0] != 'ok':
            import warnings
            warnings.warn(f'Database integrity check failed for {db_path}: {result[0]}')
    except Exception:
        pass


def _set_dir_permissions(directory: Path):
    """C-10: Set ~/.automatica directory to owner-only permissions (700)."""
    import stat
    import os as _os
    try:
        _os.chmod(str(directory), stat.S_IRWXU)  # 700 — owner only
    except Exception:
        pass  # Windows doesn't support chmod — graceful skip


def init_global_db(db_path: Path = None) -> sqlite3.Connection:
    """Initialize the global database. Creates tables if needed.

    If db_path is None, defaults to automatica.db next to this script
    (or next to the frozen EXE).
    """
    if db_path is None:
        if getattr(sys, 'frozen', False):
            base = Path(sys.executable).parent
        else:
            base = Path(__file__).parent
        db_path = base / "automatica.db"

    # C-10: set directory permissions to owner-only
    automatica_dir = Path(db_path).parent
    automatica_dir.mkdir(parents=True, exist_ok=True)
    _set_dir_permissions(automatica_dir)

    conn = init_connection(db_path)
    # C-19: integrity check on first open per process
    _check_integrity(conn, db_path)

    version = get_schema_version(conn)
    if version < SCHEMA_VERSION:
        conn.executescript(GLOBAL_SCHEMA)
        _set_schema_version(conn, SCHEMA_VERSION)
        conn.commit()
        migrate_global(conn)

    # Seed default Ollama providers on first init or if missing
    seed_ollama_providers(conn)

    return conn


def init_project_db(project_dir: Path) -> sqlite3.Connection:
    """Initialize a project database at {project_dir}/.automatica/project.db."""
    project_dir = Path(project_dir)
    db_path = project_dir / ".automatica" / "project.db"

    # C-10: set directory permissions to owner-only
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _set_dir_permissions(db_path.parent)

    conn = init_connection(db_path)
    # C-19: integrity check on first open per process
    _check_integrity(conn, db_path)

    version = get_schema_version(conn)
    if version < SCHEMA_VERSION:
        conn.executescript(PROJECT_SCHEMA)
        conn.commit()
        migrate_project(conn, from_version=version)  # migrate BEFORE setting final version
        _set_schema_version(conn, SCHEMA_VERSION)
        conn.commit()
    # Always apply auxiliary schema (all statements use IF NOT EXISTS — safe to re-run)
    conn.executescript(_PROJECT_FILES_SCHEMA)
    conn.executescript(_PROJECT_TRIGGERS_SCHEMA)
    # WF-3: race protection — manual_claim_until lets --AddTask reserve a task
    # for a window so the autonomous loop doesn't race against humans / parallel
    # agents on the same task. Idempotent column add.
    _add_column_if_missing(conn, 'tasks', 'manual_claim_until', 'TEXT DEFAULT NULL')
    conn.commit()
    # G-06: Indexes for common query patterns (only on tables that actually exist)
    tables_present = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    index_ddl = ["CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);",
                 "CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_task_id);",
                 "CREATE INDEX IF NOT EXISTS idx_sprint_tasks_sprint ON sprint_tasks(sprint_id);",
                 "CREATE INDEX IF NOT EXISTS idx_subtasks_task ON subtasks(task_id);"]
    if "action_log" in tables_present:
        index_ddl += [
            "CREATE INDEX IF NOT EXISTS idx_action_log_timestamp ON action_log(timestamp);",
            "CREATE INDEX IF NOT EXISTS idx_action_log_actor ON action_log(actor);",
        ]
    conn.executescript("\n".join(index_ddl))
    conn.commit()
    # G-11: FTS5 full-text search for tasks
    try:
        conn.executescript("""
            CREATE VIRTUAL TABLE IF NOT EXISTS tasks_fts USING fts5(
                display_id, title, description, content='tasks', content_rowid='id'
            );
            CREATE TRIGGER IF NOT EXISTS tasks_fts_insert AFTER INSERT ON tasks BEGIN
                INSERT INTO tasks_fts(rowid, display_id, title, description)
                VALUES (new.id, new.display_id, new.title, new.description);
            END;
            CREATE TRIGGER IF NOT EXISTS tasks_fts_update AFTER UPDATE ON tasks BEGIN
                INSERT INTO tasks_fts(tasks_fts, rowid, display_id, title, description)
                VALUES ('delete', old.id, old.display_id, old.title, old.description);
                INSERT INTO tasks_fts(rowid, display_id, title, description)
                VALUES (new.id, new.display_id, new.title, new.description);
            END;
        """)
        conn.commit()
    except Exception:
        pass  # FTS5 may not be available on all SQLite builds
    # One-time: seed agents-config.json / pipeline-config.json into project_settings
    seed_project_settings_from_files(conn, project_dir)
    # One-time: seed default pipeline steps if table is empty
    seed_default_pipeline(conn)
    return conn


def migrate_global(conn: sqlite3.Connection):
    """Run incremental migrations for the global DB."""
    version = get_schema_version(conn)

    if version < 2:
        # v2: add registry tables and seed global_goal
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS known_projects (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                name                 TEXT NOT NULL,
                directory            TEXT NOT NULL UNIQUE,
                goal                 TEXT DEFAULT '',
                empty_backlog_prompt TEXT DEFAULT '',
                empty_backlog_agent  TEXT DEFAULT 'scrum-master',
                empty_sprint_prompt  TEXT DEFAULT '',
                create_sprint_prompt TEXT DEFAULT '',
                config_id            INTEGER DEFAULT NULL,
                last_opened          TEXT DEFAULT NULL,
                created_at           TEXT NOT NULL DEFAULT (datetime('now')),
                sort_order           INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_kp_dir ON known_projects(directory);

            CREATE TABLE IF NOT EXISTS configs (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                name             TEXT NOT NULL,
                description      TEXT DEFAULT '',
                parent_config_id INTEGER DEFAULT NULL REFERENCES configs(id),
                created_at       TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS config_overrides (
                config_id   INTEGER NOT NULL REFERENCES configs(id) ON DELETE CASCADE,
                scope       TEXT NOT NULL,
                key         TEXT NOT NULL,
                value       TEXT DEFAULT '',
                UNIQUE(config_id, scope, key)
            );

            CREATE TABLE IF NOT EXISTS managers (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                goal        TEXT DEFAULT '',
                config_id   INTEGER DEFAULT NULL REFERENCES configs(id),
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS manager_projects (
                manager_id  INTEGER NOT NULL REFERENCES managers(id) ON DELETE CASCADE,
                project_dir TEXT NOT NULL,
                UNIQUE(manager_id, project_dir)
            );

            CREATE TABLE IF NOT EXISTS management_tasks (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                manager_id   INTEGER NOT NULL REFERENCES managers(id) ON DELETE CASCADE,
                title        TEXT NOT NULL,
                description  TEXT DEFAULT '',
                priority     TEXT NOT NULL DEFAULT 'MEDIUM',
                status       TEXT NOT NULL DEFAULT 'backlog',
                agent_slug   TEXT DEFAULT '',
                created_at   TEXT NOT NULL DEFAULT (datetime('now')),
                completed_at TEXT DEFAULT NULL
            );

            INSERT OR IGNORE INTO global_settings(key, value) VALUES ('global_goal', '');
        """)
        _set_schema_version(conn, 2)
        conn.commit()

    # E-07/E-16: ensure new agent_definitions columns exist for older global DBs
    _add_column_if_missing(conn, 'agent_definitions', 'action_filter', "TEXT DEFAULT '[]'")
    _add_column_if_missing(conn, 'agent_definitions', 'fallback_slug', "TEXT DEFAULT ''")
    conn.commit()


def migrate_project(conn: sqlite3.Connection, from_version: int = None):
    """Run migrations for project DB up to SCHEMA_VERSION."""
    version = from_version if from_version is not None else get_schema_version(conn)

    if version < 3:
        # v3: add model_name, guid, assigned_model, iterations, duration_ms columns
        _add_column_if_missing(conn, 'tasks', 'model_name', 'TEXT DEFAULT ""')
        _add_column_if_missing(conn, 'tasks', 'guid', 'TEXT DEFAULT ""')
        _add_column_if_missing(conn, 'tasks', 'assigned_model', 'TEXT DEFAULT ""')
        _add_column_if_missing(conn, 'sprint_tasks', 'assigned_model', 'TEXT DEFAULT ""')
        _add_column_if_missing(conn, 'sprint_tasks', 'iterations', 'INTEGER DEFAULT 0')
        _add_column_if_missing(conn, 'sprint_tasks', 'duration_ms', 'INTEGER DEFAULT 0')
        _add_column_if_missing(conn, 'subtasks', 'guid', 'TEXT DEFAULT ""')
        _add_column_if_missing(conn, 'subtasks', 'assigned_provider', 'TEXT DEFAULT ""')
        _add_column_if_missing(conn, 'subtasks', 'assigned_model', 'TEXT DEFAULT ""')
        _add_column_if_missing(conn, 'prompt_responses', 'model_name', 'TEXT DEFAULT ""')
        _add_column_if_missing(conn, 'prompt_responses', 'action', 'TEXT DEFAULT ""')
        conn.commit()
        _set_schema_version(conn, 3)

    if version < 4:
        # v4: attempt tracking, handoff data, token usage, escalation log
        _add_column_if_missing(conn, 'sprint_tasks', 'attempt_number', 'INTEGER DEFAULT 1')
        _add_column_if_missing(conn, 'sprint_tasks', 'total_tokens', 'INTEGER DEFAULT 0')
        _add_column_if_missing(conn, 'tasks', 'handoff_json', 'TEXT DEFAULT NULL')
        conn.execute("""
            CREATE TABLE IF NOT EXISTS escalation_log (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id        INTEGER REFERENCES tasks(id),
                sprint_id      INTEGER REFERENCES sprints(id),
                session_id     TEXT DEFAULT '',
                iteration      INTEGER DEFAULT 0,
                reason         TEXT DEFAULT '',
                context_snapshot TEXT DEFAULT '',
                created_at     TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.commit()
        _set_schema_version(conn, 4)

    if version < 5:
        # v5: agent handoff protocol for sprint_tasks
        _add_column_if_missing(conn, 'sprint_tasks', 'handoff_json', 'TEXT DEFAULT NULL')
        conn.commit()
        _set_schema_version(conn, 5)

    if version < 6:
        # v6: per-task failure tracking for retry cap (max 5 attempts)
        _add_column_if_missing(conn, 'tasks', 'failure_count', 'INTEGER NOT NULL DEFAULT 0')
        _add_column_if_missing(conn, 'tasks', 'last_failure_at', 'TEXT DEFAULT NULL')
        conn.commit()
        _set_schema_version(conn, 6)

    # Retry-cap: ensure failure_count / last_failure_at exist for older project DBs
    _add_column_if_missing(conn, 'tasks', 'failure_count', 'INTEGER NOT NULL DEFAULT 0')
    _add_column_if_missing(conn, 'tasks', 'last_failure_at', 'TEXT DEFAULT NULL')
    # D-13: ensure sort_order column exists on tasks (guard for pre-existing DBs)
    _add_column_if_missing(conn, 'tasks', 'sort_order', 'INTEGER NOT NULL DEFAULT 0')
    # D-16: estimated/actual hours columns
    _add_column_if_missing(conn, 'tasks', 'estimated_hours', 'REAL DEFAULT 0')
    _add_column_if_missing(conn, 'tasks', 'actual_hours', 'REAL DEFAULT 0')
    # D-17: acceptance_criteria column (already in schema but guard for older DBs)
    _add_column_if_missing(conn, 'tasks', 'acceptance_criteria', 'TEXT DEFAULT ""')
    # D-18/D-22: task_comments table
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS task_comments (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id     INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                author      TEXT NOT NULL DEFAULT '',
                content     TEXT NOT NULL DEFAULT '',
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tc_task ON task_comments(task_id)")
    except sqlite3.OperationalError:
        pass
    # D-19/D-23: sprint_retrospective table
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sprint_retrospective (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                sprint_id   INTEGER NOT NULL UNIQUE REFERENCES sprints(id) ON DELETE CASCADE,
                went_well   TEXT NOT NULL DEFAULT '',
                to_improve  TEXT NOT NULL DEFAULT '',
                action_items TEXT NOT NULL DEFAULT '',
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sr_sprint ON sprint_retrospective(sprint_id)")
    except sqlite3.OperationalError:
        pass
    # E-21: agent_execution_history table (ensure exists for older project DBs)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_execution_history (
                id              INTEGER PRIMARY KEY,
                agent_slug      TEXT NOT NULL,
                task_display_id TEXT,
                sprint_id       INTEGER,
                session_id      TEXT,
                status          TEXT,
                duration_s      REAL,
                total_tokens    INTEGER DEFAULT 0,
                files_changed   INTEGER DEFAULT 0,
                created_at      TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_aeh_slug ON agent_execution_history(agent_slug)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_aeh_task ON agent_execution_history(task_display_id)")
    except sqlite3.OperationalError:
        pass
    conn.commit()


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, definition: str):
    """Add a column to a table if it doesn't already exist (safe migration helper)."""
    # Table may not exist in DBs created from newer schemas where the table was removed
    tbl_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if not tbl_exists:
        return
    existing = [row[1] for row in conn.execute(f'PRAGMA table_info({table})').fetchall()]
    if column not in existing:
        conn.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')


# ═══════════════════════════════════════════════════════════════════
# E-21: Agent execution history helpers
# ═══════════════════════════════════════════════════════════════════


def record_agent_execution(
    conn: sqlite3.Connection,
    agent_slug: str,
    task_display_id: str,
    session_id: str,
    status: str,
    duration_s: float,
    total_tokens: int,
    files_changed: int,
    sprint_id: int = None,
) -> int:
    """Insert one row into agent_execution_history. Returns the new row id."""
    cur = conn.execute(
        """INSERT INTO agent_execution_history
           (agent_slug, task_display_id, sprint_id, session_id,
            status, duration_s, total_tokens, files_changed)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (agent_slug, task_display_id, sprint_id, session_id,
         status, duration_s, total_tokens, files_changed),
    )
    conn.commit()
    return cur.lastrowid


def get_agent_stats(conn: sqlite3.Connection, agent_slug: str) -> dict:
    """Return success_rate, avg_duration, total_tokens for a single agent."""
    row = conn.execute(
        """SELECT
               COUNT(*) AS runs,
               SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) AS successes,
               AVG(duration_s) AS avg_duration,
               SUM(total_tokens) AS total_tokens,
               MAX(created_at) AS last_run
           FROM agent_execution_history
           WHERE agent_slug = ?""",
        (agent_slug,),
    ).fetchone()
    if not row:
        return {'slug': agent_slug, 'runs': 0, 'success_rate': 0.0,
                'avg_duration': 0.0, 'total_tokens': 0, 'last_run': None}
    runs = row['runs'] or 0
    successes = row['successes'] or 0
    return {
        'slug': agent_slug,
        'runs': runs,
        'success_rate': round(successes / runs * 100, 1) if runs else 0.0,
        'avg_duration': round(row['avg_duration'] or 0.0, 1),
        'total_tokens': row['total_tokens'] or 0,
        'last_run': row['last_run'],
    }


def get_all_agent_stats(conn: sqlite3.Connection) -> list:
    """Return per-agent stats for all agents that have execution history."""
    rows = conn.execute(
        """SELECT
               agent_slug,
               COUNT(*) AS runs,
               SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) AS successes,
               AVG(duration_s) AS avg_duration,
               SUM(total_tokens) AS total_tokens,
               MAX(created_at) AS last_run
           FROM agent_execution_history
           GROUP BY agent_slug
           ORDER BY runs DESC""",
    ).fetchall()
    result = []
    for r in rows:
        runs = r['runs'] or 0
        successes = r['successes'] or 0
        result.append({
            'slug': r['agent_slug'],
            'runs': runs,
            'success_rate': round(successes / runs * 100, 1) if runs else 0.0,
            'avg_duration': round(r['avg_duration'] or 0.0, 1),
            'total_tokens': r['total_tokens'] or 0,
            'last_run': r['last_run'],
        })
    return result


def get_sprint_cost_summary(conn: sqlite3.Connection, sprint_id: int) -> dict:
    """Return total_tokens and per-agent breakdown for a sprint. (E-20)"""
    total_row = conn.execute(
        "SELECT SUM(total_tokens) AS total FROM sprint_tasks WHERE sprint_id = ?",
        (sprint_id,),
    ).fetchone()
    total_tokens = total_row['total'] or 0 if total_row else 0

    agent_rows = conn.execute(
        """SELECT assigned_agent, SUM(total_tokens) AS tokens
           FROM sprint_tasks WHERE sprint_id = ?
           GROUP BY assigned_agent""",
        (sprint_id,),
    ).fetchall()
    by_agent = {r['assigned_agent']: r['tokens'] for r in agent_rows if r['assigned_agent']}

    cost_usd = total_tokens * 0.000003
    return {
        'sprint_id': sprint_id,
        'total_tokens': total_tokens,
        'estimated_cost_usd': round(cost_usd, 6),
        'by_agent': by_agent,
    }


# ═══════════════════════════════════════════════════════════════════
# Prompt Builder Queries
# ═══════════════════════════════════════════════════════════════════


def build_task_context(conn: sqlite3.Connection, task_display_id: str) -> dict:
    """Assemble everything an agent needs for a task.

    Returns structured dict with:
      task       — full task row
      parent_chain — list of ancestor tasks (recursive CTE)
      children   — immediate child tasks
      subtasks   — checklist items with status
      recent_history — last 20 action_log entries
      file_modifications — recent edits linked to this task
      linked_files — files associated with this task
      sprint_context — sprint info if task is sprint-planned
    """
    # 1. Task details
    task_row = conn.execute(
        "SELECT * FROM tasks WHERE display_id = ?", (task_display_id,)
    ).fetchone()
    if not task_row:
        return {"error": f"Task {task_display_id} not found"}
    task = dict(task_row)
    task_id = task["id"]

    # 2. Parent chain via recursive CTE
    parent_chain = []
    parent_rows = conn.execute("""
        WITH RECURSIVE ancestors(id, display_id, title, status, parent_task_id, depth) AS (
            SELECT id, display_id, title, status, parent_task_id, 0
            FROM tasks WHERE id = (SELECT parent_task_id FROM tasks WHERE id = ?)
            UNION ALL
            SELECT t.id, t.display_id, t.title, t.status, t.parent_task_id, a.depth + 1
            FROM tasks t JOIN ancestors a ON t.id = a.parent_task_id
            WHERE a.depth < 10
        )
        SELECT * FROM ancestors ORDER BY depth DESC
    """, (task_id,)).fetchall()
    parent_chain = [dict(r) for r in parent_rows]

    # Project task parent (if linked)
    project_task = None
    if task.get("project_task_id"):
        pt_row = conn.execute(
            "SELECT * FROM project_tasks WHERE id = ?", (task["project_task_id"],)
        ).fetchone()
        if pt_row:
            project_task = dict(pt_row)

    # 3. Children
    children = [dict(r) for r in conn.execute(
        "SELECT id, display_id, title, status, complexity, sort_order "
        "FROM tasks WHERE parent_task_id = ? ORDER BY sort_order, id",
        (task_id,)
    ).fetchall()]

    # 4. Subtasks (checklist)
    subtasks = [dict(r) for r in conn.execute(
        "SELECT id, display_id, title, status, sort_order, completed_at, completed_by "
        "FROM subtasks WHERE task_id = ? ORDER BY sort_order, id",
        (task_id,)
    ).fetchall()]

    # Also get subtasks via sprint_task link
    sprint_subtasks = [dict(r) for r in conn.execute("""
        SELECT sub.id, sub.display_id, sub.title, sub.status, sub.sort_order,
               sub.completed_at, sub.completed_by
        FROM subtasks sub
        JOIN sprint_tasks st ON sub.sprint_task_id = st.id
        WHERE st.task_id = ?
        ORDER BY sub.sort_order, sub.id
    """, (task_id,)).fetchall()]
    # Merge, avoiding duplicates
    seen_ids = {s["id"] for s in subtasks}
    for ss in sprint_subtasks:
        if ss["id"] not in seen_ids:
            subtasks.append(ss)

    # 5. Recent action history (last 20)
    history = [dict(r) for r in conn.execute("""
        SELECT id, actor, action, detail, session_id, timestamp
        FROM action_log
        WHERE (scope = 'task' AND scope_id = ?)
           OR (scope = 'subtask' AND scope_id IN (
               SELECT id FROM subtasks WHERE task_id = ?))
        ORDER BY timestamp DESC LIMIT 20
    """, (task_id, task_id)).fetchall()]

    # 6. File modifications
    modifications = [dict(r) for r in conn.execute("""
        SELECT em.id, pf.path, em.action, em.before_snippet, em.after_snippet,
               em.line_start, em.line_end, em.timestamp
        FROM entity_modifications em
        JOIN project_files pf ON em.file_id = pf.id
        WHERE em.entity_type = 'task' AND em.entity_id = ?
        ORDER BY em.timestamp DESC LIMIT 20
    """, (task_id,)).fetchall()]

    # 7. Linked files
    linked_files = [dict(r) for r in conn.execute("""
        SELECT ef.relationship, ef.notes, ef.added_by, pf.path, pf.language, pf.line_count
        FROM entity_files ef
        JOIN project_files pf ON ef.file_id = pf.id
        WHERE ef.entity_type = 'task' AND ef.entity_id = ?
        ORDER BY ef.added_at DESC
    """, (task_id,)).fetchall()]

    # 8. Sprint context
    sprint_context = None
    st_row = conn.execute("""
        SELECT st.*, s.sprint_number, s.name AS sprint_name, s.goal, s.status AS sprint_status,
               s.planned_points, s.completed_points, s.velocity
        FROM sprint_tasks st
        JOIN sprints s ON st.sprint_id = s.id
        WHERE st.task_id = ?
        ORDER BY s.sprint_number DESC LIMIT 1
    """, (task_id,)).fetchone()
    if st_row:
        sprint_context = dict(st_row)

    # Build rich file contexts with GUID section histories for linked files
    file_contexts = []
    for lf in linked_files[:5]:  # Limit to 5 files to keep prompt manageable
        try:
            fc = build_file_context(conn, lf.get('path', ''))
            if fc:
                file_contexts.append(fc)
        except Exception:
            pass

    return {
        "task": task,
        "project_task": project_task,
        "parent_chain": parent_chain,
        "children": children,
        "subtasks": subtasks,
        "recent_history": history,
        "file_modifications": modifications,
        "linked_files": linked_files,
        "file_contexts": file_contexts,
        "sprint_context": sprint_context,
    }


def build_sprint_summary(conn: sqlite3.Connection, sprint_number: int = None) -> dict:
    """Get sprint overview with velocity, ceremonies, task statuses.

    If sprint_number is None, uses the most recent active sprint.
    """
    if sprint_number is not None:
        sprint_row = conn.execute(
            "SELECT * FROM sprints WHERE sprint_number = ?", (sprint_number,)
        ).fetchone()
    else:
        sprint_row = conn.execute(
            "SELECT * FROM sprints ORDER BY sprint_number DESC LIMIT 1"
        ).fetchone()

    if not sprint_row:
        return {"error": "No sprint found"}
    sprint = dict(sprint_row)
    sprint_id = sprint["id"]

    # Task breakdown by status
    status_counts = {}
    for row in conn.execute("""
        SELECT st.status, COUNT(*) AS cnt, COALESCE(SUM(st.story_points), 0) AS points
        FROM sprint_tasks st WHERE st.sprint_id = ?
        GROUP BY st.status
    """, (sprint_id,)):
        status_counts[row["status"]] = {"count": row["cnt"], "points": row["points"]}

    # Sprint tasks with details
    tasks = [dict(r) for r in conn.execute("""
        SELECT st.display_id AS sprint_task_id, st.status, st.assigned_agent,
               st.story_points, t.display_id AS task_display_id, t.title, t.complexity
        FROM sprint_tasks st
        JOIN tasks t ON st.task_id = t.id
        WHERE st.sprint_id = ?
        ORDER BY st.status, t.sort_order
    """, (sprint_id,)).fetchall()]

    # Ceremonies
    ceremonies = [dict(r) for r in conn.execute(
        "SELECT ceremony_type, notes, data_json, created_at "
        "FROM sprint_ceremonies WHERE sprint_id = ? ORDER BY created_at",
        (sprint_id,)
    ).fetchall()]

    return {
        "sprint": sprint,
        "status_counts": status_counts,
        "tasks": tasks,
        "ceremonies": ceremonies,
    }


def build_task_timeline(conn: sqlite3.Connection, task_display_id: str) -> list:
    """Get chronological dot-point summary of all actions on a task.

    Returns list of dicts: [{timestamp, actor, summary}, ...]
    """
    task_row = conn.execute(
        "SELECT id FROM tasks WHERE display_id = ?", (task_display_id,)
    ).fetchone()
    if not task_row:
        return []
    task_id = task_row["id"]

    rows = conn.execute("""
        SELECT timestamp, actor, action, detail FROM action_log
        WHERE (scope = 'task' AND scope_id = ?)
           OR (scope = 'subtask' AND scope_id IN (
               SELECT id FROM subtasks WHERE task_id = ?))
           OR (scope = 'sprint_task' AND scope_id IN (
               SELECT id FROM sprint_tasks WHERE task_id = ?))
        ORDER BY timestamp ASC
    """, (task_id, task_id, task_id)).fetchall()

    timeline = []
    for r in rows:
        detail = {}
        try:
            detail = json.loads(r["detail"]) if r["detail"] else {}
        except (json.JSONDecodeError, TypeError):
            pass
        summary = _format_action_summary(r["action"], detail)
        timeline.append({
            "timestamp": r["timestamp"],
            "actor": r["actor"],
            "action": r["action"],
            "summary": summary,
        })
    return timeline


def _format_action_summary(action: str, detail: dict) -> str:
    """Format an action_log entry into a human-readable dot point."""
    msg = detail.get("message", "")
    old = detail.get("old_value", "")
    new = detail.get("new_value", "")

    if action == "status_change" and old and new:
        return f"Status changed: {old} -> {new}"
    if action == "created":
        return f"Created{': ' + msg if msg else ''}"
    if action == "completed":
        return f"Completed{': ' + msg if msg else ''}"
    if action == "assigned":
        return f"Assigned to {new}" if new else "Assigned"
    if action == "file_modified":
        path = detail.get("file_path", "")
        return f"Modified {path}" if path else "File modified"
    if action == "commented":
        return f"Comment: {msg[:100]}" if msg else "Comment added"
    if msg:
        return f"{action}: {msg}"
    return action.replace("_", " ").capitalize()


# ── Standard Prompt Context Builders ──────────────────────────────

def build_project_context(conn) -> dict:
    """Build overview context for the entire project."""
    project = get_project_task(conn)
    sprints = get_all_sprints(conn)
    backlog = get_backlog(conn, limit=100)
    active_sprint = get_active_sprint(conn)
    blocked = get_blocked_tasks(conn)
    stale = get_stale_reservations(conn)

    return {
        'project': dict(project) if project else {},
        'active_sprint': dict(active_sprint) if active_sprint else {},
        'sprint_count': len(sprints),
        'backlog_count': len(backlog),
        'backlog_points': sum(t.get('complexity', 3) for t in backlog),
        'blocked_tasks': blocked,
        'stale_reservations': stale,
        'total_sprints': len(sprints),
        'completed_sprints': len([s for s in sprints if s['status'] == 'completed']),
    }

def build_backlog_context(conn) -> dict:
    """Build context for backlog management / empty backlog generation."""
    backlog = get_backlog(conn, limit=100)
    eb_config = get_empty_backlog_config(conn)
    project = get_project_task(conn)

    return {
        'project_goal': project.get('goal', '') if project else '',
        'empty_backlog_prompt': eb_config.get('prompt', ''),
        'empty_backlog_agent': eb_config.get('agent', ''),
        'backlog_tasks': backlog,
        'backlog_count': len(backlog),
        'total_points': sum(t.get('complexity', 3) for t in backlog),
        'by_priority': {
            'CRITICAL': len([t for t in backlog if t['priority'] == 'CRITICAL']),
            'HIGH': len([t for t in backlog if t['priority'] == 'HIGH']),
            'MEDIUM': len([t for t in backlog if t['priority'] == 'MEDIUM']),
            'LOW': len([t for t in backlog if t['priority'] == 'LOW']),
        },
    }

def build_agent_workload(conn, global_conn = None) -> dict:
    """Build team/agent workload overview."""
    in_progress = [dict(r) for r in conn.execute(
        "SELECT assigned_agent, COUNT(*) as count, SUM(complexity) as points "
        "FROM tasks WHERE status = 'in_progress' AND assigned_agent != '' "
        "GROUP BY assigned_agent"
    ).fetchall()]

    reserved = [dict(r) for r in conn.execute(
        "SELECT reserved_by, COUNT(*) as count "
        "FROM tasks WHERE reserved_by != '' "
        "GROUP BY reserved_by"
    ).fetchall()]

    agents = []
    if global_conn:
        agents = [dict(r) for r in global_conn.execute(
            "SELECT slug, display_name, role, max_complexity FROM agent_definitions WHERE enabled = 1"
        ).fetchall()]

    return {
        'agents': agents,
        'in_progress_by_agent': in_progress,
        'reserved_by_agent': reserved,
    }


# ═══════════════════════════════════════════════════════════════════
# CRUD Helpers
# ═══════════════════════════════════════════════════════════════════


def create_task(
    conn: sqlite3.Connection,
    title: str,
    description: str = "",
    priority: str = "MEDIUM",
    complexity: int = 3,
    agent_slug: str = "",
    project_task_id: Optional[int] = None,
    parent_task_id: Optional[int] = None,
    depends_on_task_id: Optional[int] = None,
    acceptance_criteria: str = "",
    parallel_ok: bool = False,
    value: str = "",
) -> dict:
    """Create a task and return {id, display_id}."""
    max_order = conn.execute(
        "SELECT COALESCE(MAX(sort_order), -1) FROM tasks"
    ).fetchone()[0]

    cur = conn.execute("""
        INSERT INTO tasks (display_id, title, description, priority, complexity,
                           agent_slug, project_task_id, parent_task_id, sort_order,
                           depends_on_task_id, acceptance_criteria, parallel_ok, value)
        VALUES ('', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (title, description, priority, complexity, agent_slug,
          project_task_id, parent_task_id, max_order + 1,
          depends_on_task_id, acceptance_criteria, int(parallel_ok), value))
    conn.commit()

    task_id = cur.lastrowid
    row = conn.execute(
        "SELECT id, display_id FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    return dict(row)


def update_task(conn: sqlite3.Connection, task_display_id: str, **fields) -> bool:
    """Update task fields. Returns True on success.

    Allowed fields: title, description, status, priority, complexity,
    agent_slug, provider_name, sort_order, parallel_ok, value,
    acceptance_criteria, blocked_reason, depends_on_task_id, parent_task_id.
    """
    allowed = {
        "title", "description", "status", "priority", "complexity",
        "agent_slug", "provider_name", "model_name", "guid",
        "sort_order", "parallel_ok", "value",
        "acceptance_criteria", "blocked_reason", "depends_on_task_id",
        "parent_task_id", "assigned_agent", "assigned_provider", "assigned_model",
        "started_at", "completed_at", "handoff_json",
        "estimated_hours", "actual_hours",  # D-16
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False

    # Handle timestamp side effects (auto-populate if not explicitly given)
    if updates.get("status") == "in_progress" and "started_at" not in updates:
        updates["started_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    if updates.get("status") == "done" and "completed_at" not in updates:
        updates["completed_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [task_display_id]
    cur = conn.execute(
        f"UPDATE tasks SET {set_clause} WHERE display_id = ?", values
    )
    conn.commit()
    return cur.rowcount > 0


def reorder_task(conn: sqlite3.Connection, task_display_id: str, new_position: int) -> bool:
    """Move task to new sort_order position within its status group."""
    task_row = conn.execute(
        "SELECT id, status FROM tasks WHERE display_id = ?", (task_display_id,)
    ).fetchone()
    if not task_row:
        return False

    task_id = task_row["id"]
    status = task_row["status"]

    # Get all tasks in same status group, ordered
    siblings = conn.execute(
        "SELECT id FROM tasks WHERE status = ? ORDER BY sort_order, id",
        (status,)
    ).fetchall()
    sibling_ids = [r["id"] for r in siblings]

    # Remove and reinsert at new position
    if task_id in sibling_ids:
        sibling_ids.remove(task_id)
    new_position = max(0, min(new_position, len(sibling_ids)))
    sibling_ids.insert(new_position, task_id)

    # Reassign sort_order
    for idx, sid in enumerate(sibling_ids):
        conn.execute("UPDATE tasks SET sort_order = ? WHERE id = ?", (idx, sid))
    conn.commit()
    return True


def add_subtask(
    conn: sqlite3.Connection,
    task_display_id: str,
    title: str,
    sprint_task_id: Optional[int] = None,
    description: str = "",
) -> dict:
    """Create a subtask under a task. Returns {id, display_id}.
    
    Automatically enriches the subtask description with planning context:
    - Parent task title and plan
    - Specific files identified for this subtask
    - Results from previously completed subtasks
    """
    task_row = conn.execute(
        "SELECT id, title, description FROM tasks WHERE display_id = ?", (task_display_id,)
    ).fetchone()
    if not task_row:
        return {"error": f"Task {task_display_id} not found"}
    task_id = task_row["id"]
    
    # Build enriched description with planning context
    context_parts = []
    
    # 1. Parent task title and plan
    parent_title = task_row["title"] or ""
    parent_plan = task_row["description"] or ""
    if parent_title:
        context_parts.append(f"## Parent Task: {task_display_id} - {parent_title}")
    if parent_plan:
        context_parts.append(f"\n### Parent Plan:\n{parent_plan}")
    
    # 2. Get files identified for this subtask (from task description or acceptance criteria)
    # Look for file paths mentioned in the subtask title or description
    files_mentioned = []
    combined_text = title + " " + description
    # Match common file patterns (e.g., path/to/file.ext, file.py, etc.)
    import re as _re
    file_pattern = r'\b[\w/\\.-]+\.(py|js|ts|cpp|hpp|c|h|java|go|rs|md|json|yaml|yml|txt|css|html|xml|sql)\b'
    files_mentioned = list(set(_re.findall(file_pattern, combined_text, _re.IGNORECASE)))
    
    if files_mentioned:
        context_parts.append(f"\n### Files for this subtask:\n" + "\n".join(f"- {f}" for f in files_mentioned))
    
    # 3. Results from previously completed subtasks
    completed_subtasks = conn.execute("""
        SELECT display_id, title, description, completed_at, completed_by
        FROM subtasks
        WHERE task_id = ? AND status = 'done'
        ORDER BY completed_at ASC
    """, (task_id,)).fetchall()
    
    if completed_subtasks:
        context_parts.append("\n### Previously Completed Subtasks:")
        for sub in completed_subtasks:
            sub_dict = dict(sub)
            context_parts.append(f"\n#### {sub_dict['display_id']}: {sub_dict['title']}")
            if sub_dict.get('description'):
                # Extract result from description if present
                sub_desc = sub_dict['description']
                if sub_desc and len(sub_desc) > 0:
                    context_parts.append(f"Result: {sub_desc[:200]}{'...' if len(sub_desc) > 200 else ''}")
    
    # Combine user description with planning context
    enriched_description = description
    if context_parts:
        planning_context = "\n".join(context_parts)
        if enriched_description:
            enriched_description = f"{enriched_description}\n\n---\n# Planning Context\n{planning_context}"
        else:
            enriched_description = f"# Planning Context\n{planning_context}"

    max_order = conn.execute(
        "SELECT COALESCE(MAX(sort_order), -1) FROM subtasks WHERE task_id = ?",
        (task_id,)
    ).fetchone()[0]

    cur = conn.execute("""
        INSERT INTO subtasks (display_id, task_id, sprint_task_id, title, description, sort_order)
        VALUES ('', ?, ?, ?, ?, ?)
    """, (task_id, sprint_task_id, title, enriched_description, max_order + 1))
    conn.commit()

    sub_id = cur.lastrowid
    row = conn.execute(
        "SELECT id, display_id FROM subtasks WHERE id = ?", (sub_id,)
    ).fetchone()
    return dict(row)


def check_subtask(
    conn: sqlite3.Connection,
    subtask_id: int,
    checked: bool = True,
    by: str = "",
) -> bool:
    """Toggle subtask completion."""
    if checked:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "UPDATE subtasks SET status = 'done', completed_at = ?, completed_by = ? WHERE id = ?",
            (now, by, subtask_id),
        )
    else:
        conn.execute(
            "UPDATE subtasks SET status = 'todo', completed_at = NULL, completed_by = '' WHERE id = ?",
            (subtask_id,),
        )
    conn.commit()
    return conn.total_changes > 0


def create_subtask(pconn: sqlite3.Connection, parent_display_id: str, title: str, description: str = '') -> dict:
    """Create a subtask under a parent task. Returns {id, display_id}.

    Alias wrapper that delegates to add_subtask for agent-action compatibility.
    """
    return add_subtask(pconn, parent_display_id, title, description=description)


def complete_subtask(pconn: sqlite3.Connection, subtask_display_id: str, result: str) -> dict:
    """Mark a subtask as done with a result string.

    Also checks if all sibling subtasks are done; if so updates parent task status to 'done'.
    Returns {success, parent_completed}.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    pconn.execute(
        "UPDATE subtasks SET status = 'done', completed_at = ?, description = ? WHERE display_id = ?",
        (now, result, subtask_display_id),
    )
    pconn.commit()
    if pconn.total_changes == 0:
        return {"success": False, "parent_completed": False}

    # Determine parent task id
    row = pconn.execute(
        "SELECT task_id FROM subtasks WHERE display_id = ?", (subtask_display_id,)
    ).fetchone()
    if not row or not row["task_id"]:
        return {"success": True, "parent_completed": False}

    task_id = row["task_id"]

    # Check if all sibling subtasks are done
    total = pconn.execute(
        "SELECT COUNT(*) FROM subtasks WHERE task_id = ?", (task_id,)
    ).fetchone()[0]
    done_count = pconn.execute(
        "SELECT COUNT(*) FROM subtasks WHERE task_id = ? AND status = 'done'", (task_id,)
    ).fetchone()[0]

    parent_completed = False
    if total > 0 and done_count >= total:
        pconn.execute(
            "UPDATE tasks SET status = 'done', completed_at = ? WHERE id = ?",
            (now, task_id),
        )
        pconn.commit()
        parent_completed = True

    return {"success": True, "parent_completed": parent_completed}


def log_escalation(
    pconn: sqlite3.Connection,
    task_display_id: str,
    session_id: str,
    iteration: int,
    reason: str,
    context_snapshot: str,
) -> None:
    """Log an escalation event to escalation_log."""
    task_row = pconn.execute(
        "SELECT id FROM tasks WHERE display_id = ?", (task_display_id,)
    ).fetchone()
    task_id = task_row["id"] if task_row else None
    pconn.execute(
        "INSERT INTO escalation_log (task_id, session_id, iteration, reason, context_snapshot) "
        "VALUES (?, ?, ?, ?, ?)",
        (task_id, session_id, iteration, reason, context_snapshot),
    )
    pconn.commit()


def log_action(
    conn: sqlite3.Connection,
    scope: str,
    scope_id: int,
    actor: str,
    action: str,
    detail: Optional[dict] = None,
    session_id: str = "",
) -> int:
    """Log an action to action_log. Returns log entry id."""
    detail_json = json.dumps(detail) if detail else "{}"
    cur = conn.execute("""
        INSERT INTO action_log (scope, scope_id, actor, action, detail, session_id)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (scope, scope_id, actor, action, detail_json, session_id))
    conn.commit()
    return cur.lastrowid


def log_file_modification(
    conn: sqlite3.Connection,
    entity_type: str,
    entity_id: int,
    file_path: str,
    action: str,
    before_snippet: str = "",
    after_snippet: str = "",
    session_id: str = "",
) -> int:
    """Record a file modification linked to an entity.

    Ensures the file exists in project_files (upserts by path).
    Returns the entity_modifications row id.
    """
    # Upsert into project_files
    conn.execute("""
        INSERT INTO project_files (path) VALUES (?)
        ON CONFLICT(path) DO NOTHING
    """, (file_path,))
    file_row = conn.execute(
        "SELECT id FROM project_files WHERE path = ?", (file_path,)
    ).fetchone()
    file_id = file_row["id"]

    cur = conn.execute("""
        INSERT INTO entity_modifications
            (entity_type, entity_id, file_id, action, before_snippet, after_snippet, session_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (entity_type, entity_id, file_id, action, before_snippet, after_snippet, session_id))
    conn.commit()
    return cur.lastrowid


# ── Update functions ──────────────────────────────────────────────

def update_project_task(conn, display_id: str, **fields) -> bool:
    allowed = {'title', 'goal', 'description', 'status', 'priority',
               'empty_backlog_prompt', 'empty_backlog_agent'}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates: return False
    set_clause = ', '.join(f'{k} = ?' for k in updates)
    conn.execute(f"UPDATE project_tasks SET {set_clause} WHERE display_id = ?",
                  [*updates.values(), display_id])
    conn.commit()
    return True

def update_sprint(conn, sprint_number: int, **fields) -> bool:
    allowed = {'name', 'goal', 'status', 'planned_points', 'completed_points',
               'velocity', 'start_date', 'end_date'}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates: return False
    set_clause = ', '.join(f'{k} = ?' for k in updates)
    conn.execute(f"UPDATE sprints SET {set_clause} WHERE sprint_number = ?",
                  [*updates.values(), sprint_number])
    conn.commit()
    return True

def update_subtask(conn, subtask_id: int, **fields) -> bool:
    allowed = {'title', 'description', 'status', 'sort_order', 'assigned_agent'}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates: return False
    set_clause = ', '.join(f'{k} = ?' for k in updates)
    conn.execute(f"UPDATE subtasks SET {set_clause} WHERE id = ?",
                  [*updates.values(), subtask_id])
    conn.commit()
    return True

def update_sprint_task(conn, sprint_task_id: int, **fields) -> bool:
    allowed = {'status', 'assigned_agent', 'assigned_provider', 'story_points', 'notes', 'total_tokens', 'handoff_json'}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates: return False
    set_clause = ', '.join(f'{k} = ?' for k in updates)
    conn.execute(f"UPDATE sprint_tasks SET {set_clause} WHERE id = ?",
                  [*updates.values(), sprint_task_id])
    conn.commit()
    return True

def remove_task_from_sprint(conn, sprint_task_id: int) -> bool:
    conn.execute("DELETE FROM sprint_tasks WHERE id = ?", (sprint_task_id,))
    conn.commit()
    return True


# ═══════════════════════════════════════════════════════════════════
# Migration from Flat Files
# ═══════════════════════════════════════════════════════════════════


def migrate_from_todo_md(project_dir: Path, conn: sqlite3.Connection):
    """One-time migration from TODO.md to project.db.

    Parses TODO.md format:
      ## Category Header
      - [ ] Task title | complexity:5 | priority:HIGH
      - [x] Completed task
    """
    todo_path = Path(project_dir) / "TODO.md"
    if not todo_path.exists():
        return

    content = todo_path.read_text(encoding="utf-8", errors="replace")
    current_category = ""

    for line in content.splitlines():
        line = line.strip()

        # Category header
        header_match = re.match(r"^#{1,3}\s+(.+)$", line)
        if header_match:
            current_category = header_match.group(1).strip()
            continue

        # Task line: - [ ] or - [x]
        task_match = re.match(r"^-\s+\[([ xX])\]\s+(.+)$", line)
        if not task_match:
            continue

        checked = task_match.group(1).lower() == "x"
        raw_title = task_match.group(2)

        # Parse optional metadata: | complexity:N | priority:X
        title = raw_title
        complexity = 3
        priority = "MEDIUM"

        parts = raw_title.split("|")
        if len(parts) > 1:
            title = parts[0].strip()
            for part in parts[1:]:
                part = part.strip().lower()
                if part.startswith("complexity:"):
                    try:
                        complexity = int(part.split(":")[1])
                    except (ValueError, IndexError):
                        pass
                elif part.startswith("priority:"):
                    priority = part.split(":")[1].strip().upper()

        status = "done" if checked else "backlog"
        description = f"Migrated from TODO.md [{current_category}]" if current_category else "Migrated from TODO.md"

        cur = conn.execute("""
            INSERT INTO tasks (display_id, title, description, status, priority, complexity, sort_order)
            VALUES ('', ?, ?, ?, ?, ?, (SELECT COALESCE(MAX(sort_order), -1) + 1 FROM tasks))
        """, (title, description, status, priority, complexity))

        if checked:
            conn.execute(
                "UPDATE tasks SET completed_at = datetime('now') WHERE id = ?",
                (cur.lastrowid,)
            )

    conn.commit()


def migrate_from_agents_config(config_path: Path, conn: sqlite3.Connection):
    """Seed agent_definitions from agents-config.json.

    Expected format: list of objects with slug, display_name, role, description, etc.
    """
    config_path = Path(config_path)
    if not config_path.exists():
        return

    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return

    agents = data if isinstance(data, list) else data.get("agents", [])

    for agent in agents:
        if not isinstance(agent, dict):
            continue
        slug = agent.get("slug", agent.get("name", "")).strip()
        if not slug:
            continue

        conn.execute("""
            INSERT INTO agent_definitions (slug, display_name, role, description, system_prompt,
                                           max_complexity, min_complexity, permissions)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(slug) DO UPDATE SET
                display_name = excluded.display_name,
                role = excluded.role,
                description = excluded.description,
                system_prompt = excluded.system_prompt
        """, (
            slug,
            agent.get("display_name", slug),
            agent.get("role", ""),
            agent.get("description", ""),
            agent.get("system_prompt", ""),
            agent.get("max_complexity", 13),
            agent.get("min_complexity", 1),
            json.dumps(agent.get("permissions", {})),
        ))

    conn.commit()


def seed_default_providers(conn: sqlite3.Connection):
    """Insert sample AI providers for testing. Not called in production — DB starts empty."""
    providers = [
        # (name, adapter_type, api_key_env, base_url, model_name, max_tokens)
        ("Sample-ChatCompletions", "http_chat_completions", "CHAT_API_KEY", "https://api.example.com", "model-name", 4096),
        ("Sample-GenerateContent", "http_generate_content", "GEN_CONTENT_API_KEY", "https://generativelanguage.googleapis.com/v1beta/models", "model-name", 8192),
        ("Sample-LocalChat",       "http_local_chat",       "", "http://localhost:11434", "llama3.2:3b", 4096),
        ("Sample-CLI",             "subprocess_cli",         "", "", "", 4096),
    ]

    for name, ptype, key_env, url, model, tokens in providers:
        conn.execute("""
            INSERT INTO providers (name, provider_type, api_key_env, base_url, model_name, max_tokens)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO NOTHING
        """, (name, ptype, key_env, url, model, tokens))

    conn.commit()


def seed_ollama_providers(conn: sqlite3.Connection):
    """Seed default Ollama providers (gemma3:4b and gemma3:12b) if not already present."""
    providers = [
        # (name, adapter_type, api_key_env, base_url, model_name, max_tokens)
        ("Gemma3-4b",  "http_local_chat", "", "http://localhost:11434", "gemma3:4b", 4096),
        ("Gemma3-12b", "http_local_chat", "", "http://localhost:11434", "gemma3:12b", 4096),
    ]

    for name, ptype, key_env, url, model, tokens in providers:
        conn.execute("""
            INSERT INTO providers (name, provider_type, api_key_env, base_url, model_name, max_tokens)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO NOTHING
        """, (name, ptype, key_env, url, model, tokens))

    conn.commit()


def seed_default_sprint_phases(conn: sqlite3.Connection):
    """Insert default sprint phases into the GLOBAL DB."""
    phases = [
        # (order, name, type, agent, prompt, description)
        (1, "Backlog Refinement", "agent", "scrum-master",
         "Review all backlog tasks. Ensure each has a clear description, acceptance criteria, "
         "complexity estimate, and related files identified. Add missing subtasks.",
         "Ensure backlog tasks are well-defined before sprint planning"),
        (2, "Sprint Planning", "agent", "scrum-master",
         "Select tasks from the backlog for this sprint based on priority, dependencies, and team capacity. "
         "Set the sprint goal. Assign agents to tasks. Mark parallel-safe tasks.",
         "Select and organize sprint work"),
        (3, "Task Execution", "builtin", "",
         "", "Execute all sprint tasks with dependency resolution and parallel batching"),
        (4, "Build Verification", "cmd", "",
         "", "Build the project and verify no compilation errors"),
        (5, "Test Verification", "cmd", "",
         "", "Run the test suite and verify all tests pass"),
        (6, "Sprint Review", "review", "product-owner",
         "Summarize what was accomplished this sprint. List completed tasks, points delivered, "
         "and any tasks that were not finished. Assess if sprint goal was met.",
         "Review sprint outcomes with stakeholders"),
        (7, "Sprint Retrospective", "review", "scrum-master",
         "Identify what went well, what could improve, and action items for next sprint. "
         "Note any recurring blockers or process issues.",
         "Continuous improvement"),
        (8, "Sprint Audit", "review", "architect",
         "Review ALL changes made this sprint. For each file modified:\n"
         "1. Check for bugs, missed DB entries, inconsistencies\n"
         "2. Look for repeated manual patterns that could be automated as stored actions\n"
         "3. Verify GUID markers are correctly placed on new/modified functions\n"
         "4. Check that all task subtasks were properly checked off\n"
         "5. Review prompt/response logs for error patterns or wasted iterations\n"
         "6. Suggest improvements to agent prompts/standards that would prevent issues\n"
         "Report findings using add_checklist_item on a review task.",
         "Audit sprint changes for bugs, automation opportunities, and process improvements"),
        (9, "VCS Push", "cmd", "",
         "", "Push all sprint commits to remote repository"),
    ]

    for order, name, ptype, slug, prompt, desc in phases:
        conn.execute("""
            INSERT INTO default_sprint_phases (phase_order, name, phase_type, agent_slug, prompt, description)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(phase_order) DO NOTHING
        """, (order, name, ptype, slug, prompt, desc))
    conn.commit()


def seed_default_pipeline(conn: sqlite3.Connection, global_db: sqlite3.Connection = None):
    """Seed project pipeline_steps from global default_sprint_phases.
    If global_db provided, reads from it (with default_phase_id links).
    Otherwise uses hardcoded fallback."""
    count = conn.execute("SELECT COUNT(*) FROM pipeline_steps").fetchone()[0]
    if count > 0:
        return

    if global_db:
        # Read from global defaults — preserves default_phase_id for reset functionality
        sync_project_pipeline_from_defaults(conn, global_db)
        return

    # Fallback: hardcoded defaults (no reset-to-default capability without global DB)
    phases = [
        (1, "Backlog Refinement", "agent", "scrum-master",
         "Review all backlog tasks. Ensure each has a clear description, acceptance criteria, "
         "complexity estimate, and related files identified.", "Ensure backlog tasks are well-defined"),
        (2, "Sprint Planning", "agent", "scrum-master",
         "Select tasks from the backlog for this sprint. Set sprint goal. Assign agents.",
         "Select and organize sprint work"),
        (3, "Task Execution", "builtin", "", "", "Execute all sprint tasks with dependency resolution"),
        (4, "Build Verification", "cmd", "", "", "Build project and verify no errors"),
        (5, "Test Verification", "cmd", "", "", "Run tests and verify all pass"),
        (6, "Sprint Review", "review", "product-owner",
         "Summarize accomplishments, assess sprint goal.", "Review sprint outcomes"),
        (7, "Sprint Retrospective", "review", "scrum-master",
         "What went well, what to improve, action items.", "Continuous improvement"),
        (8, "VCS Push", "cmd", "", "", "Push all sprint commits"),
    ]

    for order, name, ptype, slug, prompt, desc in phases:
        conn.execute("""
            INSERT INTO pipeline_steps (step_order, name, phase_type, agent_slug, prompt,
                                         description, is_default, enabled)
            VALUES (?, ?, ?, ?, ?, ?, 1, 1)
            ON CONFLICT(step_order) DO NOTHING
        """, (order, name, ptype, slug, prompt, desc))
    conn.commit()


def sync_project_pipeline_from_defaults(project_db: sqlite3.Connection,
                                          global_db: sqlite3.Connection):
    """Sync project pipeline steps from global defaults.
    Adds missing defaults (as is_default=1), preserves project customizations."""
    defaults = global_db.execute(
        "SELECT * FROM default_sprint_phases ORDER BY phase_order"
    ).fetchall()

    for d in defaults:
        # Check if this default already exists in project
        existing = project_db.execute(
            "SELECT id, removed FROM pipeline_steps WHERE default_phase_id = ?",
            (d['id'],)
        ).fetchone()

        if existing:
            continue  # Already exists (even if removed — user chose to remove it)

        # Check if step_order is taken by a custom step
        taken = project_db.execute(
            "SELECT id FROM pipeline_steps WHERE step_order = ?",
            (d['phase_order'],)
        ).fetchone()

        order = d['phase_order']
        if taken:
            # Shift to next available order
            max_order = project_db.execute("SELECT MAX(step_order) FROM pipeline_steps").fetchone()[0] or 0
            order = max_order + 1

        project_db.execute("""
            INSERT INTO pipeline_steps (step_order, name, phase_type, agent_slug, prompt,
                                         description, is_default, default_phase_id, enabled)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, 1)
        """, (order, d['name'], d['phase_type'], d['agent_slug'], d['prompt'],
              d['description'], d['id']))

    project_db.commit()


def reset_pipeline_step_to_default(project_db: sqlite3.Connection,
                                     global_db: sqlite3.Connection,
                                     step_id: int) -> bool:
    """Reset a project pipeline step to its global default values."""
    step = project_db.execute("SELECT * FROM pipeline_steps WHERE id = ?", (step_id,)).fetchone()
    if not step or not step['default_phase_id']:
        return False  # Not a default-based step

    default = global_db.execute(
        "SELECT * FROM default_sprint_phases WHERE id = ?",
        (step['default_phase_id'],)
    ).fetchone()
    if not default:
        return False

    project_db.execute("""
        UPDATE pipeline_steps SET name = ?, phase_type = ?, agent_slug = ?,
               prompt = ?, description = ?, removed = 0, enabled = 1
        WHERE id = ?
    """, (default['name'], default['phase_type'], default['agent_slug'],
          default['prompt'], default['description'], step_id))
    project_db.commit()
    return True


def restore_removed_default_step(project_db: sqlite3.Connection, step_id: int) -> bool:
    """Re-enable a previously removed default step."""
    project_db.execute(
        "UPDATE pipeline_steps SET removed = 0, enabled = 1 WHERE id = ? AND is_default = 1",
        (step_id,))
    project_db.commit()
    return True


def reserve_task(conn: sqlite3.Connection, task_display_id: str, agent: str) -> bool:
    """Reserve a task for an agent. Prevents other agents from picking it up.
    Returns False if already reserved by someone else."""
    row = conn.execute("SELECT id, reserved_by FROM tasks WHERE display_id = ?",
                        (task_display_id,)).fetchone()
    if not row:
        return False
    if row['reserved_by'] and row['reserved_by'] != agent:
        return False  # Already reserved by someone else
    conn.execute("""
        UPDATE tasks SET reserved_by = ?, reserved_at = datetime('now')
        WHERE display_id = ?
    """, (agent, task_display_id))
    conn.commit()
    log_action(conn, 'task', row['id'], agent, 'reserved', {'agent': agent})
    return True


def release_task_reservation(conn: sqlite3.Connection, task_display_id: str) -> bool:
    """Release a task reservation."""
    conn.execute("""
        UPDATE tasks SET reserved_by = '', reserved_at = NULL
        WHERE display_id = ?
    """, (task_display_id,))
    conn.commit()
    return True


def assign_task(conn: sqlite3.Connection, task_display_id: str,
                 agent: str, provider: str = '') -> bool:
    """Assign a task to an agent/provider and record the timestamp."""
    row = conn.execute("SELECT id FROM tasks WHERE display_id = ?",
                        (task_display_id,)).fetchone()
    if not row:
        return False
    conn.execute("""
        UPDATE tasks SET assigned_agent = ?, assigned_provider = ?,
                         assigned_at = datetime('now'), status = 'in_progress',
                         started_at = COALESCE(started_at, datetime('now'))
        WHERE display_id = ?
    """, (agent, provider, task_display_id))
    conn.commit()
    log_action(conn, 'task', row['id'], agent, 'assigned',
               {'agent': agent, 'provider': provider})
    return True


def reserve_subtask(conn: sqlite3.Connection, subtask_id: int, agent: str) -> bool:
    """Reserve a subtask for an agent."""
    row = conn.execute("SELECT reserved_by FROM subtasks WHERE id = ?",
                        (subtask_id,)).fetchone()
    if not row:
        return False
    if row['reserved_by'] and row['reserved_by'] != agent:
        return False
    conn.execute("""
        UPDATE subtasks SET reserved_by = ?, reserved_at = datetime('now'),
                            assigned_agent = ?, assigned_at = datetime('now'),
                            started_at = COALESCE(started_at, datetime('now'))
        WHERE id = ?
    """, (agent, agent, subtask_id))
    conn.commit()
    return True


def get_stale_reservations(conn: sqlite3.Connection, timeout_minutes: int = 30) -> list:
    """Find tasks/subtasks reserved for longer than timeout — may indicate crashed agents."""
    stale_tasks = [dict(r) for r in conn.execute("""
        SELECT display_id, title, reserved_by, reserved_at, status
        FROM tasks
        WHERE reserved_by != '' AND reserved_at IS NOT NULL
          AND datetime(reserved_at, '+' || ? || ' minutes') < datetime('now')
    """, (timeout_minutes,)).fetchall()]

    stale_subtasks = [dict(r) for r in conn.execute("""
        SELECT id, display_id, title, reserved_by, reserved_at, status
        FROM subtasks
        WHERE reserved_by != '' AND reserved_at IS NOT NULL
          AND datetime(reserved_at, '+' || ? || ' minutes') < datetime('now')
    """, (timeout_minutes,)).fetchall()]

    return {'tasks': stale_tasks, 'subtasks': stale_subtasks}


def get_empty_backlog_config(conn: sqlite3.Connection) -> dict:
    """Get the empty backlog prompt/agent config from the project task."""
    row = conn.execute("""
        SELECT empty_backlog_prompt, empty_backlog_agent FROM project_tasks
        WHERE status = 'active' ORDER BY id LIMIT 1
    """).fetchone()
    if row:
        return {'prompt': row['empty_backlog_prompt'] or '', 'agent': row['empty_backlog_agent'] or 'scrum-master'}
    return {'prompt': '', 'agent': 'scrum-master'}


def set_empty_backlog_config(conn: sqlite3.Connection, prompt: str, agent: str = 'scrum-master'):
    """Set the empty backlog prompt/agent on the active project task."""
    conn.execute("""
        UPDATE project_tasks SET empty_backlog_prompt = ?, empty_backlog_agent = ?
        WHERE status = 'active'
    """, (prompt, agent))
    conn.commit()


def get_pipeline_with_removed(conn: sqlite3.Connection) -> dict:
    """Get active pipeline steps + list of removed defaults that can be re-added."""
    active = [dict(r) for r in conn.execute(
        "SELECT * FROM pipeline_steps WHERE removed = 0 ORDER BY step_order"
    ).fetchall()]
    removed = [dict(r) for r in conn.execute(
        "SELECT * FROM pipeline_steps WHERE removed = 1 AND is_default = 1 ORDER BY step_order"
    ).fetchall()]
    return {'active': active, 'removed': removed}


# ═══════════════════════════════════════════════════════════════════
# Safe Delete (Discard) System
# ═══════════════════════════════════════════════════════════════════


def discard_task(conn, task_display_id: str, discarded_by: str = '', reason: str = '') -> bool:
    """Soft-delete a task by setting status to 'archived' and logging the action."""
    row = conn.execute("SELECT id, status FROM tasks WHERE display_id = ?", (task_display_id,)).fetchone()
    if not row: return False
    old_status = row['status']
    conn.execute("UPDATE tasks SET status = 'archived' WHERE display_id = ?", (task_display_id,))
    conn.commit()
    log_action(conn, 'task', row['id'], discarded_by, 'discarded',
               {'old_status': old_status, 'reason': reason})
    return True

def restore_task(conn, task_display_id: str, restore_to: str = 'backlog') -> bool:
    """Restore an archived task."""
    row = conn.execute("SELECT id FROM tasks WHERE display_id = ? AND status = 'archived'",
                        (task_display_id,)).fetchone()
    if not row: return False
    conn.execute("UPDATE tasks SET status = ? WHERE display_id = ?",
                  (restore_to, task_display_id))
    conn.commit()
    log_action(conn, 'task', row['id'], '', 'restored', {'restored_to': restore_to})
    return True

def discard_subtask(conn, subtask_id: int, discarded_by: str = '') -> bool:
    """Soft-delete a subtask by marking as 'blocked' with a discard reason."""
    conn.execute("""
        UPDATE subtasks SET status = 'blocked', description =
            COALESCE(description, '') || '\n[DISCARDED by ' || ? || ' at ' || datetime('now') || ']'
        WHERE id = ?
    """, (discarded_by, subtask_id))
    conn.commit()
    return True

def discard_file(conn, project_dir, file_path: str, discarded_by: str = '',
                  reason: str = '', entity_type: str = '', entity_id: int = None) -> Optional[dict]:
    """Move a file to _Discarded folder and record in DB for recovery.
    Returns {id, discard_name} or None on failure."""
    import shutil, hashlib
    from pathlib import Path as _Path

    src = _Path(project_dir) / file_path
    if not src.exists():
        return None

    discard_dir = _Path(project_dir) / '_Discarded'
    discard_dir.mkdir(exist_ok=True)

    # Generate numbered discard name
    cursor = conn.execute("SELECT MAX(id) FROM discarded_files")
    max_id = cursor.fetchone()[0] or 0
    ext = src.suffix
    discard_name = f"{max_id + 1}{ext}"
    dst = discard_dir / discard_name

    # Compute hash before moving
    file_hash = hashlib.sha256(src.read_bytes()).hexdigest()
    size = src.stat().st_size

    # Move file
    shutil.move(str(src), str(dst))

    # Record in DB
    cursor = conn.execute("""
        INSERT INTO discarded_files (original_path, original_name, discard_name,
                                      entity_type, entity_id, discarded_by, reason,
                                      size_bytes, file_hash)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (file_path, src.name, discard_name, entity_type, entity_id,
          discarded_by, reason, size, file_hash))
    conn.commit()

    log_action(conn, 'file', cursor.lastrowid, discarded_by, 'file_discarded',
               {'original_path': file_path, 'discard_name': discard_name, 'reason': reason})

    return {'id': cursor.lastrowid, 'discard_name': discard_name}

def restore_file(conn, project_dir, discard_id: int = None, original_path: str = None) -> bool:
    """Restore a discarded file to its original location.
    Can find by discard_id or original_path."""
    import shutil
    from pathlib import Path as _Path

    if discard_id:
        row = conn.execute("SELECT * FROM discarded_files WHERE id = ? AND restored = 0",
                            (discard_id,)).fetchone()
    elif original_path:
        row = conn.execute("""
            SELECT * FROM discarded_files WHERE original_path = ? AND restored = 0
            ORDER BY discarded_at DESC LIMIT 1
        """, (original_path,)).fetchone()
    else:
        return False

    if not row:
        return False

    discard_dir = _Path(project_dir) / '_Discarded'
    src = discard_dir / row['discard_name']
    dst = _Path(project_dir) / row['original_path']

    if not src.exists():
        return False

    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))

    conn.execute("""
        UPDATE discarded_files SET restored = 1, restored_at = datetime('now')
        WHERE id = ?
    """, (row['id'],))
    conn.commit()

    log_action(conn, 'file', row['id'], '', 'file_restored',
               {'original_path': row['original_path'], 'discard_name': row['discard_name']})
    return True

def get_discarded_files(conn, original_path: str = None,
                          since: str = None, until: str = None) -> list:
    """Query discarded files with optional filters."""
    sql = "SELECT * FROM discarded_files WHERE restored = 0"
    params = []
    if original_path:
        sql += " AND original_path LIKE ?"
        params.append(f"%{original_path}%")
    if since:
        sql += " AND discarded_at >= ?"
        params.append(since)
    if until:
        sql += " AND discarded_at <= ?"
        params.append(until)
    sql += " ORDER BY discarded_at DESC"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]

def get_file_history(conn, file_path: str) -> list:
    """Get complete history of a file: modifications + discards + restores."""
    mods = [dict(r) for r in conn.execute("""
        SELECT 'modification' as event_type, action, before_snippet, after_snippet,
               session_id, timestamp as event_time
        FROM entity_modifications em
        JOIN project_files pf ON em.file_id = pf.id
        WHERE pf.path = ?
        ORDER BY timestamp DESC LIMIT 50
    """, (file_path,)).fetchall()]

    discards = [dict(r) for r in conn.execute("""
        SELECT 'discard' as event_type, discarded_by, reason, discarded_at as event_time,
               restored, restored_at
        FROM discarded_files
        WHERE original_path = ?
        ORDER BY discarded_at DESC LIMIT 20
    """, (file_path,)).fetchall()]

    # Merge and sort by time
    all_events = mods + discards
    all_events.sort(key=lambda x: x.get('event_time', ''), reverse=True)
    return all_events


# ═══════════════════════════════════════════════════════════════════
# GUID-Tagged Code Sections
# ═══════════════════════════════════════════════════════════════════

import uuid


def create_code_section(conn, description, section_type, file_path='',
                         language='', line_start=None, line_end=None,
                         task_id=None, subtask_id=None, sprint_id=None,
                         created_by='', replaces=None, guid=None,
                         persistent_id=None):
    """Create a GUID-tagged code section record. Auto-generates GUID if not provided.

    persistent_id: Stable identity that survives renames. If replacing another section,
    inherits the old section's persistent_id automatically (same logical entity).
    If this is a brand-new entity, auto-generates a persistent_id."""
    if not guid:
        guid = str(uuid.uuid4())

    revision = 1
    if replaces:
        old = conn.execute("SELECT id, revision, persistent_id FROM code_sections WHERE guid = ?",
                            (replaces,)).fetchone()
        if old:
            revision = old['revision'] + 1
            # Inherit persistent_id from the section we're replacing
            if not persistent_id and old['persistent_id']:
                persistent_id = old['persistent_id']
            conn.execute("UPDATE code_sections SET replaced_by = ? WHERE guid = ?", (guid, replaces))

    # Auto-generate persistent_id for new entities
    if not persistent_id:
        persistent_id = str(uuid.uuid4())

    conn.execute("""
        INSERT INTO code_sections (guid, persistent_id, description, section_type, file_path,
                                    language, line_start, line_end, replaces, revision,
                                    task_id, subtask_id, sprint_id, created_by)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (guid, persistent_id, description, section_type, file_path, language,
          line_start, line_end, replaces, revision,
          task_id, subtask_id, sprint_id, created_by))
    conn.commit()
    return {'guid': guid, 'revision': revision}


def get_code_section(conn, guid):
    row = conn.execute("SELECT * FROM code_sections WHERE guid = ?", (guid,)).fetchone()
    return dict(row) if row else None


def get_current_sections_for_file(conn, file_path, section_type=None):
    sql = "SELECT * FROM code_sections WHERE file_path = ? AND replaced_by IS NULL"
    params = [file_path]
    if section_type:
        sql += " AND section_type = ?"
        params.append(section_type)
    return [dict(r) for r in conn.execute(sql + " ORDER BY line_start", params).fetchall()]


def replace_code_section(conn, old_guid, new_description, created_by='',
                          task_id=None, file_path=None, line_start=None, line_end=None):
    new_guid = str(uuid.uuid4())
    old = get_code_section(conn, old_guid)
    if not old:
        return None
    return create_code_section(
        conn, new_description, old['section_type'],
        file_path=file_path or old['file_path'],
        language=old['language'],
        line_start=line_start, line_end=line_end,
        task_id=task_id or old['task_id'],
        sprint_id=old['sprint_id'],
        created_by=created_by, replaces=old_guid, guid=new_guid)


def get_revision_chain(conn, guid):
    # Walk backwards to find the root
    current = guid
    root = guid
    visited = set()
    while current:
        if current in visited: break
        visited.add(current)
        row = conn.execute("SELECT replaces FROM code_sections WHERE guid = ?", (current,)).fetchone()
        if row and row['replaces']:
            root = row['replaces']
            current = row['replaces']
        else:
            root = current
            break

    # Walk forward from root
    chain = []
    current = root
    visited.clear()
    while current:
        if current in visited: break
        visited.add(current)
        row = conn.execute("SELECT * FROM code_sections WHERE guid = ?", (current,)).fetchone()
        if row:
            chain.append(dict(row))
            current = row['replaced_by']
        else:
            break
    return chain


def find_latest_guid(conn, guid):
    current = guid
    visited = set()
    while current:
        if current in visited: break
        visited.add(current)
        row = conn.execute("SELECT replaced_by FROM code_sections WHERE guid = ?", (current,)).fetchone()
        if row and row['replaced_by']:
            current = row['replaced_by']
        else:
            return current
    return guid


def search_code_sections(conn, description=None, section_type=None,
                          file_path=None, task_id=None, current_only=True, limit=50):
    sql = "SELECT * FROM code_sections WHERE 1=1"
    params = []
    if current_only:
        sql += " AND replaced_by IS NULL"
    if description:
        sql += " AND description LIKE ?"
        params.append(f"%{description}%")
    if section_type:
        sql += " AND section_type = ?"
        params.append(section_type)
    if file_path:
        sql += " AND file_path LIKE ?"
        params.append(f"%{file_path}%")
    if task_id:
        sql += " AND task_id = ?"
        params.append(task_id)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def update_section_location(conn, guid, line_start, line_end, file_path=None):
    sql = "UPDATE code_sections SET line_start = ?, line_end = ?, last_verified = datetime('now'), location_stale = 0"
    params = [line_start, line_end]
    if file_path:
        sql += ", file_path = ?"
        params.append(file_path)
    sql += " WHERE guid = ?"
    params.append(guid)
    conn.execute(sql, params)
    conn.commit()


def clear_section_location(conn, guid):
    conn.execute("""
        UPDATE code_sections SET line_start = NULL, line_end = NULL,
               location_stale = 1, last_verified = datetime('now')
        WHERE guid = ?
    """, (guid,))
    conn.commit()


# ═══════════════════════════════════════════════════════════════════
# Persistent Identity — stable GUID that survives renames
# ═══════════════════════════════════════════════════════════════════


def find_by_persistent_id(conn, persistent_id: str) -> Optional[dict]:
    """Find the CURRENT (non-replaced) code section by its persistent identity.
    A persistent_id stays the same even when Function_A is renamed to FunctionA."""
    row = conn.execute("""
        SELECT * FROM code_sections
        WHERE persistent_id = ? AND replaced_by IS NULL
        ORDER BY revision DESC LIMIT 1
    """, (persistent_id,)).fetchone()
    return dict(row) if row else None


def get_all_versions_by_persistent_id(conn, persistent_id: str) -> list:
    """Get all revisions of a section by persistent identity, newest first."""
    return [dict(r) for r in conn.execute("""
        SELECT * FROM code_sections WHERE persistent_id = ?
        ORDER BY revision DESC
    """, (persistent_id,)).fetchall()]


# ═══════════════════════════════════════════════════════════════════
# File Locking — exclusive access while editing
# ═══════════════════════════════════════════════════════════════════


def lock_file(conn, file_path: str, agent: str, session_id: str = '',
               task_id: int = None, project_dir=None) -> dict:
    """Lock a file for exclusive editing. Returns {locked, holder, hash}.
    If already locked by another agent, returns locked=False with holder info."""
    import hashlib
    from pathlib import Path

    # Check existing lock
    existing = conn.execute("SELECT * FROM file_locks WHERE file_path = ?",
                             (file_path,)).fetchone()
    if existing:
        if existing['locked_by'] == agent:
            return {'locked': True, 'holder': agent, 'hash': existing['content_hash']}
        return {'locked': False, 'holder': existing['locked_by'],
                'locked_at': existing['locked_at'], 'session': existing['session_id']}

    # Compute content hash
    content_hash = ''
    if project_dir:
        fp = Path(project_dir) / file_path
        if fp.is_file():
            content_hash = hashlib.sha256(fp.read_bytes()).hexdigest()

    conn.execute("""
        INSERT INTO file_locks (file_path, locked_by, session_id, task_id, content_hash)
        VALUES (?, ?, ?, ?, ?)
    """, (file_path, agent, session_id, task_id, content_hash))
    conn.commit()
    return {'locked': True, 'holder': agent, 'hash': content_hash}


def unlock_file(conn, file_path: str, agent: str = '') -> bool:
    """Unlock a file. If agent specified, only unlock if locked by that agent."""
    if agent:
        conn.execute("DELETE FROM file_locks WHERE file_path = ? AND locked_by = ?",
                      (file_path, agent))
    else:
        conn.execute("DELETE FROM file_locks WHERE file_path = ?", (file_path,))
    conn.commit()
    return True


def check_file_lock(conn, file_path: str) -> Optional[dict]:
    """Check if a file is locked. Returns lock info or None."""
    row = conn.execute("SELECT * FROM file_locks WHERE file_path = ?",
                        (file_path,)).fetchone()
    return dict(row) if row else None


def get_all_locks(conn, agent: str = None) -> list:
    """Get all file locks, optionally filtered by agent."""
    sql = "SELECT * FROM file_locks"
    params = []
    if agent:
        sql += " WHERE locked_by = ?"
        params.append(agent)
    sql += " ORDER BY locked_at"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_stale_locks(conn, timeout_minutes: int = 30) -> list:
    """Find locks held longer than timeout (may indicate crashed agents)."""
    return [dict(r) for r in conn.execute("""
        SELECT * FROM file_locks
        WHERE datetime(locked_at, '+' || ? || ' minutes') < datetime('now')
    """, (timeout_minutes,)).fetchall()]


def force_unlock_stale(conn, timeout_minutes: int = 30) -> int:
    """Force-unlock all stale locks. Returns count of locks released."""
    cursor = conn.execute("""
        DELETE FROM file_locks
        WHERE datetime(locked_at, '+' || ? || ' minutes') < datetime('now')
    """, (timeout_minutes,))
    conn.commit()
    return cursor.rowcount


def check_file_freshness(conn, file_path: str, session_id: str,
                           project_dir=None) -> dict:
    """Check if a file has changed since an agent last read it.
    Returns {fresh: bool, old_hash, current_hash}."""
    import hashlib
    from pathlib import Path

    # Get the hash from when this session last read the file
    row = conn.execute("""
        SELECT content_hash FROM file_read_hashes
        WHERE file_path = ? AND session_id = ?
        ORDER BY read_at DESC LIMIT 1
    """, (file_path, session_id)).fetchone()

    if not row:
        return {'fresh': True, 'reason': 'never read in this session'}

    old_hash = row['content_hash']

    # Compute current hash
    current_hash = ''
    if project_dir:
        fp = Path(project_dir) / file_path
        if fp.is_file():
            current_hash = hashlib.sha256(fp.read_bytes()).hexdigest()

    if current_hash == old_hash:
        return {'fresh': True, 'old_hash': old_hash, 'current_hash': current_hash}
    return {'fresh': False, 'old_hash': old_hash, 'current_hash': current_hash,
            'reason': 'file content changed since last read'}


def record_file_read(conn, file_path: str, session_id: str, project_dir=None):
    """Record that an agent read a file (stores hash for freshness checking)."""
    import hashlib
    from pathlib import Path

    content_hash = ''
    if project_dir:
        fp = Path(project_dir) / file_path
        if fp.is_file():
            content_hash = hashlib.sha256(fp.read_bytes()).hexdigest()

    conn.execute("""
        INSERT INTO file_read_hashes (file_path, session_id, content_hash)
        VALUES (?, ?, ?)
    """, (file_path, session_id, content_hash))
    conn.commit()


def build_file_context(conn, file_path: str) -> dict:
    """Build rich context for a file: current sections, revision history, linked tasks."""
    sections = get_current_sections_for_file(conn, file_path)
    lock = check_file_lock(conn, file_path)

    # Get recent modifications
    mods = [dict(r) for r in conn.execute("""
        SELECT * FROM entity_modifications em
        JOIN project_files pf ON em.file_id = pf.id
        WHERE pf.path = ?
        ORDER BY em.timestamp DESC LIMIT 10
    """, (file_path,)).fetchall()]

    # Get linked tasks
    tasks = [dict(r) for r in conn.execute("""
        SELECT t.display_id, t.title, t.status, ef.relationship
        FROM entity_files ef
        JOIN project_files pf ON ef.file_id = pf.id
        JOIN tasks t ON ef.entity_id = t.id AND ef.entity_type = 'task'
        WHERE pf.path = ?
        ORDER BY ef.added_at DESC LIMIT 10
    """, (file_path,)).fetchall()]

    # For each section, get the last 3 revisions
    section_histories = []
    for s in sections:
        chain = get_revision_chain(conn, s['guid'])
        section_histories.append({
            'current': s,
            'revision_count': len(chain),
            'recent_revisions': chain[-3:] if len(chain) > 1 else [],
        })

    return {
        'file_path': file_path,
        'sections': section_histories,
        'lock': dict(lock) if lock else None,
        'recent_modifications': mods,
        'linked_tasks': tasks,
    }


# ═══════════════════════════════════════════════════════════════════
# Prompt Log Queries
# ═══════════════════════════════════════════════════════════════════


def get_prompt_logs(conn, session_id: str = None, task_display_id: str = None,
                      provider_name: str = None, limit: int = 50) -> list:
    """Query stored prompt/response pairs with optional filters."""
    sql = "SELECT * FROM prompt_responses WHERE 1=1"
    params = []
    if session_id:
        sql += " AND session_id = ?"
        params.append(session_id)
    if task_display_id:
        sql += " AND task_display_id = ?"
        params.append(task_display_id)
    if provider_name:
        sql += " AND provider_name = ?"
        params.append(provider_name)
    sql += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_sprint_prompt_logs(conn, sprint_number: int) -> list:
    """Get all prompt/response logs for tasks in a given sprint."""
    return [dict(r) for r in conn.execute("""
        SELECT pr.*, st.task_id
        FROM prompt_responses pr
        JOIN sprint_tasks st ON pr.task_display_id = (
            SELECT display_id FROM tasks WHERE id = st.task_id
        )
        JOIN sprints s ON st.sprint_id = s.id
        WHERE s.sprint_number = ?
        ORDER BY pr.timestamp
    """, (sprint_number,)).fetchall()]


def build_sprint_audit_context(conn, sprint_number: int) -> dict:
    """Build comprehensive audit context for sprint review.
    Includes: all modifications, all prompt exchanges, all task outcomes,
    error patterns, repeated actions, and potential automation opportunities."""

    # Get sprint tasks and their outcomes
    tasks = get_sprint_tasks(conn, sprint_number)

    # Get all action log entries for this sprint's tasks
    task_ids = []
    for t in tasks:
        tid = t.get('task_id', t.get('id'))
        if tid:
            task_ids.append(str(tid))

    actions = []
    if task_ids:
        placeholders = ','.join('?' * len(task_ids))
        actions = [dict(r) for r in conn.execute(f"""
            SELECT * FROM action_log
            WHERE scope = 'task' AND scope_id IN ({placeholders})
            ORDER BY timestamp
        """, task_ids).fetchall()]

    # Get all file modifications
    modifications = []
    if task_ids:
        modifications = [dict(r) for r in conn.execute(f"""
            SELECT em.*, pf.path as file_path
            FROM entity_modifications em
            LEFT JOIN project_files pf ON em.file_id = pf.id
            WHERE em.entity_type = 'task' AND em.entity_id IN ({placeholders})
            ORDER BY em.timestamp
        """, task_ids).fetchall()]

    # Get all prompt/response exchanges
    prompt_count = 0
    total_iterations = 0
    error_count = 0
    try:
        row = conn.execute("""
            SELECT COUNT(*) as cnt, SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) as errors
            FROM prompt_responses
            WHERE session_id LIKE '%sprint%' OR task_display_id IN (
                SELECT display_id FROM tasks WHERE id IN ({})
            )
        """.format(','.join('?' * len(task_ids)) if task_ids else "''"
        ), task_ids or []).fetchone()
        if row:
            prompt_count = row['cnt'] or 0
            error_count = row['errors'] or 0
    except Exception:
        pass

    # Analyze patterns: repeated actions, common errors
    action_counts = {}
    error_patterns = []
    for a in actions:
        atype = a.get('action', '')
        action_counts[atype] = action_counts.get(atype, 0) + 1
        if 'error' in atype.lower() or 'fail' in atype.lower() or 'block' in atype.lower():
            detail = a.get('detail', '{}')
            try:
                d = json.loads(detail) if isinstance(detail, str) else detail
                error_patterns.append({
                    'action': atype,
                    'detail': str(d)[:200],
                    'timestamp': a.get('timestamp', ''),
                })
            except Exception:
                pass

    # Files modified
    files_modified = list(set(m.get('file_path', '') for m in modifications if m.get('file_path')))

    # Sprint ceremonies
    ceremonies = [dict(r) for r in conn.execute("""
        SELECT * FROM sprint_ceremonies
        WHERE sprint_id = (SELECT id FROM sprints WHERE sprint_number = ?)
        ORDER BY created_at
    """, (sprint_number,)).fetchall()]

    return {
        'sprint_number': sprint_number,
        'tasks': tasks,
        'task_count': len(tasks),
        'completed_count': len([t for t in tasks if t.get('status') == 'done']),
        'failed_count': len([t for t in tasks if t.get('status') == 'blocked']),
        'actions': actions,
        'action_counts': action_counts,
        'modifications': modifications,
        'files_modified': files_modified,
        'prompt_count': prompt_count,
        'error_count': error_count,
        'error_patterns': error_patterns,
        'ceremonies': ceremonies,
    }


# ═══════════════════════════════════════════════════════════════════
# Dynamic CRUD — Providers, Agents, Pipeline Steps
# ═══════════════════════════════════════════════════════════════════

def has_any_provider(conn: sqlite3.Connection) -> bool:
    """Return True if at least one provider is configured (used to detect first-run)."""
    return conn.execute("SELECT COUNT(*) FROM providers WHERE enabled = 1").fetchone()[0] > 0


def add_provider(conn: sqlite3.Connection, name: str, provider_type: str = 'api',
                  api_key_env: str = '', base_url: str = '', model_name: str = '',
                  max_tokens: int = 4096, config_json: dict = None) -> dict:
    """Add a new AI provider. Returns {id, name}."""
    cur = conn.execute("""
        INSERT INTO providers (name, provider_type, api_key_env, base_url, model_name, max_tokens, config_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (name, provider_type, api_key_env, base_url, model_name, max_tokens,
          json.dumps(config_json or {})))
    conn.commit()
    return {'id': cur.lastrowid, 'name': name}


def update_provider(conn: sqlite3.Connection, provider_id: int, **fields) -> bool:
    """Update provider fields. Returns True on success."""
    allowed = {'name', 'provider_type', 'api_key_env', 'base_url', 'model_name',
               'max_tokens', 'config_json', 'enabled'}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    if 'config_json' in updates and isinstance(updates['config_json'], dict):
        updates['config_json'] = json.dumps(updates['config_json'])
    set_clause = ', '.join(f'{k} = ?' for k in updates)
    conn.execute(f"UPDATE providers SET {set_clause} WHERE id = ?",
                  [*updates.values(), provider_id])
    conn.commit()
    return True


def remove_provider(conn: sqlite3.Connection, provider_id: int) -> bool:
    """Remove a provider. Returns True if deleted, False if not found."""
    row = conn.execute("SELECT id FROM providers WHERE id = ?", (provider_id,)).fetchone()
    if not row:
        return False
    conn.execute("DELETE FROM providers WHERE id = ?", (provider_id,))
    conn.commit()
    return True


def get_providers(conn: sqlite3.Connection, enabled_only: bool = False) -> list:
    """Get all providers."""
    sql = "SELECT * FROM providers"
    if enabled_only:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY id"
    return [dict(r) for r in conn.execute(sql).fetchall()]


def add_agent_definition(conn: sqlite3.Connection, slug: str, display_name: str,
                          role: str = '', description: str = '',
                          system_prompt: str = '', permissions: dict = None,
                          provider_id: int = None, max_complexity: int = 13) -> dict:
    """Add a new agent definition. Returns {id, slug}."""
    cur = conn.execute("""
        INSERT INTO agent_definitions (slug, display_name, role, description,
                                        system_prompt, permissions, provider_id, max_complexity)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (slug, display_name, role, description, system_prompt,
          json.dumps(permissions or {}), provider_id, max_complexity))
    conn.commit()
    return {'id': cur.lastrowid, 'slug': slug}


def update_agent_definition(conn: sqlite3.Connection, agent_id: int, **fields) -> bool:
    """Update agent definition fields."""
    allowed = {'slug', 'display_name', 'role', 'description', 'enabled',
               'system_prompt', 'permissions', 'provider_id', 'max_complexity', 'min_complexity'}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    if 'permissions' in updates and isinstance(updates['permissions'], dict):
        updates['permissions'] = json.dumps(updates['permissions'])
    set_clause = ', '.join(f'{k} = ?' for k in updates)
    conn.execute(f"UPDATE agent_definitions SET {set_clause} WHERE id = ?",
                  [*updates.values(), agent_id])
    conn.commit()
    return True


def remove_agent_definition(conn: sqlite3.Connection, agent_id: int) -> bool:
    """Remove an agent definition."""
    conn.execute("DELETE FROM agent_definitions WHERE id = ?", (agent_id,))
    conn.commit()
    return True


def get_agent_definitions(conn: sqlite3.Connection, enabled_only: bool = False) -> list:
    """Get all agent definitions."""
    sql = "SELECT * FROM agent_definitions"
    if enabled_only:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY slug"
    return [dict(r) for r in conn.execute(sql).fetchall()]


def add_pipeline_step(conn: sqlite3.Connection, step_order: int, name: str,
                       phase_type: str = 'agent', agent_slug: str = '',
                       prompt: str = '', description: str = '', enabled: bool = True,
                       is_default: bool = False, config_json: dict = None) -> dict:
    """Add a new pipeline step. Returns {id, step_order, name}.
    Shifts existing steps down if inserting in the middle."""
    conn.execute("UPDATE pipeline_steps SET step_order = -(step_order + 1) WHERE step_order >= ?",
                  (step_order,))
    conn.execute("UPDATE pipeline_steps SET step_order = -step_order WHERE step_order < 0")
    cur = conn.execute("""
        INSERT INTO pipeline_steps (step_order, name, phase_type, agent_slug, prompt,
                                     description, enabled, is_default, config_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (step_order, name, phase_type, agent_slug, prompt, description,
          int(enabled), int(is_default), json.dumps(config_json or {})))
    conn.commit()
    return {'id': cur.lastrowid, 'step_order': step_order, 'name': name}


def update_pipeline_step(conn: sqlite3.Connection, step_id: int, **fields) -> bool:
    """Update pipeline step fields."""
    allowed = {'name', 'phase_type', 'agent_slug', 'prompt', 'description', 'enabled',
               'removed', 'config_json'}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    if 'config_json' in updates and isinstance(updates['config_json'], (dict, list)):
        updates['config_json'] = json.dumps(updates['config_json'])
    if 'enabled' in updates:
        updates['enabled'] = int(updates['enabled'])
    set_clause = ', '.join(f'{k} = ?' for k in updates)
    conn.execute(f"UPDATE pipeline_steps SET {set_clause} WHERE id = ?",
                  [*updates.values(), step_id])
    conn.commit()
    return True


def remove_pipeline_step(conn: sqlite3.Connection, step_id: int) -> bool:
    """Remove a pipeline step and re-order remaining steps."""
    row = conn.execute("SELECT step_order FROM pipeline_steps WHERE id = ?", (step_id,)).fetchone()
    if not row:
        return False
    removed_order = row['step_order']
    conn.execute("DELETE FROM pipeline_steps WHERE id = ?", (step_id,))
    # Close the gap: negate first to avoid UNIQUE collision, then fix
    conn.execute("UPDATE pipeline_steps SET step_order = -(step_order - 1) WHERE step_order > ?",
                  (removed_order,))
    conn.execute("UPDATE pipeline_steps SET step_order = -step_order WHERE step_order < 0")
    conn.commit()
    return True


def reorder_pipeline_step(conn: sqlite3.Connection, step_id: int, new_order: int) -> bool:
    """Move a pipeline step to a new position."""
    row = conn.execute("SELECT step_order FROM pipeline_steps WHERE id = ?", (step_id,)).fetchone()
    if not row:
        return False
    old_order = row['step_order']
    if old_order == new_order:
        return True
    # Temporarily set the moving step to a negative order to avoid conflicts
    conn.execute("UPDATE pipeline_steps SET step_order = -9999 WHERE id = ?", (step_id,))
    if new_order > old_order:
        conn.execute("UPDATE pipeline_steps SET step_order = -(step_order - 1) WHERE step_order > ? AND step_order <= ?",
                      (old_order, new_order))
        conn.execute("UPDATE pipeline_steps SET step_order = -step_order WHERE step_order < 0 AND step_order != -9999")
    else:
        conn.execute("UPDATE pipeline_steps SET step_order = -(step_order + 1) WHERE step_order >= ? AND step_order < ?",
                      (new_order, old_order))
        conn.execute("UPDATE pipeline_steps SET step_order = -step_order WHERE step_order < 0 AND step_order != -9999")
    conn.execute("UPDATE pipeline_steps SET step_order = ? WHERE id = ?", (new_order, step_id))
    conn.commit()
    return True


def get_pipeline_steps(conn: sqlite3.Connection, enabled_only: bool = False) -> list:
    """Get all pipeline steps in order."""
    sql = "SELECT * FROM pipeline_steps"
    if enabled_only:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY step_order"
    return [dict(r) for r in conn.execute(sql).fetchall()]


# ═══════════════════════════════════════════════════════════════════
# Convenience Queries
# ═══════════════════════════════════════════════════════════════════


# ── Get By ID ─────────────────────────────────────────────────────

def get_task_by_id(conn, display_id: str) -> Optional[dict]:
    row = conn.execute("SELECT * FROM tasks WHERE display_id = ?", (display_id,)).fetchone()
    return dict(row) if row else None

def get_subtask_by_id(conn, subtask_id: int) -> Optional[dict]:
    row = conn.execute("SELECT * FROM subtasks WHERE id = ?", (subtask_id,)).fetchone()
    return dict(row) if row else None

def get_subtasks_for_task(conn, task_display_id: str) -> list:
    row = conn.execute("SELECT id FROM tasks WHERE display_id = ?", (task_display_id,)).fetchone()
    if not row: return []
    return [dict(r) for r in conn.execute(
        "SELECT * FROM subtasks WHERE task_id = ? ORDER BY sort_order", (row['id'],)).fetchall()]

def get_sprint_by_number(conn, sprint_number: int) -> Optional[dict]:
    row = conn.execute("SELECT * FROM sprints WHERE sprint_number = ?", (sprint_number,)).fetchone()
    return dict(row) if row else None

def get_all_sprints(conn) -> list:
    return [dict(r) for r in conn.execute("SELECT * FROM sprints ORDER BY sprint_number DESC").fetchall()]

def get_active_sprint(conn) -> Optional[dict]:
    row = conn.execute("SELECT * FROM sprints WHERE status = 'active' ORDER BY sprint_number DESC LIMIT 1").fetchone()
    return dict(row) if row else None

def get_project_task(conn, display_id: str = None) -> Optional[dict]:
    if display_id:
        row = conn.execute("SELECT * FROM project_tasks WHERE display_id = ?", (display_id,)).fetchone()
    else:
        row = conn.execute("SELECT * FROM project_tasks WHERE status = 'active' ORDER BY id LIMIT 1").fetchone()
    return dict(row) if row else None

def list_project_tasks(conn) -> list:
    return [dict(r) for r in conn.execute("SELECT * FROM project_tasks ORDER BY id").fetchall()]

def get_tasks_by_agent(conn, agent_slug: str, limit: int = 50) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM tasks WHERE assigned_agent = ? OR agent_slug = ? ORDER BY sort_order LIMIT ?",
        (agent_slug, agent_slug, limit)).fetchall()]

def get_tasks_by_complexity(conn, complexity: int, limit: int = 50) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM tasks WHERE complexity = ? ORDER BY priority, sort_order LIMIT ?",
        (complexity, limit)).fetchall()]

def get_blocked_tasks(conn) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM tasks WHERE status = 'blocked' ORDER BY sort_order").fetchall()]

def get_dependency_chain(conn, task_display_id: str) -> list:
    """Get the dependency chain for a task (what it depends on, recursively)."""
    chain = []
    current = conn.execute("SELECT * FROM tasks WHERE display_id = ?", (task_display_id,)).fetchone()
    while current and current['depends_on_task_id']:
        dep = conn.execute("SELECT * FROM tasks WHERE id = ?", (current['depends_on_task_id'],)).fetchone()
        if dep:
            chain.append(dict(dep))
            current = dep
        else:
            break
    return chain

def get_task_children(conn, task_display_id: str) -> list:
    row = conn.execute("SELECT id FROM tasks WHERE display_id = ?", (task_display_id,)).fetchone()
    if not row: return []
    return [dict(r) for r in conn.execute(
        "SELECT * FROM tasks WHERE parent_task_id = ? ORDER BY sort_order", (row['id'],)).fetchall()]


def get_tasks_by_status(conn: sqlite3.Connection, status: str, limit: int = 50) -> list:
    """Get tasks filtered by status."""
    return [dict(r) for r in conn.execute(
        "SELECT * FROM tasks WHERE status = ? ORDER BY sort_order, id LIMIT ?",
        (status, limit)
    ).fetchall()]


def search_tasks(conn: sqlite3.Connection, query: str, limit: int = 50) -> list:
    """G-11: Full-text search tasks using FTS5 virtual table."""
    try:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE id IN (SELECT rowid FROM tasks_fts WHERE tasks_fts MATCH ?) LIMIT ?",
            (query, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        # Fallback: LIKE search if FTS5 not available
        pattern = f'%{query}%'
        rows = conn.execute(
            "SELECT * FROM tasks WHERE title LIKE ? OR description LIKE ? OR display_id LIKE ? LIMIT ?",
            (pattern, pattern, pattern, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def get_tasks(conn: sqlite3.Connection, limit: int = 200, offset: int = 0) -> list:
    """Get all tasks with optional pagination (G-09)."""
    return [dict(r) for r in conn.execute(
        "SELECT * FROM tasks ORDER BY sort_order, id LIMIT ? OFFSET ?",
        (limit, offset)
    ).fetchall()]


def get_backlog(conn: sqlite3.Connection, limit: int = 50) -> list:
    """Get all backlog tasks ordered by priority then sort_order.

    WF-3: skip tasks under a manual_claim_until window — humans / parallel
    agents have first dibs for the claim period.
    """
    priority_order = "CASE priority WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1 WHEN 'MEDIUM' THEN 2 WHEN 'LOW' THEN 3 END"
    return [dict(r) for r in conn.execute(
        f"SELECT * FROM tasks WHERE status = 'backlog' "
        f"  AND (manual_claim_until IS NULL OR datetime(manual_claim_until) <= datetime('now')) "
        f"ORDER BY {priority_order}, sort_order, id LIMIT ?",
        (limit,)
    ).fetchall()]


def get_sprint_tasks(conn: sqlite3.Connection, sprint_number: int) -> list:
    """Get all tasks for a given sprint number."""
    return [dict(r) for r in conn.execute("""
        SELECT st.*, t.display_id AS task_display_id, t.title, t.complexity,
               t.description, t.acceptance_criteria
        FROM sprint_tasks st
        JOIN tasks t ON st.task_id = t.id
        JOIN sprints s ON st.sprint_id = s.id
        WHERE s.sprint_number = ?
        ORDER BY st.status, t.sort_order
    """, (sprint_number,)).fetchall()]


def get_provider_for_complexity(conn: sqlite3.Connection, complexity: int,
                                  project_name: str = '*') -> Optional[dict]:
    """Find the preferred provider for a given complexity level.
    Uses complexity_routing table (configurable per-project), falls back to
    performance-based selection, then to any enabled provider."""

    # Step 1: Check explicit routing config (project-specific first, then global)
    for pname in ([project_name, '*'] if project_name != '*' else ['*']):
        row = conn.execute("""
            SELECT p.* FROM complexity_routing cr
            JOIN providers p ON cr.provider_id = p.id
            WHERE cr.project_name = ? AND cr.enabled = 1 AND p.enabled = 1
              AND ? >= cr.min_complexity AND ? <= cr.max_complexity
            ORDER BY cr.priority ASC
            LIMIT 1
        """, (pname, complexity, complexity)).fetchone()
        if row:
            return dict(row)

    # Step 2: Fall back to performance-based selection
    row = conn.execute("""
        SELECT p.*, pp.successes * 1.0 / MAX(pp.attempts, 1) AS success_rate
        FROM providers p
        JOIN provider_performance pp ON pp.provider_id = p.id
        WHERE pp.complexity = ? AND p.enabled = 1 AND pp.attempts >= 3
          AND (pp.successes * 1.0 / pp.attempts) > 0.5
        ORDER BY success_rate DESC
        LIMIT 1
    """, (complexity,)).fetchone()
    if row:
        return dict(row)

    # Step 3: Fall back to first enabled provider
    row = conn.execute("SELECT * FROM providers WHERE enabled = 1 ORDER BY id LIMIT 1").fetchone()
    return dict(row) if row else None


def set_complexity_routing(conn: sqlite3.Connection, project_name: str,
                            min_complexity: int, max_complexity: int,
                            provider_id: int, priority: int = 0) -> bool:
    """Set preferred provider for a complexity range in a project."""
    conn.execute("""
        INSERT INTO complexity_routing (project_name, min_complexity, max_complexity, provider_id, priority)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(project_name, provider_id, min_complexity, max_complexity)
        DO UPDATE SET priority = ?, enabled = 1
    """, (project_name, min_complexity, max_complexity, provider_id, priority, priority))
    conn.commit()
    return True


def get_complexity_routing(conn: sqlite3.Connection, project_name: str = '*') -> list:
    """Get all complexity routing rules for a project."""
    return [dict(r) for r in conn.execute("""
        SELECT cr.*, p.name as provider_name
        FROM complexity_routing cr
        JOIN providers p ON cr.provider_id = p.id
        WHERE cr.project_name IN (?, '*')
        ORDER BY cr.min_complexity, cr.priority
    """, (project_name,)).fetchall()]


def remove_complexity_routing(conn: sqlite3.Connection, routing_id: int) -> bool:
    conn.execute("DELETE FROM complexity_routing WHERE id = ?", (routing_id,))
    conn.commit()
    return True


def setup_complexity_routing_by_name(conn: sqlite3.Connection, 
                                      routing_config: list,
                                      project_name: str = '*') -> dict:
    """Set up complexity routing using provider names.
    
    Args:
        conn: SQLite connection to global DB
        routing_config: List of dicts with keys:
            - min_complexity: int
            - max_complexity: int
            - provider_name: str (provider name to look up)
            - priority: int (default 0, lower = preferred)
        project_name: Project name ('*' for global default)
        
    Returns:
        dict with 'success', 'configured', and 'skipped' lists
        
    Example:
        setup_complexity_routing_by_name(conn, [
            {'min_complexity': 1, 'max_complexity': 3, 'provider_name': 'Gemma3-4b'},
            {'min_complexity': 4, 'max_complexity': 8, 'provider_name': 'Gemma3-12b'},
            {'min_complexity': 9, 'max_complexity': 89, 'provider_name': 'Phi4'},
        ])
    """
    # Get all providers
    providers = {}
    for row in conn.execute("SELECT id, name, enabled FROM providers").fetchall():
        providers[row['name']] = {'id': row['id'], 'enabled': row['enabled']}
    
    configured = []
    skipped = []
    
    for config in routing_config:
        min_c = config['min_complexity']
        max_c = config['max_complexity']
        provider_name = config['provider_name']
        priority = config.get('priority', 0)
        
        if provider_name not in providers:
            skipped.append(f"Provider '{provider_name}' not found")
            continue
        
        if not providers[provider_name]['enabled']:
            skipped.append(f"Provider '{provider_name}' is disabled")
            continue
        
        provider_id = providers[provider_name]['id']
        
        conn.execute("""
            INSERT OR REPLACE INTO complexity_routing 
            (project_name, min_complexity, max_complexity, provider_id, priority, enabled)
            VALUES (?, ?, ?, ?, ?, 1)
        """, (project_name, min_c, max_c, provider_id, priority))
        
        configured.append(f"complexity {min_c}-{max_c} → {provider_name} (priority {priority})")
    
    conn.commit()
    
    return {
        'success': True,
        'configured': configured,
        'skipped': skipped
    }


def seed_default_complexity_routing(conn: sqlite3.Connection):
    """Seed default complexity routing based on task complexity:
    - complexity <= 3 → gemma3:4b (fast)
    - complexity 4-8 → gemma3:12b (better)
    - complexity > 8 → phi4 or claude (if configured)
    """
    # Get all providers with their IDs and names
    providers = {}
    for row in conn.execute("SELECT id, name, model_name, enabled FROM providers").fetchall():
        providers[row['name']] = {'id': row['id'], 'enabled': row['enabled']}
    
    project_name = '*'  # global default
    
    # Route 1: complexity <= 3 → gemma3:4b (fast)
    for name in ['Gemma3-4b', 'gemma3:4b', 'fast']:
        if name in providers and providers[name]['enabled']:
            conn.execute("""
                INSERT OR IGNORE INTO complexity_routing 
                (project_name, min_complexity, max_complexity, provider_id, priority, enabled)
                VALUES (?, 1, 3, ?, 0, 1)
            """, (project_name, providers[name]['id']))
            break
    
    # Route 2: complexity 4-8 → gemma3:12b (better)
    for name in ['Gemma3-12b', 'gemma3:12b', 'medium']:
        if name in providers and providers[name]['enabled']:
            conn.execute("""
                INSERT OR IGNORE INTO complexity_routing 
                (project_name, min_complexity, max_complexity, provider_id, priority, enabled)
                VALUES (?, 4, 8, ?, 0, 1)
            """, (project_name, providers[name]['id']))
            break
    
    # Route 3: complexity > 8 → phi4 (priority 0)
    for name in ['Phi4', 'phi4', 'powerful']:
        if name in providers and providers[name]['enabled']:
            conn.execute("""
                INSERT OR IGNORE INTO complexity_routing 
                (project_name, min_complexity, max_complexity, provider_id, priority, enabled)
                VALUES (?, 9, 89, ?, 0, 1)
            """, (project_name, providers[name]['id']))
            break
    
    # Route 4: complexity > 8 → claude (priority 1, fallback)
    for name in ['Claude', 'claude', 'claude-sonnet']:
        if name in providers and providers[name]['enabled']:
            conn.execute("""
                INSERT OR IGNORE INTO complexity_routing 
                (project_name, min_complexity, max_complexity, provider_id, priority, enabled)
                VALUES (?, 9, 89, ?, 1, 1)
            """, (project_name, providers[name]['id']))
            break
    
    conn.commit()


FIBONACCI = [1, 2, 3, 5, 8, 13, 21, 34, 55, 89]


def bump_complexity(conn: sqlite3.Connection, task_display_id: str,
                     bumped_by: str = '', reason: str = '') -> Optional[int]:
    """Bump a task's complexity to the next Fibonacci number.
    Returns the new complexity, or None if already at max (89)."""
    row = conn.execute("SELECT id, complexity FROM tasks WHERE display_id = ?",
                        (task_display_id,)).fetchone()
    if not row:
        return None
    old = row['complexity']
    # Find next Fibonacci
    new = None
    for f in FIBONACCI:
        if f > old:
            new = f
            break
    if new is None:
        return None  # Already at 89

    conn.execute("UPDATE tasks SET complexity = ? WHERE display_id = ?",
                  (new, task_display_id))
    conn.commit()
    log_action(conn, 'task', row['id'], bumped_by, 'complexity_bumped',
               {'old_complexity': old, 'new_complexity': new, 'reason': reason})
    return new


# ── Per-task failure tracking (retry cap) ─────────────────────────

MAX_TASK_RETRIES = 5
RETRY_BACKOFF_BASE_S = 60  # 60s, 120s, 240s, 480s, 960s


def get_task_failure_count(conn: sqlite3.Connection, task_display_id: str) -> int:
    """Return the cumulative failure_count for a task (0 if not found)."""
    row = conn.execute(
        'SELECT failure_count FROM tasks WHERE display_id = ?',
        (task_display_id,)
    ).fetchone()
    if not row:
        return 0
    return row['failure_count'] if isinstance(row, dict) else row[0]


def increment_task_failure_count(
    conn: sqlite3.Connection,
    task_display_id: str,
    reason: str = '',
) -> int:
    """Increment failure_count and set last_failure_at. Returns new count."""
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    conn.execute(
        'UPDATE tasks SET failure_count = failure_count + 1, '
        'last_failure_at = ? WHERE display_id = ?',
        (now, task_display_id),
    )
    conn.commit()
    return get_task_failure_count(conn, task_display_id)


def reset_task_failure_count(conn: sqlite3.Connection, task_display_id: str):
    """Reset failure_count to 0 (e.g. after manual unblock)."""
    conn.execute(
        'UPDATE tasks SET failure_count = 0, last_failure_at = NULL WHERE display_id = ?',
        (task_display_id,),
    )
    conn.commit()


def get_retry_backoff_seconds(failure_count: int) -> float:
    """Exponential backoff: 60s * 2^(failure_count-1).

    failure_count=1 → 60s, 2 → 120s, 3 → 240s, 4 → 480s, 5 → 960s.
    """
    if failure_count <= 0:
        return 0
    return RETRY_BACKOFF_BASE_S * (2 ** (failure_count - 1))


def record_usage_log(
    conn: sqlite3.Connection,
    provider_id: int,
    agent_slug: str = '',
    duration_ms: int = 0,
    success: bool = True,
    error_message: str = '',
    session_id: str = '',
    request_tokens: int = 0,
    response_tokens: int = 0,
):
    """Log a single agent execution to usage_log for per-agent performance analysis."""
    try:
        conn.execute("""
            INSERT INTO usage_log
                (provider_id, agent_slug, request_tokens, response_tokens, cost_usd,
                 duration_ms, success, error_message, session_id, timestamp)
            VALUES (?, ?, ?, ?, 0.0, ?, ?, ?, ?, datetime('now'))
        """, (
            provider_id, agent_slug, request_tokens, response_tokens,
            duration_ms, 1 if success else 0,
            (error_message or '')[:500], session_id or '',
        ))
        conn.commit()
    except Exception:
        pass  # usage logging is best-effort; never break the main flow


def log_provider_health(
    gconn: sqlite3.Connection,
    provider_name: str,
    latency_ms: int,
    status: str,
) -> None:
    """S3-W07: Log a provider health check result to provider_health_log."""
    try:
        gconn.execute(
            "INSERT INTO provider_health_log (provider_name, latency_ms, status) VALUES (?, ?, ?)",
            (provider_name, latency_ms, status),
        )
        gconn.commit()
    except Exception:
        pass  # health logging is best-effort


def record_provider_attempt(
    conn: sqlite3.Connection,
    provider_id: int,
    complexity: int,
    success: bool,
    duration_ms: int = 0,
):
    """Record a provider attempt for performance tracking."""
    conn.execute("""
        INSERT INTO provider_performance (provider_id, complexity, attempts, successes, failures,
                                          avg_duration, last_attempt)
        VALUES (?, ?, 1, ?, ?, ?, datetime('now'))
        ON CONFLICT(provider_id, complexity) DO UPDATE SET
            attempts = attempts + 1,
            successes = successes + ?,
            failures = failures + ?,
            avg_duration = (avg_duration * attempts + ?) / (attempts + 1),
            last_attempt = datetime('now')
    """, (
        provider_id, complexity,
        1 if success else 0,
        0 if success else 1,
        duration_ms,
        1 if success else 0,
        0 if success else 1,
        duration_ms,
    ))
    conn.commit()


def create_sprint(
    conn: sqlite3.Connection,
    sprint_number: int,
    name: str = "",
    goal: str = "",
) -> dict:
    """Create a new sprint. Returns {id, sprint_number}."""
    cur = conn.execute("""
        INSERT INTO sprints (sprint_number, name, goal)
        VALUES (?, ?, ?)
    """, (sprint_number, name, goal))
    conn.commit()
    return {"id": cur.lastrowid, "sprint_number": sprint_number}


def assign_task_to_sprint(
    conn: sqlite3.Connection,
    task_display_id: str,
    sprint_number: int,
    story_points: Optional[int] = None,
) -> dict:
    """Assign a task to a sprint. Returns {id, display_id}."""
    task_row = conn.execute(
        "SELECT id, complexity FROM tasks WHERE display_id = ?", (task_display_id,)
    ).fetchone()
    if not task_row:
        return {"error": f"Task {task_display_id} not found"}

    sprint_row = conn.execute(
        "SELECT id FROM sprints WHERE sprint_number = ?", (sprint_number,)
    ).fetchone()
    if not sprint_row:
        return {"error": f"Sprint {sprint_number} not found"}

    points = story_points if story_points is not None else task_row["complexity"]
    cur = conn.execute("""
        INSERT INTO sprint_tasks (display_id, sprint_id, task_id, story_points)
        VALUES ('', ?, ?, ?)
    """, (sprint_row["id"], task_row["id"], points))

    # Update task status
    conn.execute(
        "UPDATE tasks SET status = 'sprint_planned' WHERE id = ? AND status = 'backlog'",
        (task_row["id"],)
    )
    conn.commit()

    st_id = cur.lastrowid
    row = conn.execute(
        "SELECT id, display_id FROM sprint_tasks WHERE id = ?", (st_id,)
    ).fetchone()
    return dict(row)


def create_project_task(
    conn: sqlite3.Connection,
    title: str,
    goal: str = "",
    description: str = "",
    priority: str = "MEDIUM",
    empty_backlog_prompt: str = "",
    empty_backlog_agent: str = "scrum-master",
) -> dict:
    """Create a top-level project task. Returns {id, display_id}."""
    cur = conn.execute("""
        INSERT INTO project_tasks (display_id, title, goal, description, priority,
                                    empty_backlog_prompt, empty_backlog_agent)
        VALUES ('', ?, ?, ?, ?, ?, ?)
    """, (title, goal, description, priority, empty_backlog_prompt, empty_backlog_agent))
    conn.commit()
    pt_id = cur.lastrowid
    row = conn.execute(
        "SELECT id, display_id FROM project_tasks WHERE id = ?", (pt_id,)
    ).fetchone()
    return dict(row)


def log_prompt_response(
    conn: sqlite3.Connection,
    session_id: str,
    agent_slug: str = "",
    provider_name: str = "",
    task_display_id: str = "",
    prompt_json: str = "{}",
    response_json: str = "{}",
    tokens_in: int = 0,
    tokens_out: int = 0,
    duration_ms: int = 0,
    success: bool = True,
    error_message: str = "",
    model_name: str = "",
    action: str = "",
) -> int:
    """Log a prompt/response exchange. Returns row id."""
    cur = conn.execute("""
        INSERT INTO prompt_responses
            (session_id, agent_slug, provider_name, model_name, task_display_id,
             prompt_json, response_json, action, tokens_in, tokens_out,
             duration_ms, success, error_message)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (session_id, agent_slug, provider_name, model_name, task_display_id,
          prompt_json, response_json, action, tokens_in, tokens_out,
          duration_ms, 1 if success else 0, error_message))
    conn.commit()
    return cur.lastrowid


def log_vcs_commit(
    conn: sqlite3.Connection,
    commit_hash: str,
    entity_type: Optional[str] = None,
    entity_id: Optional[int] = None,
    message: str = "",
    author: str = "",
    branch: str = "",
    files_changed: int = 0,
    insertions: int = 0,
    deletions: int = 0,
    session_id: str = "",
) -> int:
    """Record a VCS commit. Returns row id."""
    cur = conn.execute("""
        INSERT INTO vcs_commits
            (commit_hash, entity_type, entity_id, message, author, branch,
             files_changed, insertions, deletions, session_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (commit_hash, entity_type, entity_id, message, author, branch,
          files_changed, insertions, deletions, session_id))
    conn.commit()
    return cur.lastrowid


# ═══════════════════════════════════════════════════════════════════
# Global Settings Helpers
# ═══════════════════════════════════════════════════════════════════


def get_global_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    """Return a value from global_settings, or default if missing."""
    row = conn.execute(
        "SELECT value FROM global_settings WHERE key = ?", (key,)
    ).fetchone()
    return row["value"] if row else default


def set_global_setting(conn: sqlite3.Connection, key: str, value: str):
    """Upsert a key/value pair in global_settings."""
    conn.execute(
        "INSERT INTO global_settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value)
    )
    conn.commit()


# ═══════════════════════════════════════════════════════════════════
# Project Settings Helpers
# ═══════════════════════════════════════════════════════════════════


def get_project_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    """Return a value from project_settings, or default if missing."""
    row = conn.execute(
        "SELECT value FROM project_settings WHERE key = ?", (key,)
    ).fetchone()
    return row["value"] if row else default


def set_project_setting(conn: sqlite3.Connection, key: str, value: str):
    """Upsert a key/value pair in project_settings."""
    conn.execute(
        "INSERT INTO project_settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value)
    )
    conn.commit()


def get_project_setting_json(conn: sqlite3.Connection, key: str, default=None):
    """Return a parsed JSON value from project_settings, or default if missing/invalid."""
    import json as _json
    raw = get_project_setting(conn, key, "")
    if not raw:
        return default
    try:
        return _json.loads(raw)
    except Exception:
        return default


def set_project_setting_json(conn: sqlite3.Connection, key: str, obj):
    """Store obj serialized as JSON in project_settings."""
    import json as _json
    set_project_setting(conn, key, _json.dumps(obj, indent=2))


def seed_project_settings_from_files(conn: sqlite3.Connection, project_dir):
    """
    One-time migration: read agents-config.json and pipeline-config.json from
    the project directory and store them in project_settings if not already present.
    Silently skips missing files or already-seeded keys.
    """
    import json as _json
    from pathlib import Path as _Path
    project_dir = _Path(project_dir)
    for filename, key in [
        ("agents-config.json", "agents_config"),
        ("pipeline-config.json", "pipeline_config"),
        ("settings.json", "project_settings_json"),
    ]:
        fpath = project_dir / filename
        if not fpath.exists():
            continue
        existing = get_project_setting(conn, key, "")
        if existing:
            continue  # already seeded
        try:
            raw = fpath.read_text(encoding="utf-8")
            _json.loads(raw)  # validate JSON
            set_project_setting(conn, key, raw)
        except Exception:
            pass  # skip malformed files


# ═══════════════════════════════════════════════════════════════════
# Known Projects Helpers
# ═══════════════════════════════════════════════════════════════════


def register_project(
    conn: sqlite3.Connection,
    name: str,
    directory: str,
    goal: str = "",
    empty_backlog_prompt: str = "",
    empty_backlog_agent: str = "scrum-master",
    empty_sprint_prompt: str = "",
    create_sprint_prompt: str = "",
    config_id: Optional[int] = None,
) -> int:
    """Insert or update a project in known_projects. Returns the row id."""
    conn.execute(
        """INSERT INTO known_projects
               (name, directory, goal, empty_backlog_prompt, empty_backlog_agent,
                empty_sprint_prompt, create_sprint_prompt, config_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(directory) DO UPDATE SET
               name                 = excluded.name,
               goal                 = excluded.goal,
               empty_backlog_prompt = excluded.empty_backlog_prompt,
               empty_backlog_agent  = excluded.empty_backlog_agent,
               empty_sprint_prompt  = excluded.empty_sprint_prompt,
               create_sprint_prompt = excluded.create_sprint_prompt,
               config_id            = excluded.config_id
        """,
        (name, directory, goal, empty_backlog_prompt, empty_backlog_agent,
         empty_sprint_prompt, create_sprint_prompt, config_id),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM known_projects WHERE directory = ?", (directory,)
    ).fetchone()
    return row["id"]


def get_known_projects(conn: sqlite3.Connection) -> List[Dict]:
    """Return all known projects ordered by last_opened DESC, then sort_order."""
    rows = conn.execute(
        """SELECT kp.*, c.name AS config_name
           FROM known_projects kp
           LEFT JOIN configs c ON c.id = kp.config_id
           ORDER BY
               CASE WHEN kp.last_opened IS NULL THEN 1 ELSE 0 END,
               kp.last_opened DESC,
               kp.sort_order
        """
    ).fetchall()
    return [dict(r) for r in rows]


def update_project_last_opened(conn: sqlite3.Connection, directory: str):
    """Stamp last_opened = now() for the given project directory."""
    conn.execute(
        "UPDATE known_projects SET last_opened = datetime('now') WHERE directory = ?",
        (directory,),
    )
    conn.commit()


def update_known_project(conn: sqlite3.Connection, directory: str, **fields) -> bool:
    """Update arbitrary fields on a known_projects row. Returns True if found."""
    allowed = {
        "name", "goal", "empty_backlog_prompt", "empty_backlog_agent",
        "empty_sprint_prompt", "create_sprint_prompt", "config_id", "sort_order",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    conn.execute(
        f"UPDATE known_projects SET {set_clause} WHERE directory = ?",
        list(updates.values()) + [directory],
    )
    conn.commit()
    return conn.execute(
        "SELECT changes()"
    ).fetchone()[0] > 0


# ═══════════════════════════════════════════════════════════════════
# Config Helpers
# ═══════════════════════════════════════════════════════════════════


def create_config(
    conn: sqlite3.Connection,
    name: str,
    description: str = "",
    parent_config_id: Optional[int] = None,
) -> int:
    """Insert a new config. Returns the new row id."""
    cur = conn.execute(
        "INSERT INTO configs (name, description, parent_config_id) VALUES (?, ?, ?)",
        (name, description, parent_config_id),
    )
    conn.commit()
    return cur.lastrowid


def get_configs(conn: sqlite3.Connection) -> List[Dict]:
    """Return all configs ordered by name."""
    rows = conn.execute(
        """SELECT c.*, p.name AS parent_name
           FROM configs c
           LEFT JOIN configs p ON p.id = c.parent_config_id
           ORDER BY c.name
        """
    ).fetchall()
    return [dict(r) for r in rows]


def set_config_override(
    conn: sqlite3.Connection,
    config_id: int,
    scope: str,
    key: str,
    value: str,
):
    """Upsert a config override key/value."""
    conn.execute(
        """INSERT INTO config_overrides (config_id, scope, key, value)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(config_id, scope, key) DO UPDATE SET value = excluded.value
        """,
        (config_id, scope, key, value),
    )
    conn.commit()


def delete_config_override(
    conn: sqlite3.Connection,
    config_id: int,
    scope: str,
    key: str,
):
    """Remove a specific override (falls back to inherited value)."""
    conn.execute(
        "DELETE FROM config_overrides WHERE config_id = ? AND scope = ? AND key = ?",
        (config_id, scope, key),
    )
    conn.commit()


def get_config_overrides(conn: sqlite3.Connection, config_id: int) -> Dict:
    """Return raw overrides for this config only, as {scope: {key: value}}."""
    rows = conn.execute(
        "SELECT scope, key, value FROM config_overrides WHERE config_id = ?",
        (config_id,),
    ).fetchall()
    result: Dict[str, Dict[str, str]] = {}
    for r in rows:
        result.setdefault(r["scope"], {})[r["key"]] = r["value"]
    return result


def get_resolved_config(conn: sqlite3.Connection, config_id: Optional[int]) -> Dict:
    """Walk the parent chain and merge overrides (parent first, child wins).

    Returns {scope: {key: value}} representing the fully resolved config.
    Starts from Globals (empty dict) → parent chain → this config.
    Detects and stops at circular references.
    """
    if config_id is None:
        return {}

    # Walk parent chain to root
    chain: List[int] = []
    seen: set = set()
    current_id: Optional[int] = config_id
    while current_id is not None and current_id not in seen:
        seen.add(current_id)
        chain.append(current_id)
        row = conn.execute(
            "SELECT parent_config_id FROM configs WHERE id = ?", (current_id,)
        ).fetchone()
        if not row:
            break
        current_id = row["parent_config_id"]

    # Merge from root down (child overrides parent)
    merged: Dict[str, Dict[str, str]] = {}
    for cid in reversed(chain):
        overrides = get_config_overrides(conn, cid)
        for scope, kvs in overrides.items():
            merged.setdefault(scope, {}).update(kvs)
    return merged


def validate_config_parent(
    conn: sqlite3.Connection,
    config_id: int,
    proposed_parent_id: int,
) -> bool:
    """Return True if setting proposed_parent_id would NOT create a cycle."""
    if config_id == proposed_parent_id:
        return False
    seen: set = {config_id}
    current: Optional[int] = proposed_parent_id
    while current is not None:
        if current in seen:
            return False
        seen.add(current)
        row = conn.execute(
            "SELECT parent_config_id FROM configs WHERE id = ?", (current,)
        ).fetchone()
        if not row:
            break
        current = row["parent_config_id"]
    return True


# ═══════════════════════════════════════════════════════════════════
# Manager Helpers
# ═══════════════════════════════════════════════════════════════════


def create_manager(
    conn: sqlite3.Connection,
    name: str,
    goal: str = "",
    config_id: Optional[int] = None,
) -> int:
    """Insert a new manager. Returns the new row id."""
    cur = conn.execute(
        "INSERT INTO managers (name, goal, config_id) VALUES (?, ?, ?)",
        (name, goal, config_id),
    )
    conn.commit()
    return cur.lastrowid


def get_managers(conn: sqlite3.Connection) -> List[Dict]:
    """Return all managers with config name, ordered by name."""
    rows = conn.execute(
        """SELECT m.*, c.name AS config_name
           FROM managers m
           LEFT JOIN configs c ON c.id = m.config_id
           ORDER BY m.name
        """
    ).fetchall()
    return [dict(r) for r in rows]


def update_manager(conn: sqlite3.Connection, manager_id: int, **fields) -> bool:
    """Update arbitrary fields on a managers row."""
    allowed = {"name", "goal", "config_id"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    conn.execute(
        f"UPDATE managers SET {set_clause} WHERE id = ?",
        list(updates.values()) + [manager_id],
    )
    conn.commit()
    return True


def delete_manager(conn: sqlite3.Connection, manager_id: int):
    """Delete a manager (cascades to manager_projects and management_tasks)."""
    conn.execute("DELETE FROM managers WHERE id = ?", (manager_id,))
    conn.commit()


def get_manager_projects(conn: sqlite3.Connection, manager_id: int) -> List[str]:
    """Return list of project directories managed by this manager."""
    rows = conn.execute(
        "SELECT project_dir FROM manager_projects WHERE manager_id = ? ORDER BY project_dir",
        (manager_id,),
    ).fetchall()
    return [r["project_dir"] for r in rows]


def add_manager_project(conn: sqlite3.Connection, manager_id: int, project_dir: str):
    """Link a project to a manager (ignore if already linked)."""
    conn.execute(
        "INSERT OR IGNORE INTO manager_projects (manager_id, project_dir) VALUES (?, ?)",
        (manager_id, project_dir),
    )
    conn.commit()


def remove_manager_project(conn: sqlite3.Connection, manager_id: int, project_dir: str):
    """Unlink a project from a manager."""
    conn.execute(
        "DELETE FROM manager_projects WHERE manager_id = ? AND project_dir = ?",
        (manager_id, project_dir),
    )
    conn.commit()


def get_management_tasks(
    conn: sqlite3.Connection,
    manager_id: int,
    status: Optional[str] = None,
) -> List[Dict]:
    """Return management tasks for a manager, optionally filtered by status."""
    if status:
        rows = conn.execute(
            "SELECT * FROM management_tasks WHERE manager_id = ? AND status = ? ORDER BY created_at",
            (manager_id, status),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM management_tasks WHERE manager_id = ? ORDER BY created_at",
            (manager_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def create_management_task(
    conn: sqlite3.Connection,
    manager_id: int,
    title: str,
    description: str = "",
    priority: str = "MEDIUM",
    agent_slug: str = "",
) -> int:
    """Insert a management task. Returns new row id."""
    cur = conn.execute(
        """INSERT INTO management_tasks
               (manager_id, title, description, priority, agent_slug)
           VALUES (?, ?, ?, ?, ?)
        """,
        (manager_id, title, description, priority, agent_slug),
    )
    conn.commit()
    return cur.lastrowid


def complete_management_task(conn: sqlite3.Connection, task_id: int):
    """Mark a management task as done with completed_at timestamp."""
    conn.execute(
        "UPDATE management_tasks SET status='done', completed_at=datetime('now') WHERE id=?",
        (task_id,),
    )
    conn.commit()


# ═══════════════════════════════════════════════════════════════════
# DB Maintenance
# ═══════════════════════════════════════════════════════════════════

def get_db_size_mb(db_path: str) -> float:
    """Return DB file size in MB."""
    import os
    try:
        return os.path.getsize(db_path) / (1024 * 1024)
    except Exception:
        return 0.0


def cleanup_old_project_data(
    conn: sqlite3.Connection,
    keep_sprints: int = 5,
    max_action_log_rows: int = 5000,
    max_prompt_response_rows: int = 2000,
) -> dict:
    """Remove old log data from a project DB to keep size manageable.

    Keeps data for the most recent `keep_sprints` sprints.
    Caps action_log and prompt_responses row counts.
    Returns a dict with counts of rows deleted per table.
    """
    deleted = {}

    # Find sprint IDs to keep (most recent N completed sprints)
    keep_ids = [r[0] for r in conn.execute(
        "SELECT id FROM sprints ORDER BY id DESC LIMIT ?", (keep_sprints,)
    ).fetchall()]

    if keep_ids:
        placeholders = ','.join('?' * len(keep_ids))

        # action_log — scoped by sprint_id if column exists, else cap by rowid
        try:
            conn.execute(
                f"DELETE FROM action_log WHERE sprint_id NOT IN ({placeholders})",
                keep_ids,
            )
            deleted['action_log_sprint'] = conn.execute("SELECT changes()").fetchone()[0]
        except Exception:
            pass  # sprint_id column may not exist

    # Cap action_log rows (delete oldest beyond limit)
    max_id_row = conn.execute(
        "SELECT id FROM action_log ORDER BY id DESC LIMIT 1 OFFSET ?",
        (max_action_log_rows,),
    ).fetchone()
    if max_id_row:
        conn.execute("DELETE FROM action_log WHERE id <= ?", (max_id_row[0],))
        deleted['action_log_cap'] = conn.execute("SELECT changes()").fetchone()[0]

    # Cap prompt_responses rows (delete oldest beyond limit)
    max_id_row = conn.execute(
        "SELECT id FROM prompt_responses ORDER BY id DESC LIMIT 1 OFFSET ?",
        (max_prompt_response_rows,),
    ).fetchone()
    if max_id_row:
        conn.execute("DELETE FROM prompt_responses WHERE id <= ?", (max_id_row[0],))
        deleted['prompt_responses_cap'] = conn.execute("SELECT changes()").fetchone()[0]

    conn.execute("VACUUM")
    conn.commit()
    return deleted


def cleanup_old_global_data(
    conn: sqlite3.Connection,
    max_usage_log_rows: int = 10000,
) -> dict:
    """Trim global DB usage_log to keep the most recent rows."""
    deleted = {}
    max_id_row = conn.execute(
        "SELECT id FROM usage_log ORDER BY id DESC LIMIT 1 OFFSET ?",
        (max_usage_log_rows,),
    ).fetchone()
    if max_id_row:
        conn.execute("DELETE FROM usage_log WHERE id <= ?", (max_id_row[0],))
        deleted['usage_log_cap'] = conn.execute("SELECT changes()").fetchone()[0]

    conn.execute("VACUUM")
    conn.commit()
    return deleted


def maybe_cleanup_project_db(
    conn: sqlite3.Connection,
    db_path: str,
    size_threshold_mb: float = 50.0,
    **kwargs,
) -> Optional[dict]:
    """Run cleanup only when DB file exceeds size_threshold_mb. Returns stats or None."""
    if get_db_size_mb(db_path) >= size_threshold_mb:
        return cleanup_old_project_data(conn, **kwargs)
    return None


def get_agent_performance_stats(conn: sqlite3.Connection) -> List[Dict]:
    """Query usage_log for per-agent average response times and success rates."""
    rows = conn.execute("""
        SELECT
            agent_slug,
            COUNT(*) as total,
            ROUND(AVG(duration_ms) / 1000.0, 1) as avg_seconds,
            ROUND(MIN(duration_ms) / 1000.0, 1) as min_seconds,
            ROUND(MAX(duration_ms) / 1000.0, 1) as max_seconds,
            SUM(success) as successes,
            SUM(CASE WHEN success=0 THEN 1 ELSE 0 END) as failures
        FROM usage_log
        WHERE agent_slug != ''
        GROUP BY agent_slug
        ORDER BY total DESC
    """).fetchall()
    return [
        {
            'agent': r[0], 'total': r[1], 'avg_s': r[2],
            'min_s': r[3], 'max_s': r[4],
            'successes': r[5], 'failures': r[6],
            'success_rate': round(r[5] / r[1] * 100, 1) if r[1] else 0,
        }
        for r in rows
    ]


# ═══════════════════════════════════════════════════════════════════
# Self-test
# ═══════════════════════════════════════════════════════════════════
# D-18/D-22: Task Comment Helpers
# ═══════════════════════════════════════════════════════════════════


def add_comment(conn: sqlite3.Connection, task_display_id: str, author: str, content: str) -> dict:
    """Add a comment to a task. Returns {id, task_id}."""
    row = conn.execute(
        "SELECT id FROM tasks WHERE display_id = ?", (task_display_id,)
    ).fetchone()
    if not row:
        return {"error": f"Task {task_display_id} not found"}
    task_id = row["id"]
    cur = conn.execute(
        "INSERT INTO task_comments (task_id, author, content) VALUES (?, ?, ?)",
        (task_id, author, content),
    )
    conn.commit()
    return {"id": cur.lastrowid, "task_id": task_id}


def get_comments(conn: sqlite3.Connection, task_display_id: str) -> list:
    """Return all comments for a task, oldest first."""
    row = conn.execute(
        "SELECT id FROM tasks WHERE display_id = ?", (task_display_id,)
    ).fetchone()
    if not row:
        return []
    return [dict(r) for r in conn.execute(
        "SELECT id, author, content, created_at FROM task_comments "
        "WHERE task_id = ? ORDER BY created_at ASC",
        (row["id"],),
    ).fetchall()]


# ═══════════════════════════════════════════════════════════════════
# D-19/D-23: Sprint Retrospective Helpers
# ═══════════════════════════════════════════════════════════════════


def save_retrospective(
    conn: sqlite3.Connection,
    sprint_id: int,
    went_well: str,
    to_improve: str,
    action_items: str,
) -> dict:
    """Upsert a sprint retrospective. Returns {id, sprint_id}."""
    conn.execute("""
        INSERT INTO sprint_retrospective (sprint_id, went_well, to_improve, action_items)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(sprint_id) DO UPDATE SET
            went_well    = excluded.went_well,
            to_improve   = excluded.to_improve,
            action_items = excluded.action_items
    """, (sprint_id, went_well, to_improve, action_items))
    conn.commit()
    row = conn.execute(
        "SELECT id, sprint_id FROM sprint_retrospective WHERE sprint_id = ?", (sprint_id,)
    ).fetchone()
    return dict(row) if row else {"sprint_id": sprint_id}


def get_retrospective(conn: sqlite3.Connection, sprint_id: int) -> dict:
    """Return a retrospective for a sprint or empty dict if none exists."""
    row = conn.execute(
        "SELECT * FROM sprint_retrospective WHERE sprint_id = ?", (sprint_id,)
    ).fetchone()
    return dict(row) if row else {}


# ═══════════════════════════════════════════════════════════════════
# D-12: update_sprint by ID (also expose by sprint_id)
# ═══════════════════════════════════════════════════════════════════


def update_sprint_by_id(conn: sqlite3.Connection, sprint_id: int, **fields) -> bool:
    """Update sprint fields by primary key id. Returns True on success."""
    allowed = {'name', 'goal', 'status', 'planned_points', 'completed_points',
               'velocity', 'start_date', 'end_date'}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    set_clause = ', '.join(f'{k} = ?' for k in updates)
    cur = conn.execute(
        f"UPDATE sprints SET {set_clause} WHERE id = ?",
        [*updates.values(), sprint_id]
    )
    conn.commit()
    return cur.rowcount > 0


# ═══════════════════════════════════════════════════════════════════
# D-06: list_sprints — alias for get_all_sprints for db_service compat
# ═══════════════════════════════════════════════════════════════════


def list_sprints(conn: sqlite3.Connection) -> list:
    """Return all sprints ordered by sprint_number descending."""
    return get_all_sprints(conn)


# ═══════════════════════════════════════════════════════════════════
# D-02: get_subtasks — alias compatible with db_service naming
# ═══════════════════════════════════════════════════════════════════


def get_subtasks(conn: sqlite3.Connection, task_display_id: str) -> list:
    """Return subtasks for a task by display_id (alias for get_subtasks_for_task)."""
    return get_subtasks_for_task(conn, task_display_id)


# ── H2-01: Device registry and auth audit helpers ────────────────────────────

def register_device(conn, device_id: str, device_name: str, platform: str,
                    token: str, role: str = 'admin') -> int:
    import hashlib
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    conn.execute("""
        INSERT INTO device_registry (device_id, device_name, platform, token_hash, role)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(device_id) DO UPDATE SET
            token_hash=excluded.token_hash, device_name=excluded.device_name,
            revoked=0, last_seen=datetime('now')
    """, (device_id, device_name, platform, token_hash, role))
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]

def verify_device_token(conn, token: str) -> dict | None:
    import hashlib
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    row = conn.execute("""
        SELECT * FROM device_registry
        WHERE token_hash=? AND revoked=0
    """, (token_hash,)).fetchone()
    if row:
        conn.execute("UPDATE device_registry SET last_seen=datetime('now') WHERE token_hash=?",
                     (token_hash,))
        conn.commit()
        return dict(row)
    return None

def revoke_device(conn, device_id: str) -> bool:
    cur = conn.execute("UPDATE device_registry SET revoked=1 WHERE device_id=?", (device_id,))
    conn.commit()
    return cur.rowcount > 0

def list_devices(conn) -> list:
    rows = conn.execute("""
        SELECT id, device_id, device_name, platform, role, paired_at, last_seen, revoked
        FROM device_registry ORDER BY paired_at DESC
    """).fetchall()
    return [dict(r) for r in rows]

def log_auth_event(conn, event: str, device_id: str = '', device_name: str = '',
                   ip_address: str = '', success: bool = True, detail: str = '') -> None:
    conn.execute("""
        INSERT INTO auth_audit_log (event, device_id, device_name, ip_address, success, detail)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (event, device_id, device_name, ip_address, int(success), detail))
    conn.commit()

def get_auth_audit_log(conn, limit: int = 100) -> list:
    rows = conn.execute("""
        SELECT * FROM auth_audit_log ORDER BY created_at DESC LIMIT ?
    """, (limit,)).fetchall()
    return [dict(r) for r in rows]


# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import tempfile, os

    print("=== Automatica DB self-test ===\n")

    # Test global DB
    with tempfile.TemporaryDirectory() as tmpdir:
        gdb_path = Path(tmpdir) / "automatica.db"
        gconn = init_global_db(gdb_path)
        print(f"[OK] Global DB created: {gdb_path}")
        assert get_schema_version(gconn) == SCHEMA_VERSION
        print(f"[OK] Schema version: {SCHEMA_VERSION}")

        seed_default_providers(gconn)
        providers = gconn.execute("SELECT name FROM providers ORDER BY id").fetchall()
        print(f"[OK] Seeded {len(providers)} providers: {[r['name'] for r in providers]}")
        gconn.close()

    # Test project DB
    with tempfile.TemporaryDirectory() as tmpdir:
        pconn = init_project_db(Path(tmpdir))
        print(f"[OK] Project DB created: {Path(tmpdir) / '.automatica' / 'project.db'}")

        seed_default_pipeline(pconn)
        steps = pconn.execute("SELECT COUNT(*) FROM pipeline_steps").fetchone()[0]
        print(f"[OK] Seeded {steps} pipeline steps")

        # Create project task
        pt = create_project_task(pconn, "Build game engine", "The big goal", "HIGH")
        print(f"[OK] Project task: {pt}")

        # Create tasks
        t1 = create_task(pconn, "Implement rendering", complexity=8, project_task_id=pt["id"])
        t2 = create_task(pconn, "Add physics", complexity=5, parent_task_id=t1["id"])
        print(f"[OK] Tasks: {t1['display_id']}, {t2['display_id']}")

        # Add subtasks
        sub = add_subtask(pconn, t1["display_id"], "Setup shader pipeline")
        print(f"[OK] Subtask: {sub}")

        # Check subtask
        check_subtask(pconn, sub["id"], True, by="coder-agent")
        sub_row = pconn.execute("SELECT status FROM subtasks WHERE id = ?", (sub["id"],)).fetchone()
        assert sub_row["status"] == "done"
        print("[OK] Subtask checked off")

        # Update task
        update_task(pconn, t1["display_id"], status="in_progress")
        t1_row = pconn.execute("SELECT status, started_at FROM tasks WHERE display_id = ?",
                               (t1["display_id"],)).fetchone()
        assert t1_row["status"] == "in_progress"
        assert t1_row["started_at"] is not None
        print("[OK] Task updated to in_progress with started_at")

        # Log action
        aid = log_action(pconn, "task", t1["id"], "coder-agent", "status_change",
                         {"old_value": "backlog", "new_value": "in_progress"})
        print(f"[OK] Action logged: id={aid}")

        # Log file modification
        mid = log_file_modification(pconn, "task", t1["id"], "engine/renderer.cpp",
                                     "edit", "old code", "new code")
        print(f"[OK] File modification logged: id={mid}")

        # Sprint
        sprint = create_sprint(pconn, 1, "Sprint 1", "Get rendering working")
        st = assign_task_to_sprint(pconn, t1["display_id"], 1)
        print(f"[OK] Sprint task: {st}")

        # Build context
        ctx = build_task_context(pconn, t1["display_id"])
        assert ctx["task"]["display_id"] == t1["display_id"]
        assert len(ctx["subtasks"]) >= 1
        assert len(ctx["recent_history"]) >= 1
        assert ctx["sprint_context"] is not None
        print(f"[OK] Task context built: {len(ctx)} sections")

        # Sprint summary
        summary = build_sprint_summary(pconn, 1)
        assert summary["sprint"]["sprint_number"] == 1
        print(f"[OK] Sprint summary: {len(summary['tasks'])} tasks")

        # Timeline
        timeline = build_task_timeline(pconn, t1["display_id"])
        assert len(timeline) >= 1
        print(f"[OK] Timeline: {len(timeline)} entries")

        # Reorder
        reorder_task(pconn, t2["display_id"], 0)
        t2_row = pconn.execute("SELECT sort_order FROM tasks WHERE display_id = ?",
                               (t2["display_id"],)).fetchone()
        assert t2_row["sort_order"] == 0
        print("[OK] Task reordered")

        # Log prompt response
        pr_id = log_prompt_response(pconn, "sess-001", "coder", "Ollama",
                                     t1["display_id"], '{"role":"user"}', '{"content":"ok"}',
                                     100, 50, 1200)
        print(f"[OK] Prompt response logged: id={pr_id}")

        # Log VCS commit
        vc_id = log_vcs_commit(pconn, "abc123def", "task", t1["id"],
                                "Implement rendering", "agent", "main", 3, 100, 20)
        print(f"[OK] VCS commit logged: id={vc_id}")

        pconn.close()

    print("\n=== All tests passed ===")
