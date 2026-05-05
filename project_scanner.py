#!/usr/bin/env python3
"""
Automatica Project Scanner — Live file structure and symbol queries.

All queries run against actual files on disk (not cached DB data).
The DB is only used for historical snapshots and caching scan results.

Usage:
    from project_scanner import get_file_structure, search_symbols, list_project_files
    symbols = get_file_structure(project_dir, 'engine/core/Engine.cpp')
    results = search_symbols(project_dir, name='Renderer', symbol_type='class')
    files = list_project_files(project_dir, extension='.cpp')
"""

import fnmatch
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional


# ── Configuration ──────────────────────────────────────────────────

# File extensions to language mapping
LANGUAGE_MAP = {
    '.cpp': 'cpp', '.hpp': 'cpp', '.h': 'cpp', '.c': 'c', '.cc': 'cpp', '.cxx': 'cpp',
    '.py': 'python', '.pyx': 'python', '.pyi': 'python',
    '.js': 'javascript', '.mjs': 'javascript', '.cjs': 'javascript',
    '.ts': 'typescript', '.tsx': 'typescript', '.jsx': 'javascript',
    '.json': 'json', '.jsonc': 'json',
    '.yaml': 'yaml', '.yml': 'yaml', '.toml': 'toml',
    '.md': 'markdown', '.rst': 'markdown',
    '.rs': 'rust', '.go': 'go', '.java': 'java', '.cs': 'csharp',
    '.bat': 'batch', '.cmd': 'batch', '.sh': 'shell', '.bash': 'shell',
    '.ps1': 'powershell', '.psm1': 'powershell',
    '.cmake': 'cmake', '.txt': 'text',
    '.xml': 'xml', '.html': 'html', '.css': 'css', '.scss': 'css',
    '.sql': 'sql', '.lua': 'lua', '.rb': 'ruby', '.swift': 'swift',
    '.kt': 'kotlin', '.kts': 'kotlin', '.dart': 'dart',
    '.proto': 'protobuf', '.ini': 'ini', '.cfg': 'ini',
}

# Directories to skip when walking
SKIP_DIRS = frozenset({
    '.git', '.svn', '.hg', 'node_modules', '__pycache__', '.vs', '.vscode',
    'build', 'build_new', 'dist', 'out', 'target', '.automatica', '.idea',
    'Debug', 'Release', 'x64', 'x86', '.cache', 'CMakeFiles',
    'vcpkg_installed', 'packages', 'bin', 'obj',
    # Python virtual envs — when an agent provisions its own venv inside a
    # project (Pong did this), site-packages dwarfs every real source file
    # and floods wiki_summary with thousands of pip internals.
    'env', 'venv', '.venv', 'virtualenv', '.virtualenv', 'site-packages',
    'pyvenv', '.pyvenv', '.tox', '.nox', '.pytest_cache', '.mypy_cache',
    '.ruff_cache',
})

# Binary extensions to skip entirely
BINARY_EXTS = frozenset({
    '.exe', '.dll', '.so', '.dylib', '.o', '.obj', '.lib', '.a', '.pdb',
    '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.ico', '.svg', '.webp',
    '.zip', '.tar', '.gz', '.bz2', '.7z', '.rar', '.xz',
    '.mp3', '.wav', '.ogg', '.mp4', '.avi', '.mkv', '.flac',
    '.ttf', '.otf', '.woff', '.woff2', '.eot',
    '.db', '.sqlite', '.sqlite3', '.pyc', '.pyo', '.class',
    '.pdf', '.doc', '.docx', '.xls', '.xlsx',
})

# Max file size to parse (skip very large files)
MAX_PARSE_SIZE = 2 * 1024 * 1024  # 2 MB

# ── Symbol Regex Patterns ──────────────────────────────────────────

# C/C++ patterns
_CPP_PATTERNS = {
    'class': re.compile(
        r'^(?:\s*(?:template\s*<[^>]*>\s*)?)'
        r'(?:class|struct)\s+'
        r'(?:[\w_]+\s+)?'  # optional declspec/attribute
        r'([\w_]+)'
        r'(?:\s*(?:final\s*)?(?::\s*(?:public|private|protected)\s+[\w_:]+(?:\s*,\s*(?:public|private|protected)\s+[\w_:]+)*)?)?\s*\{',
        re.MULTILINE,
    ),
    'function': re.compile(
        r'^(?:[\w_:*&<>,\s]+?)\s+'  # return type
        r'([\w_]+)\s*'               # function name
        r'\(([^)]*)\)\s*'            # params
        r'(?:const\s*)?(?:noexcept\s*(?:\([^)]*\))?\s*)?(?:override\s*)?(?:final\s*)?'
        r'(?:->[\w_:*&<>,\s]+\s*)?'  # trailing return
        r'\{',
        re.MULTILINE,
    ),
    'enum': re.compile(
        r'^\s*enum\s+(?:class\s+)?(\w+)',
        re.MULTILINE,
    ),
    'namespace': re.compile(
        r'^\s*namespace\s+([\w_]+(?:::[\w_]+)*)\s*\{',
        re.MULTILINE,
    ),
    'define': re.compile(
        r'^\s*#define\s+([\w_]+)',
        re.MULTILINE,
    ),
    'typedef': re.compile(
        r'^\s*(?:typedef|using)\s+[\w_:*&<>,\s]+?\s+([\w_]+)\s*[;=]',
        re.MULTILINE,
    ),
}

# Python patterns
_PY_PATTERNS = {
    'class': re.compile(r'^class\s+([\w_]+)\s*[:\(]', re.MULTILINE),
    'function': re.compile(r'^(\s*)def\s+([\w_]+)\s*\(', re.MULTILINE),
    'variable': re.compile(r'^([A-Z][A-Z_0-9]+)\s*=', re.MULTILINE),
}

# Markdown patterns
_MD_PATTERNS = {
    'section': re.compile(r'^(#{1,6})\s+(.+)$', re.MULTILINE),
}

