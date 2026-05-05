"""
summary_indexer — change-driven, in-source code summaries.

Generates and maintains *summaries that live in the code itself* (docstrings,
file headers, _folder_summary.md, README.md auto-section). The indexer only
touches comment / docstring nodes — never logic. After every rewrite the
Python AST is compared to the pre-rewrite version and, if any non-docstring
node changed, the rewrite is rolled back.

Refresh model
-------------
Change-driven, not sprint-walk:

* Hook ``enqueue_for_refresh(path)`` from agent_executor's write_file /
  edit_file handlers. The file's path lands in the queue.
* ``drain(provider_config)`` is called at end of sprint. Only files whose
  non-comment hash changed since the last summary actually call the LLM.
* Folder summary regenerates only when a child file summary changed; project
  summary regenerates only when a folder summary changed. Cascade is bounded
  by the queue depth.

Hard guardrails
---------------
* Python rewrites use ``ast.parse`` + ``ast.unparse``. Pre and post are
  compared with module-docstrings and function/class docstrings stripped —
  if any non-docstring node differs the rewrite is discarded.
* C++/Markdown/JSON paths are regex-only and *only* replace existing comment
  blocks; they never insert into code positions.
* If the LLM call fails or returns garbage, the file is left untouched.
"""
from __future__ import annotations

import ast
import hashlib
import json
import logging
import re
import textwrap
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

log = logging.getLogger(__name__)

# Sentinels embedded in generated comment blocks. Their presence tells the
# indexer "this is *our* comment, safe to replace next refresh"; absence means
# the existing docstring was hand-written and we leave it alone (or upgrade
# it conservatively only if it's clearly stale).
SENTINEL_BEGIN = "<!-- automatica:summary -->"
SENTINEL_END   = "<!-- /automatica:summary -->"
SUMMARY_MAGIC  = "@automatica-summary"

# Files we never summarise — too noisy or actively dangerous to touch.
SKIP_PATTERNS = (
    "__pycache__", ".git", ".automatica", ".claude", ".venv", "venv", "env",
    "node_modules", "build", "dist", "site-packages", "logs",
    ".pytest_cache", ".mypy_cache", ".ruff_cache",
)
# Files that are always safe to summarise.
SUMMARISABLE_EXTS = {".py", ".pyi", ".md"}
# C/C++/JS/TS support is regex-only; opt-in via config to avoid false edits.
RISKY_EXTS = {".cpp", ".hpp", ".h", ".cc", ".c", ".js", ".ts", ".tsx"}


# ---------------------------------------------------------------------------
# Hashing — used to skip files that haven't changed since last summary
# ---------------------------------------------------------------------------

