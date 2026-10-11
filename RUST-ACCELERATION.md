# Rust acceleration

**Status:** pure Python today. Rust acceleration is required eventually: a Rust core
(`rust/aixyad_core`) with a thin PyO3/maturin wrapper, the pattern the PlayTow
modules (Arithma, Themelios) use. There is no silent fallback. A missing extension
raises, and `run_autonomous.main` and `api_server` call `assert_rust_backend()` at startup.
Most of AIxYad waits on model providers, builds or SQLite. The hot paths read the
*target* project's source tree, so their cost grows with that tree.

## Evidence (2026-10-02, Python 3.13, Windows 11)

Under `cProfile`, the test suite (492 tests, live-Ollama ones deselected) took 180 s:
150 s waiting in `urlopen` inside `AgentProtocol.execute_loop`, 15 s in `time.sleep`,
under 2 s in AIxYad's own CPU code, because the test projects are tiny. A synthetic
target (601 source files, one 44k-line `.cpp`, 2,000 files in `node_modules`) shows real cost:

| Call | Time |
|---|---|
| `search_symbols(name=..., limit=30)`, no hit | 4.6 s warm, 7.0 s first pass |
| `get_file_structure` on the 44k-line `.cpp` | 1.5 s |
| `scan_guid_markers` / `build_file_wiki` cold / `list_project_files` | 250 / 150 / 140 ms |
| `_extract_json` on a 40 KB reply with bad quotes / `_scrub` on a 12 KB record | 4.8 / 0.6 ms |

## Hot paths, ranked

1. **Symbol extraction**: `project_scanner.get_file_structure` and its `_parse_cpp`,
   `_parse_python`, `_parse_js_ts`, `_parse_markdown` and `_parse_generic`. Every match
   finds its line with `content[:m.start()].count('\n')`, which copies and rescans from
   the top of the file, so cost grows quadratically. 1.4k lines take 3 ms, 11k take
   102 ms and 44k take 1,526 ms. `str.count` alone is 2.5 s of the 4.6 s `search_symbols`
   profile. The `query_file_structure` and `query_symbols` actions reach it, and
   `search_symbols` re-parses every file on every call.
2. **Tree-wide text search**: `agent_executor._do_search` and `_do_search_by_guid`,
   `view_actions._grep_project`, and `project_scanner.scan_guid_markers`,
   `find_guid_section` and `_walk_text_files`. Four copies of one kernel each walk, read
   and regex every line in Python. The `rglob('*')` walkers enumerate `.git` and
   `node_modules` before discarding them, and `_do_search` never skips them.
3. **Walk, stat and count**: `_walk_files`, `list_project_files` (counts lines in a
   Python loop) and `build_file_wiki` (its cache check stats every file on every call).
   `list_project_files` runs in every `AgentProtocol.build_request` and in three places
   in `sprint_runner`.
4. **Later**, only because it would sit beside the others: `adapters/_base._extract_json`
   and its character-by-character repair loops. Milliseconds against seconds of latency.

**Not worth moving.** Provider calls (`adapters/http_*`, `subprocess_cli`,
`ollama_utils`, `summary_indexer._ollama_summarise`, `planning_quality_gate`) are
network-bound. `api_server.py` is FastAPI and WebSocket glue, and `sprint_runner.py`
and `run_autonomous.py` orchestrate and wait on builds. SQLite (`aixyad_db.py`,
`db_actions.py`), Pillow and OS calls (`vision_actions.py`, `ui_automation.py`), `re` in
`log_filter._scrub` and `hashlib` are already C. Code built on CPython's `ast` stays in
Python (`summary_indexer._content_hash`, `agent_executor._smart_merge_python`,
`agent_protocol._py_is_stub`).

## Reuse before writing

- **Themelios** (`PTTFileWatcher::check_for_changes`) replaces the stat sweep in
  `build_file_wiki`'s cache check and lets a structure cache re-parse only changed
  files. It pulls in tokio, glam and serde_yaml: compile weight, not a reason to write another.
- **Ergon** (`ergon_core::Pool`) spreads per-file parsing over threads with no runtime.
  Collect outcomes in submit order: `search_symbols` stops at `limit` in walk order.