# Rust patterns
_RUST_PATTERNS = {
    'function': re.compile(r'^\s*(?:pub\s+)?(?:async\s+)?fn\s+([\w_]+)', re.MULTILINE),
    'class': re.compile(r'^\s*(?:pub\s+)?(?:struct|trait|enum)\s+([\w_]+)', re.MULTILINE),
}

# Go patterns
_GO_PATTERNS = {
    'function': re.compile(r'^\s*func\s+(?:\(\s*\w+\s+\*?\w+\s*\)\s*)?([\w_]+)', re.MULTILINE),
    'class': re.compile(r'^\s*type\s+([\w_]+)\s+(?:struct|interface)', re.MULTILINE),
}

# Java/C# patterns
_JAVA_PATTERNS = {
    'class': re.compile(
        r'^\s*(?:public|private|protected|internal|static|abstract|sealed|final|\s)*'
        r'(?:class|interface|enum|record)\s+([\w_]+)',
        re.MULTILINE,
    ),
    'function': re.compile(
        r'^\s*(?:public|private|protected|internal|static|abstract|virtual|override|async|\s)*'
        r'[\w_<>\[\],\s]+\s+([\w_]+)\s*\(',
        re.MULTILINE,
    ),
}


def _get_language(file_path: str) -> str:
    """Determine language from file extension."""
    ext = Path(file_path).suffix.lower()
    return LANGUAGE_MAP.get(ext, '')


def _read_file_safe(full_path: Path) -> Optional[str]:
    """Read a file safely, returning None on failure."""
    try:
        if full_path.stat().st_size > MAX_PARSE_SIZE:
            return None
        return full_path.read_text(encoding='utf-8', errors='replace')
    except (OSError, UnicodeDecodeError):
        return None


def _should_skip(name: str, is_dir: bool) -> bool:
    """Check if a file/dir should be skipped during walk."""
    if is_dir:
        return name in SKIP_DIRS or name.startswith('.')
    ext = Path(name).suffix.lower()
    return ext in BINARY_EXTS


# ── Core Scanner Functions ─────────────────────────────────────────


def get_file_structure(project_dir: Path, file_path: str) -> dict:
    """Parse a file RIGHT NOW and return its symbols/structure.

    Returns:
        {
            'file': str,
            'language': str,
            'line_count': int,
            'size_bytes': int,
            'functions': [{name, line, signature}, ...],
            'classes': [{name, line}, ...],
            'sections': [{name, line, level}, ...],
            'todos': [{line, text}, ...],
            'enums': [{name, line}, ...],
            'namespaces': [{name, line}, ...],
        }
    """
    project_dir = Path(project_dir)
    full_path = (project_dir / file_path).resolve()

    result: Dict[str, Any] = {
        'file': file_path,
        'language': _get_language(file_path),
        'line_count': 0,
        'size_bytes': 0,
        'functions': [],
        'classes': [],
        'sections': [],
        'todos': [],
        'enums': [],
        'namespaces': [],
    }

    if not full_path.is_file():
        result['error'] = f'File not found: {file_path}'
        return result

    try:
        result['size_bytes'] = full_path.stat().st_size
    except OSError:
        pass

    content = _read_file_safe(full_path)
    if content is None:
        result['error'] = 'File too large or unreadable'
        return result

    lines = content.splitlines()
    result['line_count'] = len(lines)

    # Extract all GUID markers from this file
    result['guids'] = re.findall(r'(?:#|//)\s*GUID:\s*([\w-]+)', content)

    # Extract TODOs and FIXMEs from any language
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if 'TODO' in stripped or 'FIXME' in stripped or 'HACK' in stripped or 'XXX' in stripped:
            # Extract the comment text
            for prefix in ('//', '#', '--', '/*', '<!--', ';', '%'):
                idx = stripped.find(prefix)
                if idx >= 0:
                    comment = stripped[idx:].lstrip('/#*-<!;% ')
                    result['todos'].append({'line': i, 'text': comment[:120]})
                    break
            else:
                result['todos'].append({'line': i, 'text': stripped[:120]})

    lang = result['language']

    # Dispatch to language-specific parsers
    if lang in ('cpp', 'c'):
        _parse_cpp(content, result)
    elif lang == 'python':
        _parse_python(content, lines, result)
    elif lang == 'markdown':
        _parse_markdown(content, result)
    elif lang == 'json':
        _parse_json_structure(content, result)
    elif lang == 'rust':
        _parse_generic(content, result, _RUST_PATTERNS)
    elif lang == 'go':
        _parse_generic(content, result, _GO_PATTERNS)
    elif lang in ('java', 'csharp', 'kotlin'):
        _parse_generic(content, result, _JAVA_PATTERNS)
    elif lang in ('javascript', 'typescript'):
        _parse_js_ts(content, result)

    return result


def _parse_cpp(content: str, result: dict):
    """Parse C/C++ file for classes, functions, enums, namespaces."""
    for m in _CPP_PATTERNS['class'].finditer(content):
        name = m.group(1)
        line = content[:m.start()].count('\n') + 1
        result['classes'].append({'name': name, 'line': line})

    for m in _CPP_PATTERNS['function'].finditer(content):
        name = m.group(1)
        # Skip common false positives
        if name in ('if', 'while', 'for', 'switch', 'return', 'catch',
                     'sizeof', 'decltype', 'alignof', 'noexcept', 'throw',
                     'delete', 'new', 'else'):
            continue
        params = m.group(2).strip()
        line = content[:m.start()].count('\n') + 1
        sig = f"{name}({params})"
        if len(sig) > 120:
            sig = sig[:117] + "..."
        result['functions'].append({'name': name, 'line': line, 'signature': sig})

    for m in _CPP_PATTERNS['enum'].finditer(content):
        name = m.group(1)
        line = content[:m.start()].count('\n') + 1
        result['enums'].append({'name': name, 'line': line})

    for m in _CPP_PATTERNS['namespace'].finditer(content):
        name = m.group(1)
        line = content[:m.start()].count('\n') + 1
        result['namespaces'].append({'name': name, 'line': line})