def _strip_python_docstrings(src: str) -> str:
    """Return the source with all docstrings *removed* (not blanked).
    Two files that differ only in docstrings produce identical output —
    the structural fingerprint used by the guardrail. Empty bodies get a
    ``pass`` to preserve syntactic validity for re-parsing."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return src  # leave untouched; caller treats as opaque

    class _Stripper(ast.NodeTransformer):
        def _strip(self, node: ast.AST) -> ast.AST:
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body.pop(0)
                # Empty body would be invalid Python after re-emit; insert
                # a Pass so ast.unparse produces parseable output.
                if not body and isinstance(node, (ast.FunctionDef,
                                                    ast.AsyncFunctionDef,
                                                    ast.ClassDef)):
                    body.append(ast.Pass())
            return self.generic_visit(node)
        visit_Module     = _strip
        visit_FunctionDef = _strip
        visit_AsyncFunctionDef = _strip
        visit_ClassDef    = _strip

    try:
        return ast.unparse(_Stripper().visit(tree))
    except Exception:
        return src


def _content_hash(path: Path) -> str:
    """Hash a file's *non-comment* content. Two files differing only in
    docstrings produce the same hash so summary refresh is skipped."""
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    if path.suffix.lower() in {".py", ".pyi"}:
        src = _strip_python_docstrings(src)
    return hashlib.sha256(src.encode("utf-8", errors="replace")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# LLM call — kept simple, mirrors http_local_chat.send approach
# ---------------------------------------------------------------------------

def _ollama_summarise(prompt: str, *, endpoint: str, model: str,
                       timeout: int = 60, max_tokens: int = 256) -> str:
    """Call Ollama /api/generate and return raw text. Empty string on any
    failure — caller treats empty as 'leave the file alone'."""
    body = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"num_predict": max_tokens, "temperature": 0.2},
    }).encode()
    req = urllib.request.Request(
        f"{endpoint.rstrip('/')}/api/generate",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
            return (data.get("response") or "").strip()
    except Exception as exc:
        log.warning("Ollama summarise call failed: %s", exc)
        return ""


# ---------------------------------------------------------------------------
# Python file summarisation — module docstring only, AST-guarded
# ---------------------------------------------------------------------------

_KEYWORDS_RE = re.compile(r"\b([a-zA-Z][a-zA-Z0-9_-]{2,30})\b")


def _extract_keywords(text: str, limit: int = 8) -> list[str]:
    """Cheap keyword pull from a summary blurb — used for relevance ranking."""
    seen: set[str] = set()
    out: list[str] = []
    for m in _KEYWORDS_RE.findall(text.lower()):
        if m in seen or len(m) < 4:
            continue
        if m in {"the", "and", "for", "with", "this", "that", "from",
                 "into", "when", "uses", "used", "returns", "function",
                 "class", "module", "file", "code", "implementation"}:
            continue
        seen.add(m)
        out.append(m)
        if len(out) >= limit:
            break
    return out


def _build_python_summary_prompt(path: Path, src: str, signatures: list[str]) -> str:
    """Compose the prompt asked of the local LLM. Cheap, deterministic, no
    chain-of-thought theatrics."""
    head = src[:2000]
    sig_block = "\n".join(f"- {s}" for s in signatures[:20])
    return textwrap.dedent(f"""\
        You are documenting a Python source file for a code-search index.
        File path: {path}

        Top-of-file source (first 2000 chars):
        ---
        {head}
        ---

        Detected symbols (signatures):
        {sig_block or '(none extracted)'}

        Write a module-level docstring covering ONLY:
          1. One sentence: what this module does.
          2. One sentence: when another module would import or call it.
          3. A "Key symbols:" line listing 1-5 most-important public names.
          4. A "Keywords:" line — 5-8 single-word keywords (lowercase,
             space-separated) useful for searching.

        Output rules — VERY IMPORTANT:
          - Output ONLY the docstring body text. No triple quotes.
          - No markdown headers, no code fences, no preamble like
            "Here is the docstring".
          - Plain English. Under 120 words.
          - Do NOT invent symbols not listed above.""")


def _extract_python_signatures(tree: ast.AST) -> list[str]:
    sigs: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = ", ".join(a.arg for a in node.args.args)
            sigs.append(f"def {node.name}({args})")
        elif isinstance(node, ast.ClassDef):
            sigs.append(f"class {node.name}")
    return sigs


def _python_module_docstring(tree: ast.Module) -> Optional[ast.Expr]:
    body = tree.body
    if not body:
        return None
    first = body[0]
    if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)):
        return first
    return None


def _ast_dump_no_docstrings(src: str) -> str:
    """ast.dump for the source with all docstrings stripped. Used as the
    structural-equivalence fingerprint of the file's non-comment content.
    Two strings equal here ⇒ logic is identical."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return src  # if it doesn't parse, fall through; caller skips rewrite
    stripped = _strip_python_docstrings(src)
    try:
        return ast.dump(ast.parse(stripped), annotate_fields=False)
    except SyntaxError:
        return stripped


