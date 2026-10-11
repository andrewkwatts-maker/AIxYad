#!/usr/bin/env python3
"""
AIxYad View Actions — Hierarchical file/code view system for AI agents.

Provides structured get/set view operations so agents can navigate a project's
file tree, open specific files or code sections, and retrieve keyword-matched
related content — all without consuming full file contents in every prompt.

Actions:
  get_view             — Return hierarchical project tree (with open sections expanded)
  set_view             — Mark a path as open/closed (persists per session)
  get_view_compact     — Return only currently-open sections with GUID anchors
  get_related_content  — Return GUID-tagged sections matching task keywords
  get_related_keywords — Return current keyword list for this task session
  set_related_keywords — Add/remove keywords from this session's list
  replace_section      — Replace a GUID-anchored code section
  insert_line          — Insert content after a specific line number
  remove_section       — Remove a GUID-anchored section (marks referencing tasks)
  rename_symbol        — Rename a symbol and update all task references
"""

import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ───────────────────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────────────────

_BINARY_EXTS = {'.pyc', '.pyo', '.so', '.dll', '.exe', '.bin', '.jpg', '.jpeg',
                '.png', '.gif', '.bmp', '.ico', '.mp3', '.mp4', '.wav', '.zip',
                '.gz', '.tar', '.db', '.sqlite', '.sqlite3'}

_TEXT_EXTS = {'.py', '.js', '.ts', '.jsx', '.tsx', '.html', '.css', '.md',
              '.txt', '.json', '.yaml', '.yml', '.toml', '.ini', '.cfg',
              '.sh', '.bat', '.ps1', '.c', '.cpp', '.h', '.hpp', '.java',
              '.cs', '.go', '.rs', '.kv'}

_IGNORE_DIRS = {'.git', '__pycache__', 'node_modules', '.aixyad', '.automatica', 'dist',
                'build', '.venv', 'venv', 'env', '.env', '.mypy_cache', '.pytest_cache'}


def _is_text_file(path: Path) -> bool:
    return path.suffix.lower() in _TEXT_EXTS


def _read_lines(path: Path) -> List[str]:
    try:
        return path.read_text(encoding='utf-8', errors='replace').splitlines()
    except Exception:
        return []


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode('utf-8', errors='replace')).hexdigest()[:12]


# ───────────────────────────────────────────────────────────────────────────────
# ViewActionHandler
# ───────────────────────────────────────────────────────────────────────────────