def _parse_python(content: str, lines: list, result: dict):
    """Parse Python file for classes, functions, module-level constants."""
    for m in _PY_PATTERNS['class'].finditer(content):
        name = m.group(1)
        line = content[:m.start()].count('\n') + 1
        result['classes'].append({'name': name, 'line': line})

    for m in _PY_PATTERNS['function'].finditer(content):
        indent = m.group(1)
        name = m.group(2)
        line = content[:m.start()].count('\n') + 1
        # Determine if it's a method (indented) or module-level function
        symbol_type = 'method' if len(indent) > 0 else 'function'
        result['functions'].append({
            'name': name,
            'line': line,
            'type': symbol_type,
        })

    for m in _PY_PATTERNS['variable'].finditer(content):
        name = m.group(1)
        line = content[:m.start()].count('\n') + 1
        result['sections'].append({'name': name, 'line': line, 'level': 0})


def _parse_markdown(content: str, result: dict):
    """Parse Markdown file for section headers."""
    for m in _MD_PATTERNS['section'].finditer(content):
        level = len(m.group(1))
        name = m.group(2).strip()
        line = content[:m.start()].count('\n') + 1
        result['sections'].append({'name': name, 'line': line, 'level': level})


def _parse_json_structure(content: str, result: dict):
    """Parse JSON file for top-level keys."""
    try:
        data = json.loads(content)
        if isinstance(data, dict):
            for key in data:
                val = data[key]
                val_type = type(val).__name__
                if isinstance(val, list):
                    val_type = f"array[{len(val)}]"
                elif isinstance(val, dict):
                    val_type = f"object({len(val)} keys)"
                result['sections'].append({
                    'name': key,
                    'line': 1,
                    'level': 0,
                    'type': val_type,
                })
    except (json.JSONDecodeError, ValueError):
        pass


def _parse_js_ts(content: str, result: dict):
    """Parse JavaScript/TypeScript for classes and functions."""
    # Classes
    for m in re.finditer(r'^\s*(?:export\s+)?(?:abstract\s+)?class\s+([\w_]+)', content, re.MULTILINE):
        name = m.group(1)
        line = content[:m.start()].count('\n') + 1
        result['classes'].append({'name': name, 'line': line})

    # Functions (named, arrow, export)
    for m in re.finditer(
        r'^\s*(?:export\s+)?(?:async\s+)?function\s*\*?\s*([\w_]+)\s*\(',
        content, re.MULTILINE,
    ):
        name = m.group(1)
        line = content[:m.start()].count('\n') + 1
        result['functions'].append({'name': name, 'line': line})

    # const/let arrow functions
    for m in re.finditer(
        r'^\s*(?:export\s+)?(?:const|let|var)\s+([\w_]+)\s*=\s*(?:async\s+)?\(?',
        content, re.MULTILINE,
    ):
        name = m.group(1)
        line = content[:m.start()].count('\n') + 1
        # Only include if it looks like a function (arrow or function keyword follows)
        rest = content[m.end():m.end() + 100]
        if '=>' in rest or 'function' in rest:
            result['functions'].append({'name': name, 'line': line})

    # Interfaces (TypeScript)
    for m in re.finditer(r'^\s*(?:export\s+)?interface\s+([\w_]+)', content, re.MULTILINE):
        name = m.group(1)
        line = content[:m.start()].count('\n') + 1
        result['classes'].append({'name': name, 'line': line})


def _parse_generic(content: str, result: dict, patterns: dict):
    """Parse using provided regex patterns dict."""
    if 'class' in patterns:
        for m in patterns['class'].finditer(content):
            name = m.group(1)
            line = content[:m.start()].count('\n') + 1
            result['classes'].append({'name': name, 'line': line})

    if 'function' in patterns:
        for m in patterns['function'].finditer(content):
            name = m.group(1)
            line = content[:m.start()].count('\n') + 1
            result['functions'].append({'name': name, 'line': line})


def build_file_wiki(project_dir: Path, cache_path: Path = None) -> dict:
    """Build a comprehensive wiki index of all project files with metadata and GUIDs.
    
    Walks all project files, collects metadata (path, size, line count, language, 
    last modified), and scans for GUID markers. Results are cached to avoid 
    redundant scans if no files have changed.
    
    Args:
        project_dir: Root directory of the project to scan
        cache_path: Optional custom cache file path. Defaults to 
                   project_dir / '.automatica' / 'wiki_index.json'
    
    Returns:
        {
            'files': [
                {
                    'path': str,              # Relative path from project root
                    'size_bytes': int,
                    'line_count': int,
                    'language': str,
                    'last_modified': str,     # ISO format timestamp
                    'guids': [str, ...]       # List of GUIDs found in file
                },
                ...
            ],
            'generated_at': str,              # ISO format timestamp
            'total_files': int,
            'total_lines': int
        }
    """
    project_dir = Path(project_dir).resolve()
    
    # Determine cache path
    if cache_path is None:
        cache_path = project_dir / '.automatica' / 'wiki_index.json'
    else:
        cache_path = Path(cache_path)
    
    # Check if we can use cached data
    if cache_path.exists():
        try:
            cache_mtime = cache_path.stat().st_mtime
            
            # Check if any file in the project is newer than cache
            needs_rebuild = False
            for file_path in _walk_files(project_dir):
                try:
                    file_mtime = file_path.stat().st_mtime
                    if file_mtime > cache_mtime:
                        needs_rebuild = True
                        break
                except OSError:
                    continue
            
            # If cache is still valid, return it
            if not needs_rebuild:
                with open(cache_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except (OSError, json.JSONDecodeError):
            # Cache is corrupted or unreadable, rebuild
            pass
    
    # Build the wiki index
    files_data = []
    total_lines = 0
    guid_pattern = re.compile(r'(?:#|//)\s*GUID:\s*([\w-]+)')
    
    for file_path in _walk_files(project_dir):
        try:
            # Get file stats
            stat_info = file_path.stat()
            size_bytes = stat_info.st_size
            last_modified = datetime.fromtimestamp(stat_info.st_mtime).isoformat()
            
            # Get relative path
            rel_path = str(file_path.relative_to(project_dir))
            
            # Determine language
            language = _get_language(str(file_path))
            
            # Read file to count lines and extract GUIDs
            line_count = 0
            guids = []
            
            content = _read_file_safe(file_path)
            if content is not None:
                line_count = len(content.splitlines())
                # Extract all GUIDs
                guids = guid_pattern.findall(content)
            
            total_lines += line_count
            
            files_data.append({
                'path': rel_path,
                'size_bytes': size_bytes,
                'line_count': line_count,
                'language': language,
                'last_modified': last_modified,
                'guids': guids
            })
            
        except OSError:
            # Skip files that can't be read
            continue
    
    # Build result
    result = {
        'files': files_data,
        'generated_at': datetime.now().isoformat(),
        'total_files': len(files_data),
        'total_lines': total_lines
    }
    
    # Cache the result
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_path, 'w', encoding='utf-8') as f:
            json.dump(result, f, indent=2)
    except OSError:
        # Cache write failed, but still return the result
        pass
    
    return result