def summarise_python_file(path: Path, *, endpoint: str, model: str,
                           force: bool = False) -> bool:
    """Generate or refresh the module docstring for a Python file.

    Returns True if the file was rewritten, False if skipped (no change
    needed, parse failure, LLM failure, or guardrail tripped).
    """
    if not path.is_file():
        return False
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return False
    if not src.strip():
        return False
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return False  # don't rewrite syntactically broken files

    existing_doc = _python_module_docstring(tree)
    existing_text = ""
    is_ours = False
    if existing_doc is not None:
        existing_text = existing_doc.value.value or ""
        is_ours = SUMMARY_MAGIC in existing_text

    # Hash the non-comment content. If it matches the hash embedded in our
    # last-generated summary, nothing about the LOGIC changed — skip the LLM.
    cur_hash = _content_hash(path)
    if not force and is_ours:
        m = re.search(rf"{re.escape(SUMMARY_MAGIC)} hash=([0-9a-f]{{8,32}})",
                      existing_text)
        if m and m.group(1) == cur_hash:
            return False  # cache hit, nothing to do

    # Conservative rule: if a hand-written docstring is present and we don't
    # own it, don't touch it. (Future: opt-in upgrade prompt.)
    if existing_doc is not None and not is_ours:
        return False

    # Build prompt + call LLM.
    signatures = _extract_python_signatures(tree)
    prompt = _build_python_summary_prompt(path, src, signatures)
    raw = _ollama_summarise(prompt, endpoint=endpoint, model=model)
    if not raw or len(raw) < 20:
        return False

    # Tag the body with our magic + hash so next refresh can self-skip.
    body = raw.strip().strip('"').strip("'").strip()
    keywords = _extract_keywords(body)
    summary_block = (
        f"{body}\n\n"
        f"{SUMMARY_MAGIC} hash={cur_hash} "
        f"keywords={','.join(keywords)} "
        f"generated={datetime.now().isoformat(timespec='seconds')}"
    )

    # Build the rewritten source.
    new_doc = ast.Expr(value=ast.Constant(value=summary_block))
    new_tree = ast.parse(src)
    if _python_module_docstring(new_tree) is not None:
        new_tree.body[0] = new_doc
    else:
        new_tree.body.insert(0, new_doc)
    ast.fix_missing_locations(new_tree)
    try:
        new_src = ast.unparse(new_tree)
    except Exception as exc:
        log.warning("ast.unparse failed for %s: %s", path, exc)
        return False

    # Hard guardrail — non-docstring AST must be byte-identical pre vs post.
    if _ast_dump_no_docstrings(src) != _ast_dump_no_docstrings(new_src):
        log.warning("Guardrail tripped: %s — non-docstring AST drift, "
                    "discarding summary rewrite", path)
        return False

    try:
        path.write_text(new_src, encoding="utf-8")
        return True
    except Exception as exc:
        log.warning("write failed for %s: %s", path, exc)
        return False


# ---------------------------------------------------------------------------
# Folder summary — drops/refreshes <folder>/_folder_summary.md
# ---------------------------------------------------------------------------

FOLDER_SUMMARY_NAME = "_folder_summary.md"