class ViewActionHandler:
    """Handles all view/search/structured-edit agent actions."""

    ACTIONS = [
        'get_view', 'set_view', 'get_view_compact',
        'get_related_content', 'get_related_keywords', 'set_related_keywords',
        'replace_section', 'insert_line', 'remove_section', 'rename_symbol',
    ]

    def __init__(self, project_dir: Path, project_db: Optional[sqlite3.Connection] = None):
        self.project_dir = Path(project_dir)
        self.project_db = project_db

    def handle(self, action: str, params: dict, session_id: str = '') -> dict:
        handler = getattr(self, f'_do_{action}', None)
        if handler:
            return handler(params, session_id)
        return {'success': False, 'error': f'Unknown action: {action}'}

    @classmethod
    def get_available_actions(cls) -> list:
        return [
            {'name': 'get_view',
             'description': 'Return hierarchical project file tree. Open paths show content.',
             'params': {'path': 'string (optional, default "." = project root)', 'depth': 'int (optional, max depth)'}},
            {'name': 'set_view',
             'description': 'Open or close a file/section in the view. Opened paths show content in get_view.',
             'params': {'path': 'string (relative file path, or "file/SymbolName" for a symbol)', 'open': 'bool', 'lines': 'int[] (optional [start, end])'}},
            {'name': 'get_view_compact',
             'description': 'Return only currently-open sections with GUID anchors. More token-efficient than get_view.',
             'params': {}},
            {'name': 'get_related_content',
             'description': 'Return code sections and symbols related to the current task keywords.',
             'params': {'keywords': 'string[] (optional — defaults to task keywords)', 'max_results': 'int (optional, default 10)'}},
            {'name': 'get_related_keywords',
             'description': 'Return the keyword list for the current task session.',
             'params': {}},
            {'name': 'set_related_keywords',
             'description': 'Add or remove keywords from the current task session.',
             'params': {'add': 'string[] (optional)', 'remove': 'string[] (optional)'}},
            {'name': 'replace_section',
             'description': 'Replace a GUID-tagged code section with new content.',
             'params': {'guid': 'string', 'new_content': 'string'}},
            {'name': 'insert_line',
             'description': 'Insert content after a specific line number in a file.',
             'params': {'path': 'string', 'after_line': 'int', 'content': 'string'}},
            {'name': 'remove_section',
             'description': 'Remove a GUID-tagged code section. Task descriptions referencing it are updated.',
             'params': {'guid': 'string'}},
            {'name': 'rename_symbol',
             'description': 'Rename a symbol by GUID. Updates all task description references.',
             'params': {'guid': 'string', 'new_name': 'string'}},
        ]

    # ─── get_view ────────────────────────────────────────────────────────────

    def _do_get_view(self, params: dict, session_id: str) -> dict:
        root_path = params.get('path', '.')
        max_depth = int(params.get('depth', 3))
        base = (self.project_dir / root_path).resolve()
        if not base.exists():
            return {'success': False, 'error': f'Path not found: {root_path}'}

        open_paths = self._get_open_paths(session_id)
        tree_lines = []
        self._build_tree(base, self.project_dir, open_paths, tree_lines, depth=0, max_depth=max_depth)

        return {
            'success': True,
            'tree': '\n'.join(tree_lines),
            'open_count': len(open_paths),
            'tip': 'Use set_view to open files/symbols, then get_view_compact to see only open content.',
        }

    def _build_tree(self, path: Path, project_root: Path, open_paths: set,
                    lines: list, depth: int, max_depth: int, prefix: str = ''):
        rel = str(path.relative_to(project_root)).replace('\\', '/')
        indent = '  ' * depth

        if path.is_dir():
            if path.name in _IGNORE_DIRS:
                return
            lines.append(f'{indent}{path.name}/')
            if depth < max_depth:
                try:
                    children = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name))
                    for child in children:
                        self._build_tree(child, project_root, open_paths, lines,
                                         depth + 1, max_depth)
                except PermissionError:
                    pass
        else:
            is_open = rel in open_paths
            marker = ' [OPEN]' if is_open else ''
            lines.append(f'{indent}{path.name}{marker}')
            if is_open and _is_text_file(path):
                file_lines = _read_lines(path)
                # Check if specific line range is open
                line_ranges = self._get_line_ranges(session_id, rel)
                if line_ranges:
                    for start, end in line_ranges:
                        lines.append(f'{indent}  Lines {start}-{end}:')
                        for i, line in enumerate(file_lines[start-1:end], start=start):
                            lines.append(f'{indent}    {i}: {line}')
                else:
                    # Show first 40 lines
                    for i, line in enumerate(file_lines[:40], start=1):
                        lines.append(f'{indent}  {i}: {line}')
                    if len(file_lines) > 40:
                        lines.append(f'{indent}  ... ({len(file_lines) - 40} more lines)')

    # ─── set_view ────────────────────────────────────────────────────────────

    def _do_set_view(self, params: dict, session_id: str) -> dict:
        path = params.get('path', '')
        is_open = bool(params.get('open', True))
        lines = params.get('lines', None)

        if not path:
            return {'success': False, 'error': 'path is required'}

        if not self.project_db:
            return {'success': False, 'error': 'No project DB connection'}

        line_start = lines[0] if lines and len(lines) >= 1 else None
        line_end = lines[1] if lines and len(lines) >= 2 else None

        try:
            self.project_db.execute(
                'INSERT INTO view_state (session_id, path, is_open, line_start, line_end) '
                'VALUES (?, ?, ?, ?, ?) '
                'ON CONFLICT(session_id, path) DO UPDATE SET '
                'is_open=excluded.is_open, line_start=excluded.line_start, line_end=excluded.line_end',
                (session_id, path, 1 if is_open else 0, line_start, line_end)
            )
            self.project_db.commit()
        except Exception as exc:
            return {'success': False, 'error': str(exc)}

        return {
            'success': True,
            'path': path,
            'open': is_open,
            'message': f'{"Opened" if is_open else "Closed"}: {path}',
        }

    # ─── get_view_compact ────────────────────────────────────────────────────

    def _do_get_view_compact(self, params: dict, session_id: str) -> dict:
        open_paths = self._get_open_paths(session_id)
        if not open_paths:
            return {
                'success': True,
                'content': '(No paths open — use set_view first)',
                'tip': 'Call set_view with {"path": "src/main.py", "open": true} to open a file.',
            }

        sections = []
        for rel_path in sorted(open_paths):
            full_path = self.project_dir / rel_path
            if not full_path.exists() or not _is_text_file(full_path):
                continue
            file_lines = _read_lines(full_path)
            line_ranges = self._get_line_ranges(session_id, rel_path)

            if line_ranges:
                for start, end in line_ranges:
                    guid = self._get_section_guid(rel_path, start, end)
                    content = '\n'.join(f'{i}: {l}' for i, l in
                                        enumerate(file_lines[start-1:end], start=start))
                    sections.append(
                        f'[START:{rel_path} guid={guid} lines={start}-{end}]\n'
                        f'{content}\n'
                        f'[END:{rel_path}]'
                    )
            else:
                # Full file, look up code_sections GUIDs
                guid = self._get_section_guid(rel_path, 1, len(file_lines))
                preview = '\n'.join(f'{i}: {l}' for i, l in enumerate(file_lines[:60], start=1))
                if len(file_lines) > 60:
                    preview += f'\n... ({len(file_lines) - 60} more lines — use set_view with lines=[start,end])'
                sections.append(
                    f'[START:{rel_path} guid={guid}]\n'
                    f'{preview}\n'
                    f'[END:{rel_path}]'
                )

        return {
            'success': True,
            'content': '\n\n'.join(sections),
            'open_count': len(open_paths),
        }

    # ─── get_related_content ─────────────────────────────────────────────────

    def _do_get_related_content(self, params: dict, session_id: str) -> dict:
        keywords = params.get('keywords', [])
        max_results = int(params.get('max_results', 10))

        if not keywords and self.project_db:
            keywords = self._get_session_keywords(session_id)

        if not keywords:
            return {'success': False, 'error': 'No keywords provided and none in session. Call set_related_keywords first.'}

        matches = []

        # Search code_sections table if available
        if self.project_db:
            try:
                sections = self.project_db.execute(
                    'SELECT guid, file_path, section_name, section_type, content_preview, keywords '
                    'FROM code_sections WHERE location_stale = 0 ORDER BY id DESC LIMIT 500'
                ).fetchall()
                for row in sections:
                    row_keywords = (row['keywords'] or '').lower()
                    row_name = (row['section_name'] or '').lower()
                    row_path = (row['file_path'] or '').lower()
                    score = sum(
                        1 for kw in keywords
                        if kw.lower() in row_keywords or kw.lower() in row_name or kw.lower() in row_path
                    )
                    if score > 0:
                        matches.append({
                            'source': 'code_section',
                            'guid': row['guid'],
                            'path': row['file_path'],
                            'name': row['section_name'],
                            'type': row['section_type'],
                            'preview': (row['content_preview'] or '')[:200],
                            'score': score,
                        })
            except Exception:
                pass

            # Search file_symbols table
            try:
                symbols = self.project_db.execute(
                    'SELECT fs.name, fs.symbol_type, fs.line_start, fs.line_end, '
                    'pf.file_path '
                    'FROM file_symbols fs '
                    'JOIN project_files pf ON pf.id = fs.file_id '
                    'ORDER BY fs.id DESC LIMIT 500'
                ).fetchall()
                for row in symbols:
                    name = (row['name'] or '').lower()
                    path = (row['file_path'] or '').lower()
                    score = sum(1 for kw in keywords if kw.lower() in name or kw.lower() in path)
                    if score > 0:
                        matches.append({
                            'source': 'symbol',
                            'path': row['file_path'],
                            'name': row['name'],
                            'type': row['symbol_type'],
                            'line_start': row['line_start'],
                            'line_end': row['line_end'],
                            'score': score,
                        })
            except Exception:
                pass

        # Fallback: grep source files for keyword matches
        if not matches:
            matches = self._grep_project(keywords, max_results * 2)

        matches.sort(key=lambda m: m['score'], reverse=True)
        top = matches[:max_results]

        return {
            'success': True,
            'keywords': keywords,
            'results': top,
            'total_found': len(matches),
            'tip': 'Use set_view {"path": "...", "open": true} to open a result, then get_view_compact.',
        }

    def _grep_project(self, keywords: list, limit: int) -> list:
        matches = []
        pattern = '|'.join(re.escape(kw) for kw in keywords)
        try:
            compiled = re.compile(pattern, re.IGNORECASE)
        except re.error:
            return []

        for root, dirs, files in os.walk(self.project_dir):
            dirs[:] = [d for d in dirs if d not in _IGNORE_DIRS]
            for fname in files:
                fpath = Path(root) / fname
                if not _is_text_file(fpath):
                    continue
                rel = str(fpath.relative_to(self.project_dir)).replace('\\', '/')
                try:
                    text = fpath.read_text(encoding='utf-8', errors='replace')
                    hits = compiled.findall(text)
                    if hits:
                        score = len(hits)
                        first_match = compiled.search(text)
                        preview = text[max(0, first_match.start()-40):first_match.end()+80].strip()
                        matches.append({
                            'source': 'grep',
                            'path': rel,
                            'name': fname,
                            'type': 'file',
                            'preview': preview[:200],
                            'score': score,
                        })
                except Exception:
                    pass
            if len(matches) >= limit:
                break

        return matches

    # ─── get_related_keywords ────────────────────────────────────────────────

    def _do_get_related_keywords(self, params: dict, session_id: str) -> dict:
        keywords = self._get_session_keywords(session_id)
        return {
            'success': True,
            'keywords': keywords,
            'session_id': session_id,
            'tip': 'Use set_related_keywords to add/remove keywords.',
        }

    # ─── set_related_keywords ────────────────────────────────────────────────

    def _do_set_related_keywords(self, params: dict, session_id: str) -> dict:
        add = params.get('add', [])
        remove = params.get('remove', [])

        current = set(self._get_session_keywords(session_id))
        current.update(kw for kw in add if kw)
        current -= set(remove)
        new_list = sorted(current)

        if self.project_db:
            try:
                self.project_db.execute(
                    'INSERT INTO view_state (session_id, path, is_open) '
                    'VALUES (?, ?, ?) '
                    'ON CONFLICT(session_id, path) DO UPDATE SET is_open=1',
                    (session_id, '__keywords__', 1)
                )
                # Store keywords as a special JSON entry in project_settings
                self.project_db.execute(
                    'INSERT INTO project_settings (key, value) VALUES (?, ?) '
                    'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                    (f'session_keywords_{session_id}', json.dumps(new_list))
                )
                self.project_db.commit()
            except Exception as exc:
                return {'success': False, 'error': str(exc)}

        return {
            'success': True,
            'keywords': new_list,
            'added': list(add),
            'removed': list(remove),
        }

    # ─── replace_section ─────────────────────────────────────────────────────

    def _do_replace_section(self, params: dict, session_id: str) -> dict:
        guid = params.get('guid', '')
        new_content = params.get('new_content', '')

        if not guid:
            return {'success': False, 'error': 'guid is required'}

        # Look up the section in code_sections
        if not self.project_db:
            return {'success': False, 'error': 'No project DB connection'}

        try:
            row = self.project_db.execute(
                'SELECT file_path, line_start, line_end, section_name '
                'FROM code_sections WHERE guid = ? AND location_stale = 0 LIMIT 1',
                (guid,)
            ).fetchone()
        except Exception as exc:
            return {'success': False, 'error': f'DB lookup failed: {exc}'}

        if not row:
            return {'success': False, 'error': f'Section not found (guid={guid}). It may be stale or never indexed.'}

        file_path = self.project_dir / row['file_path']
        if not file_path.exists():
            return {'success': False, 'error': f'File not found: {row["file_path"]}'}

        try:
            lines = _read_lines(file_path)
            start = max(1, int(row['line_start'] or 1)) - 1  # 0-indexed
            end = min(len(lines), int(row['line_end'] or len(lines)))
            new_lines = new_content.splitlines()
            lines[start:end] = new_lines
            file_path.write_text('\n'.join(lines), encoding='utf-8')
        except Exception as exc:
            return {'success': False, 'error': f'File write failed: {exc}'}

        # Mark old section stale, create new record
        try:
            new_guid = _sha256(new_content + guid)
            self.project_db.execute(
                'UPDATE code_sections SET location_stale=1, replaced_by=? WHERE guid=?',
                (new_guid, guid)
            )
            self.project_db.execute(
                'INSERT OR IGNORE INTO code_sections '
                '(guid, file_path, section_name, section_type, line_start, line_end, '
                'content_preview, location_stale) VALUES (?,?,?,?,?,?,?,0)',
                (new_guid, row['file_path'], row['section_name'], 'edited',
                 start + 1, start + 1 + len(new_lines),
                 new_content[:300])
            )
            self.project_db.commit()
        except Exception:
            pass  # DB update is best-effort; file write is the critical part

        return {
            'success': True,
            'guid': guid,
            'new_guid': new_guid,
            'file': row['file_path'],
            'lines_replaced': end - start,
            'new_lines': len(new_lines),
        }

    # ─── insert_line ─────────────────────────────────────────────────────────

    def _do_insert_line(self, params: dict, session_id: str) -> dict:
        path = params.get('path', '')
        after_line = int(params.get('after_line', 0))
        content = params.get('content', '')

        if not path:
            return {'success': False, 'error': 'path is required'}

        file_path = self.project_dir / path
        if not file_path.exists():
            return {'success': False, 'error': f'File not found: {path}'}

        try:
            lines = _read_lines(file_path)
            insert_at = max(0, min(after_line, len(lines)))
            new_lines = content.splitlines()
            lines[insert_at:insert_at] = new_lines
            file_path.write_text('\n'.join(lines), encoding='utf-8')
        except Exception as exc:
            return {'success': False, 'error': f'File write failed: {exc}'}

        return {
            'success': True,
            'file': path,
            'inserted_after_line': after_line,
            'lines_inserted': len(new_lines),
            'new_total_lines': len(lines),
        }

    # ─── remove_section ──────────────────────────────────────────────────────

    def _do_remove_section(self, params: dict, session_id: str) -> dict:
        guid = params.get('guid', '')
        if not guid or not self.project_db:
            return {'success': False, 'error': 'guid and project DB required'}

        try:
            row = self.project_db.execute(
                'SELECT file_path, line_start, line_end, section_name '
                'FROM code_sections WHERE guid = ? AND location_stale = 0 LIMIT 1',
                (guid,)
            ).fetchone()
        except Exception as exc:
            return {'success': False, 'error': str(exc)}

        if not row:
            return {'success': False, 'error': f'Section not found: guid={guid}'}

        section_name = row['section_name'] or 'UNKNOWN'
        file_path = self.project_dir / row['file_path']

        if file_path.exists():
            try:
                lines = _read_lines(file_path)
                start = max(1, int(row['line_start'] or 1)) - 1
                end = min(len(lines), int(row['line_end'] or len(lines)))
                del lines[start:end]
                file_path.write_text('\n'.join(lines), encoding='utf-8')
            except Exception as exc:
                return {'success': False, 'error': f'File write failed: {exc}'}

        # Mark section stale
        try:
            self.project_db.execute(
                'UPDATE code_sections SET location_stale=1 WHERE guid=?', (guid,)
            )
            # Update task descriptions that reference this symbol to UNDEFINED_FUNC
            self.project_db.execute(
                "UPDATE tasks SET description = REPLACE(description, ?, '[UNDEFINED_FUNC:' || ? || ']') "
                'WHERE description LIKE ?',
                (section_name, section_name, f'%{section_name}%')
            )
            self.project_db.commit()
        except Exception:
            pass

        return {
            'success': True,
            'guid': guid,
            'removed_symbol': section_name,
            'file': row['file_path'],
            'note': f'Any task descriptions referencing "{section_name}" updated to [UNDEFINED_FUNC:{section_name}]',
        }

    # ─── rename_symbol ───────────────────────────────────────────────────────

    def _do_rename_symbol(self, params: dict, session_id: str) -> dict:
        guid = params.get('guid', '')
        new_name = params.get('new_name', '').strip()

        if not guid or not new_name:
            return {'success': False, 'error': 'guid and new_name are required'}
        if not self.project_db:
            return {'success': False, 'error': 'No project DB connection'}

        try:
            row = self.project_db.execute(
                'SELECT file_path, line_start, line_end, section_name '
                'FROM code_sections WHERE guid = ? AND location_stale = 0 LIMIT 1',
                (guid,)
            ).fetchone()
        except Exception as exc:
            return {'success': False, 'error': str(exc)}

        if not row:
            return {'success': False, 'error': f'Symbol not found: guid={guid}'}

        old_name = row['section_name'] or ''
        file_path = self.project_dir / row['file_path']

        # Rename in source file via simple string replace
        renamed_in_file = False
        if file_path.exists() and old_name:
            try:
                text = file_path.read_text(encoding='utf-8', errors='replace')
                new_text = re.sub(r'\b' + re.escape(old_name) + r'\b', new_name, text)
                if new_text != text:
                    file_path.write_text(new_text, encoding='utf-8')
                    renamed_in_file = True
            except Exception:
                pass

        # Update code_sections
        try:
            self.project_db.execute(
                'UPDATE code_sections SET section_name=? WHERE guid=?', (new_name, guid)
            )
            # Update task descriptions
            tasks_updated = self.project_db.execute(
                "UPDATE tasks SET description = REPLACE(description, ?, ?) "
                'WHERE description LIKE ?',
                (old_name, new_name, f'%{old_name}%')
            ).rowcount
            self.project_db.commit()
        except Exception as exc:
            return {'success': False, 'error': f'DB update failed: {exc}'}

        return {
            'success': True,
            'guid': guid,
            'old_name': old_name,
            'new_name': new_name,
            'renamed_in_file': renamed_in_file,
            'tasks_updated': tasks_updated,
        }

    # ─── Internal helpers ────────────────────────────────────────────────────

    def _get_open_paths(self, session_id: str) -> set:
        if not self.project_db or not session_id:
            return set()
        try:
            rows = self.project_db.execute(
                'SELECT path FROM view_state WHERE session_id=? AND is_open=1 AND path != "__keywords__"',
                (session_id,)
            ).fetchall()
            return {r[0] for r in rows}
        except Exception:
            return set()

    def _get_line_ranges(self, session_id: str, path: str) -> list:
        if not self.project_db or not session_id:
            return []
        try:
            rows = self.project_db.execute(
                'SELECT line_start, line_end FROM view_state '
                'WHERE session_id=? AND path=? AND is_open=1 '
                'AND line_start IS NOT NULL',
                (session_id, path)
            ).fetchall()
            return [(r[0], r[1]) for r in rows if r[0] and r[1]]
        except Exception:
            return []

    def _get_session_keywords(self, session_id: str) -> list:
        if not self.project_db or not session_id:
            return []
        try:
            row = self.project_db.execute(
                'SELECT value FROM project_settings WHERE key=?',
                (f'session_keywords_{session_id}',)
            ).fetchone()
            if row:
                return json.loads(row[0])
        except Exception:
            pass
        return []

    def _get_section_guid(self, rel_path: str, line_start: int, line_end: int) -> str:
        if self.project_db:
            try:
                row = self.project_db.execute(
                    'SELECT guid FROM code_sections WHERE file_path=? AND line_start=? '
                    'AND location_stale=0 LIMIT 1',
                    (rel_path, line_start)
                ).fetchone()
                if row:
                    return row[0]
            except Exception:
                pass
        # Synthetic guid from path + line
        return _sha256(f'{rel_path}:{line_start}:{line_end}')

    def clear_session(self, session_id: str):
        """Clear all view state and keywords for a session (call on step_done)."""
        if not self.project_db or not session_id:
            return
        try:
            self.project_db.execute(
                'DELETE FROM view_state WHERE session_id=?', (session_id,)
            )
            self.project_db.execute(
                'DELETE FROM project_settings WHERE key=?',
                (f'session_keywords_{session_id}',)
            )
            self.project_db.commit()
        except Exception:
            pass