# ── Section/Content Reader ─────────────────────────────────────────


def get_file_section(project_dir: Path, file_path: str,
                     line_start: int = None, line_end: int = None,
                     section_name: str = None) -> dict:
    """Read actual file content by line range or section name.

    Args:
        project_dir: root project directory
        file_path: relative path to file
        line_start: 1-based start line (inclusive)
        line_end: 1-based end line (inclusive)
        section_name: find a named section (class, function, header) and return its content

    Returns:
        {content: str, line_start: int, line_end: int, total_lines: int}
    """
    project_dir = Path(project_dir)
    full_path = (project_dir / file_path).resolve()

    if not full_path.is_file():
        return {'error': f'File not found: {file_path}', 'content': '', 'total_lines': 0}

    content = _read_file_safe(full_path)
    if content is None:
        return {'error': 'File too large or unreadable', 'content': '', 'total_lines': 0}

    lines = content.splitlines(keepends=True)
    total = len(lines)

    if section_name:
        # Find the section by searching for the name in the file structure
        structure = get_file_structure(project_dir, file_path)

        # Search across all symbol types
        target_line = None
        for key in ('functions', 'classes', 'sections', 'enums', 'namespaces'):
            for sym in structure.get(key, []):
                if sym.get('name', '').lower() == section_name.lower():
                    target_line = sym['line']
                    break
            if target_line:
                break

        if target_line is None:
            return {
                'error': f'Section "{section_name}" not found in {file_path}',
                'content': '',
                'total_lines': total,
            }

        # Find the end of the section (next symbol at same or lower indent, or end of file)
        lang = _get_language(file_path)
        section_end = _find_section_end(lines, target_line - 1, lang)
        line_start = target_line
        line_end = section_end

    if line_start is not None:
        start_idx = max(0, line_start - 1)
        end_idx = min(total, line_end) if line_end is not None else total
        selected = lines[start_idx:end_idx]
        # Add line numbers
        numbered = "".join(
            f"{i + start_idx + 1:>6}  {line}"
            for i, line in enumerate(selected)
        )
        return {
            'content': numbered,
            'line_start': start_idx + 1,
            'line_end': start_idx + len(selected),
            'total_lines': total,
        }

    # No range specified — return entire file with line numbers
    numbered = "".join(f"{i + 1:>6}  {line}" for i, line in enumerate(lines))
    return {
        'content': numbered,
        'line_start': 1,
        'line_end': total,
        'total_lines': total,
    }


def _find_section_end(lines: list, start_idx: int, lang: str) -> int:
    """Find where a section ends based on brace/indent tracking."""
    if start_idx >= len(lines):
        return len(lines)

    # For brace-based languages, count brace depth
    if lang in ('cpp', 'c', 'java', 'csharp', 'javascript', 'typescript',
                'rust', 'go', 'kotlin', 'dart', 'swift'):
        depth = 0
        found_open = False
        for i in range(start_idx, len(lines)):
            line = lines[i]
            for ch in line:
                if ch == '{':
                    depth += 1
                    found_open = True
                elif ch == '}':
                    depth -= 1
                    if found_open and depth <= 0:
                        return i + 1  # 1-based
        return len(lines)

    # For indent-based languages (Python), use indent level
    if lang == 'python':
        # Get indent of the def/class line
        start_line = lines[start_idx] if start_idx < len(lines) else ''
        base_indent = len(start_line) - len(start_line.lstrip())
        for i in range(start_idx + 1, len(lines)):
            line = lines[i]
            stripped = line.strip()
            if not stripped:
                continue  # skip blank lines
            current_indent = len(line) - len(line.lstrip())
            if current_indent <= base_indent and stripped:
                return i  # 1-based (the line BEFORE this)
        return len(lines)

    # For markdown, find next header of same or lower level
    if lang == 'markdown':
        start_line = lines[start_idx] if start_idx < len(lines) else ''
        m = re.match(r'^(#{1,6})\s', start_line)
        if m:
            level = len(m.group(1))
            for i in range(start_idx + 1, len(lines)):
                hm = re.match(r'^(#{1,6})\s', lines[i])
                if hm and len(hm.group(1)) <= level:
                    return i
        return len(lines)

    # Default: return next 50 lines or end of file
    return min(start_idx + 50, len(lines))


# ── Symbol Search ──────────────────────────────────────────────────