def _python_summary_blurb(path: Path) -> str:
    """Pull just the body of our auto-generated module docstring (without
    the @automatica-summary tail metadata)."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return ""
    doc = _python_module_docstring(tree)
    if not doc:
        return ""
    text = doc.value.value or ""
    # Drop the metadata line.
    return re.split(rf"\n\s*{re.escape(SUMMARY_MAGIC)}\s", text, maxsplit=1)[0].strip()


def summarise_folder(folder: Path, *, endpoint: str, model: str,
                      force: bool = False) -> bool:
    """Refresh the folder's _folder_summary.md from its child file blurbs."""
    if not folder.is_dir():
        return False
    if any(p in str(folder).replace("\\", "/") for p in SKIP_PATTERNS):
        return False
    children: list[tuple[str, str]] = []
    for child in sorted(folder.iterdir()):
        if child.name.startswith("_") or child.name.startswith("."):
            continue
        if child.is_file() and child.suffix.lower() in {".py", ".pyi"}:
            blurb = _python_summary_blurb(child)
            if blurb:
                children.append((child.name, blurb))
        elif child.is_dir() and not any(p in child.name for p in SKIP_PATTERNS):
            sub = child / FOLDER_SUMMARY_NAME
            if sub.is_file():
                children.append((child.name + "/", sub.read_text(
                    encoding="utf-8", errors="replace")[:300]))
    if not children:
        return False

    # Hash the concatenation of child blurbs — folder summary is stale only
    # when a child blurb changed.
    raw = "\n\n".join(f"## {name}\n{blurb}" for name, blurb in children)
    folder_hash = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16]
    out_path = folder / FOLDER_SUMMARY_NAME

    if not force and out_path.is_file():
        existing = out_path.read_text(encoding="utf-8", errors="replace")
        if f"hash={folder_hash}" in existing:
            return False

    prompt = textwrap.dedent(f"""\
        You are documenting a folder in a software project.
        Folder path: {folder}

        Child file/folder summaries:
        {raw}

        Write a 3-5 sentence overview that captures:
          - what this folder is for,
          - the main pieces inside it,
          - when a developer would open it.

        Then on a new line write:
          Keywords: 5-8 single-word lowercase keywords (space-separated).

        Output ONLY the prose + the Keywords line. No markdown headers, no
        code fences, no preamble.""")
    summary = _ollama_summarise(prompt, endpoint=endpoint, model=model,
                                 max_tokens=512)
    if not summary or len(summary) < 30:
        return False

    contents = (
        f"# {folder.name}/\n\n"
        f"<!-- {SUMMARY_MAGIC} hash={folder_hash} "
        f"generated={datetime.now().isoformat(timespec='seconds')} -->\n\n"
        f"{summary.strip()}\n"
    )
    try:
        out_path.write_text(contents, encoding="utf-8")
        return True
    except Exception as exc:
        log.warning("could not write %s: %s", out_path, exc)
        return False


# ---------------------------------------------------------------------------
# Project summary — replaces a sentinel-bracketed block in README.md
# ---------------------------------------------------------------------------

def summarise_project(project_dir: Path, *, endpoint: str, model: str,
                       force: bool = False) -> bool:
    """Refresh the README.md auto-section. Only the block between
    SENTINEL_BEGIN/SENTINEL_END is touched — everything else in the README
    is preserved exactly."""
    readme = project_dir / "README.md"
    folders: list[tuple[str, str]] = []
    for child in sorted(project_dir.iterdir()):
        if not child.is_dir() or any(p in child.name for p in SKIP_PATTERNS):
            continue
        f = child / FOLDER_SUMMARY_NAME
        if f.is_file():
            folders.append((child.name + "/",
                            f.read_text(encoding="utf-8", errors="replace")[:500]))
    if not folders:
        return False

    raw = "\n\n".join(f"## {name}\n{blurb}" for name, blurb in folders)
    project_hash = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16]

    existing = readme.read_text(encoding="utf-8", errors="replace") if readme.is_file() else ""
    if not force and SENTINEL_BEGIN in existing and f"hash={project_hash}" in existing:
        return False

    prompt = textwrap.dedent(f"""\
        You are writing the auto-generated overview section of a project's
        README. Below are summaries of the project's top-level folders:

        {raw}

        Produce:
          1. A one-paragraph project overview (3-5 sentences).
          2. A "## Capability map" section: bullet list of "<folder>: <one
             line of what's in it>".
          3. A "Keywords:" line — 8-12 lowercase keywords for search.

        Output ONLY the prose. Markdown is fine for the bullet section. No
        code fences. No preamble.""")
    body = _ollama_summarise(prompt, endpoint=endpoint, model=model,
                              max_tokens=900)
    if not body or len(body) < 60:
        return False

    block = (
        f"{SENTINEL_BEGIN}\n"
        f"<!-- {SUMMARY_MAGIC} hash={project_hash} "
        f"generated={datetime.now().isoformat(timespec='seconds')} -->\n\n"
        f"{body.strip()}\n\n"
        f"{SENTINEL_END}"
    )

    if SENTINEL_BEGIN in existing and SENTINEL_END in existing:
        new_readme = re.sub(
            rf"{re.escape(SENTINEL_BEGIN)}.*?{re.escape(SENTINEL_END)}",
            block,
            existing,
            count=1,
            flags=re.DOTALL,
        )
    elif existing:
        new_readme = existing.rstrip() + "\n\n## Auto-generated overview\n\n" + block + "\n"
    else:
        new_readme = f"# {project_dir.name}\n\n## Auto-generated overview\n\n{block}\n"
    try:
        readme.write_text(new_readme, encoding="utf-8")
        return True
    except Exception as exc:
        log.warning("could not write %s: %s", readme, exc)
        return False