- **EyeCore**: not for now. `score_text` is tiered (prefix, contains, fuzzy) while
  `_do_find_relevant_files` counts terms, so reuse would change rankings, and
  `sha256_hex` replaces `hashlib`, which is not the slow part of `_content_hash`.
- **Exetazo, Metron, Rhoe, Arithma, Topos, Epipedon**: no fit. AIxYad logs to SQLite
  rather than parsing logs, and has no metrics layer, binding, maths, spatial index or 2D.

Depend on them with their `python` feature off, so only `aixyad_core` links PyO3
(Themelios and Ergon pin pyo3 0.22, EyeCore 0.28).

## Layout

```
Cargo.toml                    workspace root; [profile.release] lto, codegen-units = 1
rust/aixyad_core/
  Cargo.toml                  cdylib + rlib; features: default = [], python = ["dep:pyo3"],
                              extension-module = ["python", "pyo3/extension-module"]
  pyproject.toml              maturin; module-name aixyad_core._aixyad_core,
                              features ["extension-module"], python-source "python"
  src/lib.rs                  plain Rust API: lines.rs, symbols.rs, walk.rs, search.rs
  src/py.rs                   #[cfg(feature = "python")] bindings only
  python/aixyad_core/     BACKEND, RustBackendUnavailable, assert_rust_backend()
  tests/                      cargo tests over the golden corpus
```

The root package stays on setuptools and depends on `aixyad-core`, which ends the
"stdlib only" claim in `pyproject.toml`. `project_scanner` keeps its public signatures.
Add `target/` to `.gitignore`. CI (`.github/workflows/ci.yml`, none yet) has a Rust job
that runs `cargo fmt --check`, `cargo clippy --all-targets --features python -- -D warnings`
and `cargo test --features python`. A Python job on windows-latest and ubuntu-latest,
Python 3.10 to 3.13, runs `maturin build --release`, installs the wheel and runs pytest,
with `conftest.py` calling `assert_rust_backend()`.

## First slice: symbol extraction

Port the parse step of `get_file_structure`: given `content` and `language`, return the
line count, TODOs, GUIDs, functions, classes, enums, namespaces and sections. Python keeps
file I/O, `_parse_json_structure` (it is `json.loads`) and every caller, so
`search_symbols` speeds up untouched. Once parity holds, delete the Python parsers; the
golden files are the reference. Traps that change output silently:
- Line counts and TODO lines use `str.splitlines()`, which also breaks on `\r`, `\v`,
  `\f`, `\x1c` to `\x1e`, `\x85`, U+2028 and U+2029. Regex lines count `\n` only.
- Python's `\s` includes `\x1c` to `\x1f`; Rust's Unicode `\s` does not.
- Python slices count code points, not bytes: `sig[:117]`, `[:120]`, the 100-character
  lookahead in `_parse_js_ts`. Reads use `errors='replace'`; check `from_utf8_lossy`.
- `build_file_wiki` is defined twice in `project_scanner.py`; the first (line 407) is dead.
- For slice 2: `_do_search` patterns are Python `re` syntax, so they need `fancy-regex`
  or an explicit rejection, never different matches. `fnmatch` ignores case on Windows.

A newline-offset table with `bisect` also fixes most of hot path 1 in Python (44k lines:
1,526 ms to 50 ms in a scratch run). If it lands first, it is the baseline Rust must beat.

**Acceptance test.** First record today's `get_file_structure` output for a fixture set
into `tests/golden/scanner/*.json`: C, C++, Python, JS/TS, Markdown, Rust, Go, Java and
C#, with CRLF, form feeds, `\x1c`, non-UTF-8 bytes and the 44k-line `.cpp`. Done when:
- `tests/test_scanner_parity.py` gets byte-identical JSON from Rust for every fixture;
- the 21 scanner tests passing today still pass unchanged (`tests/test_project_scanner.py`,
  `tests/test_wiki_index.py`, `tests/test_full_integration.py::TestExecutorScannerActions`);
- the 44k-line file parses at least 10 times faster than 1.5 s and no slower than the
  Python line-table version;
- without the extension, `assert_rust_backend()` raises `RustBackendUnavailable` and
  `get_file_structure` raises instead of parsing.

Next: slice 2 puts walk, read and search on Ergon (hot paths 2 and 3, one kernel, not
four). Slice 3 adds the Themelios watcher and a structure cache.