def search_symbols(project_dir: Path, name: str = None,
                   symbol_type: str = None, file_glob: str = None,
                   limit: int = 30) -> list:
    """Scan files and find symbols matching criteria.

    Args:
        project_dir: root directory
        name: symbol name or substring to match (case-insensitive)
        symbol_type: filter by type (function, class, enum, namespace, section, todo)
        file_glob: glob pattern to filter files (e.g. '*.cpp', 'engine/**/*.hpp')
        limit: max results

    Returns:
        List of {file, name, type, line, signature?}
    """
    project_dir = Path(project_dir).resolve()
    results: List[dict] = []
    name_lower = name.lower() if name else None

    for file_path in _walk_files(project_dir, file_glob):
        rel_path = str(file_path.relative_to(project_dir)).replace('\\', '/')
        lang = _get_language(rel_path)
        if not lang or lang in ('json', 'text', 'ini'):
            continue

        structure = get_file_structure(project_dir, rel_path)

        # Collect symbols by type
        type_map = {
            'function': structure.get('functions', []),
            'class': structure.get('classes', []),
            'enum': structure.get('enums', []),
            'namespace': structure.get('namespaces', []),
            'section': structure.get('sections', []),
            'todo': structure.get('todos', []),
        }

        for stype, symbols in type_map.items():
            if symbol_type and stype != symbol_type:
                continue
            for sym in symbols:
                sym_name = sym.get('name', sym.get('text', ''))
                if name_lower and name_lower not in sym_name.lower():
                    continue

                entry = {
                    'file': rel_path,
                    'name': sym_name,
                    'type': stype,
                    'line': sym.get('line', 0),
                }
                if 'signature' in sym:
                    entry['signature'] = sym['signature']
                results.append(entry)

                if len(results) >= limit:
                    return results

    return results


# ── File Listing ───────────────────────────────────────────────────


def list_project_files(project_dir: Path, extension: str = None,
                       path_contains: str = None, limit: int = 50) -> list:
    """Walk directory tree and return file metadata from disk.

    Args:
        project_dir: root directory
        extension: filter by extension (e.g. '.cpp', '.py')
        path_contains: filter by substring in relative path
        limit: max results

    Returns:
        List of {path, extension, language, size_bytes, line_count}
    """
    project_dir = Path(project_dir).resolve()
    results: List[dict] = []

    # Build a glob pattern if extension is specified
    file_glob = f"*{extension}" if extension else None

    for file_path in _walk_files(project_dir, file_glob):
        rel_path = str(file_path.relative_to(project_dir)).replace('\\', '/')

        if path_contains and path_contains.lower() not in rel_path.lower():
            continue

        ext = file_path.suffix.lower()
        lang = LANGUAGE_MAP.get(ext, '')

        _stat = None
        try:
            _stat = file_path.stat()
            size = _stat.st_size
        except OSError:
            size = 0

        # Quick line count (read first chunk for estimation if large)
        line_count = 0
        if size <= MAX_PARSE_SIZE and ext not in BINARY_EXTS:
            try:
                with open(file_path, 'rb') as f:
                    line_count = sum(1 for _ in f)
            except OSError:
                pass

        results.append({
            'path': rel_path,
            'extension': ext,
            'language': lang,
            'size_bytes': size,
            'line_count': line_count,
            'last_modified': (datetime.fromtimestamp(_stat.st_mtime).strftime('%Y-%m-%dT%H:%M:%S')
                              if _stat else ''),
            'mtime_epoch': _stat.st_mtime if _stat else 0.0,
        })

        if len(results) >= limit:
            break

    return results


def build_file_wiki(project_dir: Path, cache_path: Path = None) -> dict:
    """Build a compact wiki index of all source files in a project.

    Scans every file, collecting path, size, line_count, language, last_modified,
    and any GUID markers (``# GUID:`` / ``// GUID:`` patterns).

    Results are cached to ``.automatica/wiki_index.json``; the cache is skipped
    when any source file is newer than the cache file itself.

    Returns::

        {
            "files": [{"path", "size_bytes", "line_count", "language",
                       "last_modified", "guids": [...]}, ...],
            "generated_at": ISO string,
            "total_files": int,
            "total_lines": int,
        }
    """
    project_dir = Path(project_dir).resolve()
    _cache = cache_path or (project_dir / ".automatica" / "wiki_index.json")

    # Check cache freshness: valid if cache exists and no source file is newer
    if _cache.exists():
        cache_mtime = _cache.stat().st_mtime
        stale = False
        for fp in _walk_files(project_dir):
            try:
                if fp.stat().st_mtime > cache_mtime:
                    stale = True
                    break
            except OSError:
                pass
        if not stale:
            try:
                return json.loads(_cache.read_text(encoding="utf-8"))
            except Exception:
                pass  # fall through to rebuild

    _guid_re = re.compile(r'(?:#|//)\s*GUID:\s*([\w\-]+)', re.IGNORECASE)
    files_data: list[dict] = []
    total_lines = 0

    for file_path in _walk_files(project_dir):
        rel_path = str(file_path.relative_to(project_dir)).replace("\\", "/")
        ext  = file_path.suffix.lower()
        lang = LANGUAGE_MAP.get(ext, "")
        _stat = None
        size  = 0
        try:
            _stat = file_path.stat()
            size  = _stat.st_size
        except OSError:
            pass

        line_count = 0
        guids: list[str] = []
        if size <= MAX_PARSE_SIZE and ext not in BINARY_EXTS:
            try:
                content = file_path.read_text(encoding="utf-8", errors="replace")
                line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
                guids = _guid_re.findall(content)
            except OSError:
                pass

        total_lines += line_count
        files_data.append({
            "path":          rel_path,
            "size_bytes":    size,
            "line_count":    line_count,
            "language":      lang,
            "last_modified": (datetime.fromtimestamp(_stat.st_mtime).strftime("%Y-%m-%dT%H:%M:%S")
                              if _stat else ""),
            "mtime_epoch":   _stat.st_mtime if _stat else 0.0,
            "guids":         guids,
        })

    # Sort by mtime descending (most recently changed first)
    files_data.sort(key=lambda f: f["mtime_epoch"], reverse=True)

    wiki = {
        "files":        files_data,
        "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "total_files":  len(files_data),
        "total_lines":  total_lines,
    }

    try:
        _cache.parent.mkdir(parents=True, exist_ok=True)
        _cache.write_text(json.dumps(wiki, indent=2), encoding="utf-8")
    except Exception:
        pass  # cache write is best-effort

    return wiki