# ---------------------------------------------------------------------------
# Public API for hooks
# ---------------------------------------------------------------------------

class SummaryIndexer:
    """Thin orchestrator. Maintains a queue of files that changed during a
    sprint, then drains it at sprint end. Folder + project summaries are
    regenerated on cascade."""

    def __init__(self, project_dir: Path, *, endpoint: str = "http://localhost:11434",
                  model: str = "llama3.2:3b") -> None:
        self.project_dir = Path(project_dir)
        self.endpoint = endpoint
        self.model = model
        self._queue: set[Path] = set()
        self._dirty_folders: set[Path] = set()

    def enqueue(self, path: str | Path) -> None:
        p = Path(path)
        if not p.is_absolute():
            p = self.project_dir / p
        try:
            p = p.resolve()
        except Exception:
            return
        if any(s in str(p).replace("\\", "/") for s in SKIP_PATTERNS):
            return
        if p.suffix.lower() in SUMMARISABLE_EXTS:
            self._queue.add(p)

    def drain(self, *, force: bool = False) -> dict:
        """Process the queue. Returns counts of files / folders / project
        rewrites. Safe to call when queue is empty."""
        files_done = 0
        for path in sorted(self._queue):
            if path.suffix.lower() in {".py", ".pyi"}:
                if summarise_python_file(path, endpoint=self.endpoint,
                                          model=self.model, force=force):
                    files_done += 1
                    self._dirty_folders.add(path.parent)
        # Cascade folder summaries — including parents of dirty folders.
        folders_done = 0
        seen: set[Path] = set()
        for folder in list(self._dirty_folders):
            cur = folder
            while cur != self.project_dir.parent and cur not in seen:
                seen.add(cur)
                if cur != self.project_dir:
                    if summarise_folder(cur, endpoint=self.endpoint,
                                         model=self.model, force=force):
                        folders_done += 1
                if cur.parent == cur:
                    break
                cur = cur.parent
        # Project summary if any folder changed.
        project_done = 0
        if folders_done > 0 or force:
            if summarise_project(self.project_dir, endpoint=self.endpoint,
                                  model=self.model, force=force):
                project_done = 1
        # Reset queues.
        self._queue.clear()
        self._dirty_folders.clear()
        return {"files": files_done, "folders": folders_done, "project": project_done}

    def full_walk(self, *, force: bool = False) -> dict:
        """First-time / opt-in: queue every Python file in the project and
        drain. Used to backfill summaries on a project that's been running
        without the indexer."""
        for path in self.project_dir.rglob("*.py"):
            if any(s in str(path).replace("\\", "/") for s in SKIP_PATTERNS):
                continue
            self._queue.add(path.resolve())
        return self.drain(force=force)


__doc__ += "\n\n@automatica-summary"  # mark our own module as auto-aware