# ── JSON Key Extraction ────────────────────────────────────────────


def get_json_keys(project_dir: Path, file_path: str) -> dict:
    """Parse a JSON file and return its top-level keys with types and previews.

    Returns:
        {
            'file': str,
            'keys': [{key, type, preview}, ...],
            'error': str (if any),
        }
    """
    project_dir = Path(project_dir)
    full_path = (project_dir / file_path).resolve()

    result = {'file': file_path, 'keys': []}

    if not full_path.is_file():
        result['error'] = f'File not found: {file_path}'
        return result

    content = _read_file_safe(full_path)
    if content is None:
        result['error'] = 'File too large or unreadable'
        return result

    try:
        data = json.loads(content)
    except (json.JSONDecodeError, ValueError) as exc:
        result['error'] = f'JSON parse error: {exc}'
        return result

    if isinstance(data, dict):
        for key, val in data.items():
            entry = {'key': key}
            if isinstance(val, dict):
                entry['type'] = 'object'
                entry['preview'] = f'{{{len(val)} keys: {", ".join(list(val.keys())[:5])}...}}'
            elif isinstance(val, list):
                entry['type'] = 'array'
                entry['preview'] = f'[{len(val)} items]'
            elif isinstance(val, str):
                entry['type'] = 'string'
                entry['preview'] = val[:80] + ('...' if len(val) > 80 else '')
            elif isinstance(val, bool):
                entry['type'] = 'boolean'
                entry['preview'] = str(val).lower()
            elif isinstance(val, (int, float)):
                entry['type'] = 'number'
                entry['preview'] = str(val)
            elif val is None:
                entry['type'] = 'null'
                entry['preview'] = 'null'
            else:
                entry['type'] = type(val).__name__
                entry['preview'] = str(val)[:80]
            result['keys'].append(entry)
    elif isinstance(data, list):
        result['keys'].append({
            'key': '(root)',
            'type': 'array',
            'preview': f'[{len(data)} items]',
        })
    else:
        result['keys'].append({
            'key': '(root)',
            'type': type(data).__name__,
            'preview': str(data)[:80],
        })

    return result


# ── Internal Helpers ───────────────────────────────────────────────


def _walk_files(project_dir: Path, file_glob: str = None, sort_by_mtime: bool = False):
    """Walk project directory yielding file paths, respecting skip rules.

    Yields Path objects for each matching file.
    If sort_by_mtime is True, collects all paths first then yields them sorted
    by modification time descending (most recently modified first).
    """
    project_dir = Path(project_dir).resolve()

    def _collect():
        for dirpath, dirnames, filenames in os.walk(project_dir):
            # Filter out skipped directories in-place (modifying dirnames affects os.walk)
            dirnames[:] = [d for d in dirnames if not _should_skip(d, True)]

            for fname in filenames:
                if _should_skip(fname, False):
                    continue

                if file_glob and not fnmatch.fnmatch(fname, file_glob):
                    continue

                yield Path(dirpath) / fname

    if sort_by_mtime:
        all_paths = list(_collect())
        all_paths.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
        yield from all_paths
    else:
        yield from _collect()


# ── Self-test ──────────────────────────────────────────────────────

if __name__ == "__main__":
    import tempfile

    print("=== Project Scanner self-test ===\n")

    with tempfile.TemporaryDirectory() as tmpdir:
        project = Path(tmpdir)

        # Create test C++ file
        cpp_dir = project / "engine" / "core"
        cpp_dir.mkdir(parents=True)
        (cpp_dir / "Engine.hpp").write_text(
            '#pragma once\n'
            '#include <string>\n'
            '\n'
            'namespace Nova {\n'
            '\n'
            'enum class RenderMode { Forward, Deferred };\n'
            '\n'
            '/// The main engine class\n'
            'class Engine {\n'
            'public:\n'
            '    Engine();\n'
            '    ~Engine();\n'
            '\n'
            '    void initialize(int width, int height) {\n'
            '        // TODO: Add fullscreen support\n'
            '    }\n'
            '\n'
            '    void update(float dt) {\n'
            '        // FIXME: Handle frame drops\n'
            '    }\n'
            '\n'
            '    void render() {\n'
            '    }\n'
            '\n'
            'private:\n'
            '    int m_width;\n'
            '};\n'
            '\n'
            '} // namespace Nova\n',
            encoding='utf-8',
        )

        # Create test Python file
        (project / "tool.py").write_text(
            '#!/usr/bin/env python3\n'
            'MAX_RETRIES = 3\n'
            '\n'
            'class Builder:\n'
            '    def build(self, target):\n'
            '        pass\n'
            '\n'
            '    def clean(self):\n'
            '        # TODO: Implement proper cleanup\n'
            '        pass\n'
            '\n'
            'def main():\n'
            '    b = Builder()\n'
            '    b.build("all")\n',
            encoding='utf-8',
        )

        # Create test JSON file
        (project / "config.json").write_text(
            json.dumps({
                "name": "TestProject",
                "version": "1.0.0",
                "features": ["audio", "physics"],
                "build": {"type": "cmake", "flags": ["-O2"]},
            }, indent=2),
            encoding='utf-8',
        )

        # Create test Markdown file
        (project / "README.md").write_text(
            '# Test Project\n\n## Getting Started\n\nSome text.\n\n'
            '## Build Instructions\n\nMore text.\n\n### Prerequisites\n\nStuff.\n',
            encoding='utf-8',
        )

        errors = []

        # Test get_file_structure (C++)
        result = get_file_structure(project, "engine/core/Engine.hpp")
        if result.get('error'):
            errors.append(f"C++ parse error: {result['error']}")
        if not any(c['name'] == 'Engine' for c in result.get('classes', [])):
            errors.append("C++ parse: Engine class not found")
        if not any(f['name'] == 'initialize' for f in result.get('functions', [])):
            errors.append("C++ parse: initialize function not found")
        if not any(e['name'] == 'RenderMode' for e in result.get('enums', [])):
            errors.append("C++ parse: RenderMode enum not found")
        if not any(n['name'] == 'Nova' for n in result.get('namespaces', [])):
            errors.append("C++ parse: Nova namespace not found")
        if not result.get('todos'):
            errors.append("C++ parse: TODOs not found")
        print(f"[{'FAIL' if any('C++' in e for e in errors) else 'OK'}] C++ file structure")

        # Test get_file_structure (Python)
        result = get_file_structure(project, "tool.py")
        if not any(c['name'] == 'Builder' for c in result.get('classes', [])):
            errors.append("Python parse: Builder class not found")
        if not any(f['name'] == 'main' for f in result.get('functions', [])):
            errors.append("Python parse: main function not found")
        print(f"[{'FAIL' if any('Python' in e for e in errors) else 'OK'}] Python file structure")

        # Test get_file_structure (JSON)
        result = get_file_structure(project, "config.json")
        keys = [s['name'] for s in result.get('sections', [])]
        if 'name' not in keys or 'features' not in keys:
            errors.append(f"JSON parse: expected keys not found, got {keys}")
        print(f"[{'FAIL' if any('JSON' in e for e in errors) else 'OK'}] JSON file structure")

        # Test get_file_structure (Markdown)
        result = get_file_structure(project, "README.md")
        sections = [s['name'] for s in result.get('sections', [])]
        if 'Test Project' not in sections:
            errors.append(f"Markdown parse: sections not found, got {sections}")
        print(f"[{'FAIL' if any('Markdown' in e for e in errors) else 'OK'}] Markdown file structure")

        # Test get_file_section (line range)
        result = get_file_section(project, "engine/core/Engine.hpp", line_start=8, line_end=12)
        if 'Engine' not in result.get('content', ''):
            errors.append("Section read: Engine not in line range")
        print(f"[{'FAIL' if any('Section read' in e for e in errors) else 'OK'}] File section (line range)")

        # Test get_file_section (by name)
        result = get_file_section(project, "engine/core/Engine.hpp", section_name="initialize")
        if 'initialize' not in result.get('content', ''):
            errors.append("Section by name: initialize not found")
        print(f"[{'FAIL' if any('Section by name' in e for e in errors) else 'OK'}] File section (by name)")

        # Test search_symbols
        results = search_symbols(project, name='Engine', symbol_type='class')
        if not results:
            errors.append("Symbol search: Engine class not found")
        print(f"[{'FAIL' if any('Symbol search' in e for e in errors) else 'OK'}] Symbol search")

        # Test list_project_files
        files = list_project_files(project, extension='.hpp')
        if not files:
            errors.append("File listing: no .hpp files found")
        print(f"[{'FAIL' if any('File listing' in e for e in errors) else 'OK'}] File listing")

        files_with_path = list_project_files(project, path_contains='engine')
        if not files_with_path:
            errors.append("File listing path_contains: no engine files found")
        print(f"[{'FAIL' if any('path_contains' in e for e in errors) else 'OK'}] File listing (path_contains)")

        # Test get_json_keys
        result = get_json_keys(project, "config.json")
        key_names = [k['key'] for k in result.get('keys', [])]
        if 'name' not in key_names or 'build' not in key_names:
            errors.append(f"JSON keys: expected keys not found, got {key_names}")
        print(f"[{'FAIL' if any('JSON keys' in e for e in errors) else 'OK'}] JSON key extraction")

        # Test nonexistent file
        result = get_file_structure(project, "nonexistent.cpp")
        if not result.get('error'):
            errors.append("Nonexistent file: should have error")
        print(f"[{'FAIL' if any('Nonexistent' in e for e in errors) else 'OK'}] Nonexistent file handling")

        if errors:
            print(f"\nSELF-TEST FAILED ({len(errors)} errors):")
            for e in errors:
                print(f"  - {e}")
        else:
            print("\n=== All tests passed ===")


# ═══════════════════════════════════════════════════════════════════
# GUID-Tagged Code Section Scanning
# ═══════════════════════════════════════════════════════════════════

import re as _re

# GUID marker format: Start:PersistentGUID:CurrentGUID -- Description
# Legacy format:      Start:CurrentGUID - Description (no persistent_id)
# Supports /* */ (C/C++/Java/CSS) and # (Python/Shell/YAML) comment styles
# ID pattern: alphanumeric + hyphens (UUIDs, custom IDs, etc.)
_ID = r'[a-zA-Z0-9][\w-]*'
_GUID_RE = _re.compile(
    r'(?:'
    rf'/\*\s*Start:({_ID}):({_ID})\s*--?\s*(.*?)\*/'             # /*Start:PID:GUID -- desc*/
    rf'|/\*\s*Start:({_ID})\s*--?\s*(.*?)\*/'                    # /*Start:GUID - desc*/ (legacy)
    rf'|#\s*Start:({_ID}):({_ID})\s*--?\s*(.*)'                  # # Start:PID:GUID -- desc
    rf'|#\s*Start:({_ID})\s*--?\s*(.*)'                           # # Start:GUID - desc (legacy)
    r')', _re.IGNORECASE
)
_GUID_END_RE = _re.compile(
    r'(?:'
    rf'/\*\s*End:(?:{_ID}:)?({_ID})\s*\*/'                       # /*End:[PID:]GUID*/
    rf'|#\s*End:(?:{_ID}:)?({_ID})'                               # # End:[PID:]GUID
    r')', _re.IGNORECASE
)


def _parse_guid_match(m):
    """Extract (persistent_id, guid, description) from a _GUID_RE match.
    Groups: 1=PID,2=GUID,3=desc (C PID:GUID) | 4=GUID,5=desc (C legacy)
            6=PID,7=GUID,8=desc (# PID:GUID) | 9=GUID,10=desc (# legacy)"""
    if m.group(1) and m.group(2):
        return m.group(1), m.group(2), m.group(3) or ''
    if m.group(4):
        return None, m.group(4), m.group(5) or ''
    if m.group(6) and m.group(7):
        return m.group(6), m.group(7), m.group(8) or ''
    if m.group(9):
        return None, m.group(9), m.group(10) or ''
    return None, '', ''


def find_guid_section(project_dir, guid, db_conn=None):
    """Cascading search for a GUID in files. Updates DB if found at new location."""
    from pathlib import Path
    project_dir = Path(project_dir)

    # Step 1: Try DB cached location first
    record = None
    if db_conn:
        try:
            from automatica_db import get_code_section, update_section_location, clear_section_location
            record = get_code_section(db_conn, guid)
        except ImportError:
            pass

    if record and record.get('file_path') and record.get('line_start'):
        result = _check_file_at_line(project_dir, record['file_path'], record['line_start'], guid)
        if result:
            return result
        # Step 2: Search whole file
        result = _search_file_for_guid(project_dir, record['file_path'], guid)
        if result:
            if db_conn:
                update_section_location(db_conn, guid, result['line_start'], result['line_end'], result['file_path'])
            return result

    # Step 3: Search by filename
    if record and record.get('file_path'):
        filename = Path(record['file_path']).name
        for found in project_dir.rglob(filename):
            if found.is_file():
                rel = str(found.relative_to(project_dir))
                result = _search_file_for_guid(project_dir, rel, guid)
                if result:
                    if db_conn:
                        update_section_location(db_conn, guid, result['line_start'], result['line_end'], result['file_path'])
                    return result

    # Step 4: Full project search
    for fpath in _walk_text_files(project_dir):
        result = _search_file_for_guid(project_dir, fpath, guid)
        if result:
            if db_conn:
                update_section_location(db_conn, guid, result['line_start'], result['line_end'], result['file_path'])
            return result

    # Step 5: Not found
    if db_conn and record:
        clear_section_location(db_conn, guid)
    return None


def _check_file_at_line(project_dir, file_path, line_start, guid):
    """Quick check: is the GUID at the expected file+line?"""
    from pathlib import Path
    fp = Path(project_dir) / file_path
    if not fp.is_file():
        return None
    try:
        lines = fp.read_text(encoding='utf-8', errors='ignore').splitlines()
        if line_start and 0 < line_start <= len(lines):
            if guid.lower() in lines[line_start - 1].lower():
                return _extract_section(file_path, lines, line_start - 1, guid)
    except Exception:
        pass
    return None


def _search_file_for_guid(project_dir, file_path, guid):
    """Search a single file for a GUID marker."""
    from pathlib import Path
    fp = Path(project_dir) / file_path
    if not fp.is_file():
        return None
    try:
        text = fp.read_text(encoding='utf-8', errors='ignore')
        if guid.lower() not in text.lower():
            return None
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if guid.lower() in line.lower() and ('start:' in line.lower()):
                return _extract_section(file_path, lines, i, guid)
    except Exception:
        pass
    return None


def _extract_section(file_path, lines, start_idx, guid):
    """Extract content between Start:[PID:]GUID and End:[PID:]GUID markers."""
    start_line = lines[start_idx]
    desc = ''
    persistent_id = None
    current_guid = guid

    m = _GUID_RE.search(start_line)
    if m:
        persistent_id, current_guid, desc = _parse_guid_match(m)

    # Find End marker (matches either GUID or PID:GUID)
    end_idx = start_idx
    end_guid_re = _re.compile(r'End:\s*(?:[a-f0-9-]+:)?' + _re.escape(current_guid), _re.IGNORECASE)
    for j in range(start_idx + 1, min(start_idx + 5000, len(lines))):
        if end_guid_re.search(lines[j]):
            end_idx = j
            break

    content = '\n'.join(lines[start_idx + 1:end_idx])
    return {
        'guid': current_guid,
        'persistent_id': persistent_id,
        'file_path': file_path,
        'line_start': start_idx + 1,
        'line_end': end_idx + 1,
        'content': content,
        'description': desc.strip(),
    }


def _walk_text_files(project_dir):
    """Yield relative paths of text files, skipping binaries and build dirs."""
    from pathlib import Path
    skip = {'.git', '.svn', 'node_modules', '__pycache__', '.vs', 'build', 'dist', '_Discarded', '.automatica'}
    binary = {'.exe', '.dll', '.o', '.obj', '.lib', '.pdb', '.png', '.jpg', '.gif', '.zip', '.tar', '.gz', '.db'}
    for p in Path(project_dir).rglob('*'):
        if any(s in p.parts for s in skip):
            continue
        if p.is_file() and p.suffix.lower() not in binary:
            yield str(p.relative_to(project_dir))


def scan_guid_markers(project_dir, file_path=None):
    """Scan file(s) for all Start:/End: GUID markers."""
    from pathlib import Path
    project_dir = Path(project_dir)
    results = []

    if file_path:
        files = [file_path]
    else:
        files = list(_walk_text_files(project_dir))

    for fp in files:
        full = project_dir / fp
        if not full.is_file():
            continue
        try:
            lines = full.read_text(encoding='utf-8', errors='ignore').splitlines()
        except Exception:
            continue
        for i, line in enumerate(lines):
            m = _GUID_RE.search(line)
            if m:
                pid, guid, desc = _parse_guid_match(m)
                # Find matching End
                end_line = i
                end_re = _re.compile(r'End:\s*(?:[\w-]+:)?' + _re.escape(guid), _re.IGNORECASE)
                for j in range(i + 1, min(i + 5000, len(lines))):
                    if end_re.search(lines[j]):
                        end_line = j
                        break
                results.append({
                    'guid': guid,
                    'persistent_id': pid,
                    'description': desc.strip(),
                    'file_path': fp,
                    'line_start': i + 1,
                    'line_end': end_line + 1,
                })
    return results


def get_section_content(project_dir, guid, db_conn=None):
    """Get just the code between Start/End markers."""
    result = find_guid_section(project_dir, guid, db_conn)
    return result['content'] if result else None
