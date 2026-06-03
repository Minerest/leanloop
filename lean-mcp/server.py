#!/usr/bin/env python3
"""lean-mcp server — MCP tools for local code intelligence with LeanLoop.

Tools:
  run_tests(command, cwd, timeout)
    Runs a test command and returns structured results.

  rg_search(query, path, file_glob, case_sensitive)
    Regex search across repository files via ripgrep.

  rg_files(query, path)
    Return only filenames matching a regex pattern.

  rg_context(query, path, before, after)
    Regex search with surrounding context lines.

  find_symbol(name, path, fuzzy)
    Find where a symbol is defined (function, class, constant, variable).
    Supports Python and JavaScript/TypeScript.

Config reused from lean-loop's config.toml:
  [mcp]  rg_max_results   (default 50)
  [mcp]  project_root     (optional — absolute path or relative to config.toml)
  [mcp]  max_file_chars   (default 32000)
"""

from __future__ import annotations

import ast
import atexit
import json
import os
import re
import subprocess
import sys
import threading
import time
import tomllib
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Config discovery — same pattern as leanloop.py
# ---------------------------------------------------------------------------

STATIC_CONFIG_DEFAULTS = (
    Path(__file__).resolve().parent.parent / "config.toml",
    Path.home() / ".config" / "lean-loop" / "config.toml",
)


def _load_toml(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def _find_config() -> dict:
    """Find and return the merged static config (ignores per-project overrides).

    Sets the module-level ``CONFIG_DIR`` to the directory containing the
    loaded config file so relative paths (e.g. ``project_root``) resolve
    correctly.
    """
    global CONFIG_DIR
    for candidate in STATIC_CONFIG_DEFAULTS:
        if candidate.exists():
            CONFIG_DIR = candidate.parent
            return _load_toml(candidate)
    print(
        "lean-mcp: no config.toml found",
        file=sys.stderr,
    )
    sys.exit(1)


CONFIG_DIR: Path | None = None
CONFIG = _find_config()

MCP_CFG = CONFIG.get("mcp", {})

RG_MAX_RESULTS = MCP_CFG.get("rg_max_results", 50)

_LOG_FILE = "/tmp/lean-mcp.log"
_LOG_FILE_READY = False

# ---------------------------------------------------------------------------
# PID file — leanloop.py uses this to detect and kill stale servers
# ---------------------------------------------------------------------------

_PID_FILE = "/tmp/lean-mcp.pid"


def _write_pid_file() -> None:
    """Write this process's PID to the PID file. Register atexit cleanup."""
    try:
        with open(_PID_FILE, "w") as f:
            f.write(str(os.getpid()))
    except OSError:
        return
    atexit.register(_cleanup_pid_file)


def _cleanup_pid_file() -> None:
    """Remove the PID file only if it still belongs to this process."""
    try:
        if os.path.exists(_PID_FILE):
            with open(_PID_FILE) as f:
                if f.read().strip() == str(os.getpid()):
                    os.remove(_PID_FILE)
    except (OSError, ValueError):
        pass


def _parent_monitor() -> None:
    """Daemon thread: exit the process if the parent (Qwen) dies.

    Without this, the server sits in ``select()`` on stdin forever after
    Qwen exits, becoming an orphan that still holds the control port.
    """
    ppid = os.getppid()
    if ppid == 1:
        # Already orphaned at startup — shouldn't happen, but exit cleanly.
        _log("lifecycle", event="already_orphaned", ppid=ppid)
        os._exit(0)

    while True:
        time.sleep(5)
        try:
            os.kill(ppid, 0)  # signal 0 = existence check, no signal delivered
        except OSError:
            _log("lifecycle", event="parent_died", ppid=ppid)
            os._exit(0)


# Test the log file is writable at import time
try:
    with open(_LOG_FILE, "a") as f:
        ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        f.write(f"[lean-mcp] {ts} tool=startup server_initialized=true log_file={_LOG_FILE}\n")
    _LOG_FILE_READY = True
except OSError as e:
    print(f"[lean-mcp] WARNING: cannot write to {_LOG_FILE}: {e}", file=sys.stderr, flush=True)


def _log(tool: str, **kwargs: str | int | bool | None) -> None:
    """Write a structured log line to stderr AND a log file with ISO-8601 timestamp.

    Uses key=value pairs so logs are grep-able. Writes to stderr because
    stdout is the MCP transport channel. Also appends to /tmp/lean-mcp.log
    for easy tailing.
    """
    ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    fields = " ".join(f"{k}={v}" for k, v in kwargs.items() if v is not None)
    msg = f"[lean-mcp] {ts} tool={tool} {fields}"
    print(msg, file=sys.stderr, flush=True)
    if _LOG_FILE_READY:
        try:
            with open(_LOG_FILE, "a") as f:
                f.write(msg + "\n")
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Test output summary helpers
# ---------------------------------------------------------------------------

# Pytest summary lines look like:
#   "3 passed in 0.12s"
#   "1 failed, 1 passed in 3.09s"
#   "2 passed, 1 skipped in 0.05s"
#   "no tests ran in 0.00s"
_PYTEST_SUMMARY_RE = re.compile(
    r"^=+\s*(.*\d+\s+\w+.*\d+\.\d+s)\s*=+\s*$",
    re.MULTILINE,
)


def _extract_pytest_summary(stdout: str) -> str | None:
    """Extract the final summary line from pytest output.

    Returns something like "3 passed in 0.12s" or None if not found.
    """
    for match in _PYTEST_SUMMARY_RE.finditer(stdout):
        return match.group(1).strip()
    # Fallback: scan last few non-empty lines
    lines = stdout.strip().splitlines()
    for line in reversed(lines):
        stripped = line.strip().strip("=").strip()
        if re.search(r"\d+\s+\w+.*\d+\.\d+s", stripped):
            return stripped
    return None


def _parse_test_counts(summary_text: str) -> dict[str, int]:
    """Parse passed/failed/skipped/error counts from a test summary line.

    Handles pytest-style summaries like "3 passed in 0.12s" or
    "1 failed, 2 passed in 3.09s" or "2 passed, 1 skipped in 0.05s".
    Returns empty dict if no counts found.
    """
    counts: dict[str, int] = {}
    for part in summary_text.split(","):
        part = part.strip()
        m = re.match(r"(\d+)\s+(\w+)", part)
        if m and m.group(2) not in ("in",):
            counts[m.group(2)] = int(m.group(1))
    return counts


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

server = FastMCP(
    "lean-mcp",
    instructions=(
        "lean-mcp provides local code intelligence tools for autonomous coding agents. "
        "It has three tool families: (1) ripgrep-powered code search (rg_search, rg_files, "
        "rg_context) for finding symbols, patterns, definitions, usages, and understanding "
        "code structure across the repository; (2) a symbol finder (find_symbol) that "
        "returns structured definition locations with type classification for Python and "
        "JavaScript/TypeScript; and (3) a test runner (run_tests) for executing test "
        "commands and getting structured pass/fail results. "
        "Use code search tools when you need to find where a function is defined, where "
        "a class is used, how an API is called, or what files reference a particular "
        "pattern. Use find_symbol to locate definitions specifically (not usages). "
        "Use run_tests after making code changes to verify correctness. "
        "Prefer rg_files for 'which files contain X?' questions, rg_search for "
        "'show me every occurrence of X', rg_context when you need to read the "
        "surrounding code around each match, and find_symbol when you need to know "
        "where something is defined and what type it is."
    ),
)




# ---------------------------------------------------------------------------
# Test runner — summary-first
# ---------------------------------------------------------------------------


@server.tool(
    name="run_tests",
    description=(
        "Execute a test command (e.g. pytest, go test, npm test, cargo test) against "
        "the repository and return structured JSON results. Designed for autonomous "
        "fix-verify loops: after making code changes, call this to confirm tests pass. "
        "On success (exit code 0): returns a compact one-line summary "
        "(e.g. '3 passed in 0.12s'). On failure: returns the last 100 lines of "
        "stdout and stderr for debugging. Supports any shell command. "
        "Timeout defaults to 300 seconds; pass a higher timeout for slow test suites."
    ),
)
def run_tests(
    command: str,
    cwd: str | None = None,
    timeout: int = 300,
) -> str:
    """Run a test command and return structured results.

    Args:
        command: The shell command to run (e.g. \"pytest tests/ -x -q\").
        cwd: Working directory. Defaults to the project root.
        timeout: Max execution time in seconds (default 300).

    Returns:
        JSON string with returncode, stdout, stderr, timed_out.
        On success (returncode 0), stdout is just the summary line.
        On failure, stdout is the full output (last 100 lines).
    """
    _log("run_tests", command=command, cwd=cwd or "(auto)", timeout=timeout)
    if not cwd:
        cwd = str(_get_repo_root())

    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
        timed_out = False
        retcode = result.returncode
        stdout = result.stdout
        stderr = result.stderr
    except subprocess.TimeoutExpired:
        timed_out = True
        retcode = -1
        stdout = ""
        stderr = "TIMED OUT"

    # Extract test summary line for count parsing (before condensing stdout)
    raw_summary = _extract_pytest_summary(stdout) or ""

    # Summary-first: on success, compress to just the summary line
    if retcode == 0 and not timed_out:
        if raw_summary:
            stdout = raw_summary
        else:
            # Non-pytest runner — grab last meaningful line
            lines = [l for l in stdout.splitlines() if l.strip()]
            stdout = lines[-1] if lines else "ok"
        # Only include stderr if non-empty (warnings)
        stderr_lines = [l for l in stderr.splitlines() if l.strip()]
        stderr = stderr_lines[-5] if stderr_lines else ""
    elif not timed_out:
        # On failure, send full output (last 100 lines each)
        stdout_lines = stdout.splitlines()[-100:]
        stderr_lines = stderr.splitlines()[-100:]
        stdout = "\n".join(stdout_lines)
        stderr = "\n".join(stderr_lines)

    # Parse test counts and determine status
    test_counts = _parse_test_counts(raw_summary)
    hint = None
    if timed_out:
        status = "timeout"
        hint = "Command timed out. Increase the timeout parameter or split into smaller tests."
    elif retcode == 0:
        status = "pass"
    else:
        status = "fail"
        hint = "Tests failed. Review stderr for error details, then use rg_context on the failing file to understand the code."

    result: dict = {
        "status": status,
        "returncode": retcode,
        "timed_out": timed_out,
        "stdout": stdout,
        "stderr": stderr,
        "command": command,
        "cwd": cwd,
    }
    if test_counts:
        result["test_counts"] = test_counts
    if hint:
        result["hint"] = hint
    return json.dumps(result, indent=2)

# ---------------------------------------------------------------------------
# ripgrep search tools
# ---------------------------------------------------------------------------


def _run_rg(args: list[str], cwd: str) -> tuple[int, str, str]:
    """Run ripgrep with the given args, returns (returncode, stdout, stderr).

    Uses subprocess with a clean list-based argv (no shell=True).
    Handles missing binary, timeouts, and OSErrors gracefully.
    """
    try:
        result = subprocess.run(
            ["rg"] + args,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=cwd,
        )
        return result.returncode, result.stdout, result.stderr
    except FileNotFoundError:
        return -1, "", "ripgrep (rg) not found on PATH"
    except subprocess.TimeoutExpired:
        return -1, "", "rg search timed out"
    except OSError as e:
        return -1, "", str(e)


def _parse_rg_line(line: str) -> dict | None:
    """Parse a single rg output line in ``file:line:text`` format.

    Returns a dict with keys ``file``, ``line``, ``text``, or None if the
    line doesn't match expected output (e.g. ``Binary file ... matches``).
    """
    m = re.match(r"^(.+?):(\d+):(.*)", line)
    if not m:
        return None
    return {
        "file": m.group(1),
        "line": int(m.group(2)),
        "text": m.group(3),
    }


def _normalize_rg_path(raw: str) -> str:
    """Strip leading ``./`` prefix from rg output paths."""
    if raw.startswith("./"):
        return raw[2:]
    return raw


# ---------------------------------------------------------------------------
# Symbol index — held in memory, populated at startup or via control port
# ---------------------------------------------------------------------------

_SYMBOL_INDEX: dict[str, list[dict]] = {}
_INDEX_BY_FILE: dict[str, list[str]] = {}

try:
    from . import indexer
except ImportError:
    import indexer  # running directly (python server.py)


def _build_symbol_index(repo_root: Path) -> None:
    """Run the indexer against *repo_root* and store results in module dicts."""
    global _SYMBOL_INDEX, _INDEX_BY_FILE

    result = indexer.build_index(repo_root)
    _SYMBOL_INDEX = result["symbols"]
    _INDEX_BY_FILE = result["by_file"]

    _log(
        "build_index",
        py_files=result["stats"]["py_files"],
        js_files=result["stats"]["js_files"],
        symbols=result["stats"]["symbols"],
        unique_names=result["stats"]["unique_names"],
        elapsed_ms=result["stats"]["elapsed_ms"],
    )


def _build_summary(
    matches: list[dict],
    repo_root: Path,
) -> dict:
    """Build an enriched summary from a list of rg matches.

    Reads symbol names from the in-memory ``_INDEX_BY_FILE`` instead of
    re-parsing files on every call.
    """
    files_seen: dict[str, int] = {}
    for m in matches:
        f = m["file"]
        files_seen[f] = files_seen.get(f, 0) + 1

    # Extension breakdown
    extensions: dict[str, int] = {}
    for f in files_seen:
        ext = Path(f).suffix.lstrip(".") or "(none)"
        extensions[ext] = extensions.get(ext, 0) + 1

    # Symbols from the in-memory index (top 10 most-hit files)
    sorted_files = sorted(files_seen, key=files_seen.get, reverse=True)
    symbol_map: dict[str, list[str]] = {}
    for f in sorted_files[:10]:
        syms = _INDEX_BY_FILE.get(f)
        if syms:
            symbol_map[f] = syms

    return {
        "total_matches": len(matches),
        "files_affected": len(files_seen),
        "match_distribution": sorted_files[:5],
        "extensions": extensions,
        "symbols": symbol_map,
    }


def _get_repo_root() -> Path:
    """Return the project root directory for ripgrep searches and test runs.

    Resolution order (first match wins):

    1. ``LEAN_PROJECT_ROOT`` environment variable (absolute path).
    2. ``[mcp] project_root`` in the loaded config.toml. Relative paths are
       resolved against the config file's directory.
    3. Current working directory — the default when leanloop.py or Qwen CLI
       launches this server from the project directory.
    """
    # 1. Environment variable override
    if env_root := os.environ.get("LEAN_PROJECT_ROOT"):
        return Path(env_root).resolve()

    # 2. Config file override
    if cfg_root := MCP_CFG.get("project_root"):
        p = Path(cfg_root)
        if not p.is_absolute() and CONFIG_DIR is not None:
            p = (CONFIG_DIR / p).resolve()
        return p

    # 3. Default: inherit from the parent process (leanloop / Qwen CLI cwd)
    return Path.cwd()


@server.tool(
    name="rg_search",
    description=(
        "Full-text regex search across the entire repository using ripgrep. "
        "Returns up to 50 matches, each with file path, line number, and the "
        "matched line text. Also includes an enriched summary: which files were "
        "hit (sorted by match count), and symbol extraction (function and class "
        "names) from the most-affected Python files. "
        "Use this to find: function/class definitions, API usages, import patterns, "
        "error message sources, configuration keys, string literals, or any code "
        "pattern. Case-insensitive by default; set case_sensitive=true for exact "
        "matching. Filter by file type with file_glob (e.g. '*.py', '*.ts', '*.rs'). "
        "Prefer rg_search for broad code exploration; use rg_context when you need "
        "surrounding lines to understand the code around each match."
    ),
)
def rg_search(
    query: str,
    path: str = ".",
    file_glob: str | None = None,
    case_sensitive: bool = False,
) -> str:
    """General text search across repo using ripgrep.

    Args:
        query: Regex pattern to search for.
        path: Directory to search (relative to repo root, default '.').
        file_glob: Optional file glob (e.g. ``"*.ts"``, ``"*.py"``).
        case_sensitive: Case-sensitive match (default False).

    Returns:
        JSON string with list of matches, each with ``file``, ``line``, ``text``.
    """
    _log("rg_search", query=query, path=path, glob=file_glob, case=case_sensitive)
    repo_root = _get_repo_root()

    rg_args = ["--no-heading", "-n", "--color", "never"]
    if not case_sensitive:
        rg_args.append("-i")
    if file_glob:
        rg_args.extend(["-g", file_glob])
    rg_args.append(query)
    rg_args.append(path)

    retcode, stdout, stderr = _run_rg(rg_args, str(repo_root))

    if retcode == 2:
        return json.dumps({"error": f"rg error: {stderr.strip()}"})
    if retcode == -1:
        return json.dumps({"error": stderr.strip()})

    matches: list[dict] = []
    seen: set[tuple[str, int]] = set()
    for line in stdout.splitlines():
        parsed = _parse_rg_line(line)
        if parsed is None:
            continue
        key = (parsed["file"], parsed["line"])
        if key in seen:
            continue
        seen.add(key)
        parsed["file"] = _normalize_rg_path(parsed["file"])
        matches.append(parsed)
        if len(matches) >= RG_MAX_RESULTS:
            break

    summary = _build_summary(matches, repo_root)
    _log("rg_search.result", 
         matches=len(matches), 
         files=summary["files_affected"], 
         symbols=sum(len(v) for v in summary["symbols"].values()))
    result: dict = {
        "matches": matches,
        "summary": summary,
    }
    if len(matches) >= RG_MAX_RESULTS:
        result["truncated"] = True
        result["hint"] = (
            f"Results capped at {RG_MAX_RESULTS}. Narrow your query "
            "or use file_glob to filter by file type."
        )
    return json.dumps(result, indent=2)


@server.tool(
    name="rg_files",
    description=(
        "Find which files in the repository contain a regex pattern — returns only "
        "file paths, no line content. Fast and lightweight: use this when you only "
        "need to know which files reference a symbol, import, or pattern, without "
        "reading the actual lines. Returns up to 50 file paths relative to the "
        "project root. "
        "Use this before rg_search or rg_context to narrow down which files to "
        "investigate, or to answer questions like 'which files import X?' or "
        "'which files define class Y?'. More efficient than rg_search when you "
        "don't need line-level detail."
    ),
)
def rg_files(
    query: str,
    path: str = ".",
) -> str:
    """Return only files matching a regex pattern (no line content).

    Args:
        query: Regex pattern to search for.
        path: Directory to search (relative to repo root, default '.').

    Returns:
        JSON string with list of file paths.
    """
    _log("rg_files", query=query, path=path)
    repo_root = _get_repo_root()

    # Use --count-matches for per-file match density
    rg_args = ["--color", "never", "--count-matches", query, path]

    retcode, stdout, stderr = _run_rg(rg_args, str(repo_root))

    if retcode == 2:
        return json.dumps({"error": f"rg error: {stderr.strip()}"})
    if retcode == -1:
        return json.dumps({"error": stderr.strip()})

    files: list[dict] = []
    total: int = 0
    extensions: dict[str, int] = {}
    for raw in stdout.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        # rg --count-matches outputs "file:count"
        m = re.match(r"^(.+):(\d+)$", stripped)
        if m:
            fpath = _normalize_rg_path(m.group(1))
            fcount = int(m.group(2))
            files.append({"path": fpath, "matches": fcount})
            total += fcount
            ext = Path(fpath).suffix.lstrip(".") or "(none)"
            extensions[ext] = extensions.get(ext, 0) + 1
            if len(files) >= RG_MAX_RESULTS:
                break

    _log("rg_files.result", count=len(files), total_matches=total)

    result: dict = {
        "files": sorted(files, key=lambda f: f["matches"], reverse=True),
        "count": len(files),
        "total_matches": total,
    }
    if extensions:
        result["extensions"] = extensions
    if files:
        result["hint"] = (
            f"Use rg_context on '{files[0]['path']}' "
            "to see the surrounding code around each match."
        )
    return json.dumps(result, indent=2)


@server.tool(
    name="rg_context",
    description=(
        "Regex search with surrounding context lines — returns each match wrapped "
        "in the code around it (default: 5 lines before, 10 lines after). "
        "Designed for code understanding: see the full function body, class "
        "definition, or logic block that contains each match. "
        "Returns up to 50 match groups, each with file path, start_line, end_line, "
        "match_line (the line that actually matched), and a structured context "
        "array where each entry has line number, text, and an is_match boolean. "
        "Includes enriched summary with symbol extraction and file extension breakdown. "
        "Use this when you need to understand how a symbol is used in context — "
        "e.g. reading a function implementation, checking how an API is called, "
        "or understanding error handling around a pattern. "
        "Adjust before/after to control how much context you get. "
        "For simple 'find all occurrences', use rg_search instead."
    ),
)
def rg_context(
    query: str,
    path: str = ".",
    before: int = 5,
    after: int = 10,
) -> str:
    """Return matches with surrounding context for code understanding.

    Args:
        query: Regex pattern to search for.
        path: Directory to search (relative to repo root, default '.').
        before: Lines of context before each match (default 5).
        after: Lines of context after each match (default 10).

    Returns:
        JSON string with matches that include a ``context`` array of lines.
    """
    _log("rg_context", query=query, path=path, before=before, after=after)
    repo_root = _get_repo_root()

    rg_args = [
        "--no-heading", "--color", "never",
        "-n",
        "-B", str(before),
        "-A", str(after),
        query,
        path,
    ]

    retcode, stdout, stderr = _run_rg(rg_args, str(repo_root))

    if retcode == 2:
        return json.dumps({"error": f"rg error: {stderr.strip()}"})
    if retcode == -1:
        return json.dumps({"error": stderr.strip()})

    matches: list[dict] = []
    current_file: str | None = None
    current_start: int | None = None
    current_match_ln: int | None = None
    current_lines: list[str] = []

    def _flush() -> None:
        nonlocal current_file, current_start, current_match_ln, current_lines
        if current_file is not None and current_lines:
            end_line = current_start + len(current_lines) - 1
            context = []
            for i, txt in enumerate(current_lines):
                ln = current_start + i
                context.append({
                    "line": ln,
                    "text": txt,
                    "is_match": ln == current_match_ln,
                })
            matches.append({
                "file": current_file,
                "start_line": current_start,
                "end_line": end_line,
                "match_line": current_match_ln,
                "context": context,
            })
        current_file = None
        current_start = None
        current_match_ln = None
        current_lines = []

    for line in stdout.splitlines():
        # rg uses '--' as a group separator between match groups
        if line.strip() == "--":
            _flush()
            continue

        parsed = _parse_rg_line(line)
        if parsed is None:
            continue

        f = _normalize_rg_path(parsed["file"])
        ln = parsed["line"]
        txt = parsed["text"]

        if f != current_file:
            _flush()
            current_file = f
            current_start = ln
            current_match_ln = ln  # first line of each group = the match
            current_lines = [txt]
        else:
            current_lines.append(txt)

    _flush()

    # Enforce result limit
    matches = matches[:RG_MAX_RESULTS]

    summary = _build_summary(matches, repo_root)
    _log("rg_context.result", 
         matches=len(matches), 
         files=summary["files_affected"],
         symbols=sum(len(v) for v in summary["symbols"].values()))
    result: dict = {
        "matches": matches,
        "summary": summary,
    }
    if len(matches) >= RG_MAX_RESULTS:
        result["truncated"] = True
        result["hint"] = (
            f"Results capped at {RG_MAX_RESULTS}. Narrow your query "
            "or use file_glob to filter by file type."
        )
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Symbol finder — in-memory index lookup
# ---------------------------------------------------------------------------


@server.tool(
    name="find_symbol",
    description=(
        "Find where a symbol (function, class, constant, variable) is defined "
        "in the codebase. Returns structured JSON keyed by symbol name — each "
        "value has file path, line number, type, and detected language. "
        "Python symbols are extracted via AST (distinguishes method vs "
        "function); JavaScript/TypeScript via regex. "
        "The index is built once at server startup and held in memory — "
        "lookups are O(1) with no ripgrep subprocess. "
        "Exact match by default; pass fuzzy=true to also find symbols whose "
        "name starts with the query (e.g. 'auth' matches 'authenticate', "
        "'authManager', 'authorize'). "
        "Multiple definitions of the same symbol name are returned as an array. "
        "Narrow the search with the path parameter (e.g. 'backend/', 'src/')."
    ),
)
def find_symbol(
    name: str,
    path: str = ".",
    fuzzy: bool = False,
) -> str:
    """Look up symbol definitions from the in-memory index.

    Args:
        name: Symbol name to search for (e.g. ``"authenticate"``).
        path: Directory to filter by (relative to repo root, default ``"."``).
        fuzzy: When true, also matches symbols whose name starts with
               ``name`` (e.g. ``"auth"`` matches ``authenticate``,
               ``authManager``, ``authorize``).

    Returns:
        JSON with ``symbols`` dict keyed by symbol name. Each value is a
        single ``{file, line, type, language}`` object or an array.
    """
    _log("find_symbol", name=name, path=path, fuzzy=fuzzy)

    # Collect matching entries from the in-memory index
    if fuzzy:
        matches: dict[str, list[dict]] = {}
        for key, entries in _SYMBOL_INDEX.items():
            if key.startswith(name):
                matches[key] = entries
    else:
        entries = _SYMBOL_INDEX.get(name, [])
        matches = {name: entries} if entries else {}

    # Filter by path scope
    if path != "." and matches:
        path_prefix = path.rstrip("/") + "/"
        filtered: dict[str, list[dict]] = {}
        for sym_name, entries in matches.items():
            kept = [e for e in entries if e["file"].startswith(path_prefix)]
            if kept:
                filtered[sym_name] = kept
        matches = filtered

    # Format: single entry → object, multiple → array
    symbols: dict = {}
    for sym_name, entries in matches.items():
        if len(entries) == 1:
            symbols[sym_name] = entries[0]
        else:
            symbols[sym_name] = entries

    _log("find_symbol.result", names=len(symbols), total=sum(
        1 if isinstance(v, dict) else len(v)
        for v in symbols.values()
    ))

    result: dict = {"symbols": symbols}
    if not symbols:
        result["hint"] = (
            f"No definition found for '{name}'. "
            "Try fuzzy=true for partial matches, or use rg_search "
            "to find usages instead of definitions."
        )
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Index rebuild — re-expose the startup indexer as a callable tool
# ---------------------------------------------------------------------------


@server.tool(
    name="rebuild_index",
    description=(
        "Rebuild the in-memory symbol index from scratch. "
        "Call this after making code changes (new files, new functions, renames) "
        "so that find_symbol and find_usages see the latest definitions. "
        "Returns the same stats as startup: py_files, js_files, symbols, "
        "unique_names, and elapsed_ms."
    ),
)
def rebuild_index() -> str:
    """Re-run the AST+regex indexer and replace the in-memory index."""
    global _SYMBOL_INDEX, _INDEX_BY_FILE
    repo_root = _get_repo_root()
    _build_symbol_index(repo_root)
    stats = {
        "symbols": sum(len(v) for v in _SYMBOL_INDEX.values()),
        "unique_names": len(_SYMBOL_INDEX),
    }
    return json.dumps({"status": "ok", **stats}, indent=2)


# ---------------------------------------------------------------------------
# find_usages — "who calls this function?"
# ---------------------------------------------------------------------------


def _build_usages_regex(name: str, symbol_type: str | None) -> str:
    """Build a regex pattern for finding usages of a symbol.

    For functions/methods: matches ``name(`` (call sites).
    For classes: matches ``name`` as a whole word (instantiation, type hints).
    Falls back to word-boundary match when type is unknown.
    """
    if symbol_type in ("function", "method"):
        return rf"\b{re.escape(name)}\s*\("
    # class, constant, variable, or unknown — word-boundary match
    return rf"\b{re.escape(name)}\b"


_DEFINITION_KEYWORDS = {
    "python": {"def ", "class ", "async def "},
    "javascript": {"function ", "class ", "const ", "let ", "var "},
}


def _looks_like_definition(line: str, name: str, language: str | None) -> bool:
    """Heuristic: does this line look like it's defining *name*?"""
    keywords = _DEFINITION_KEYWORDS.get(language or "", set())
    stripped = line.strip()
    for kw in keywords:
        if stripped.startswith(kw) and name in stripped:
            return True
    return False


@server.tool(
    name="find_usages",
    description=(
        "Find every call site / usage of a symbol (function, class, constant, "
        "variable). Uses the in-memory symbol index to determine the symbol's "
        "type, then runs a targeted ripgrep search: "
        "functions/methods → matches ``name(``; "
        "classes/constants → matches ``name`` as a word. "
        "Definition sites (from the symbol index) are excluded so you only see "
        "callers and references, not the definition itself. "
        "Narrow the search with the path parameter (e.g. 'backend/', 'src/'). "
        "Use this for debugging: 'who calls this?', 'where is this class used?', "
        "'what code depends on this function?'"
    ),
)
def find_usages(
    name: str,
    path: str = ".",
) -> str:
    """Find all usages of a symbol across the repository.

    Args:
        name: Symbol name to find usages of.
        path: Directory to scope the search to (default '.').

    Returns:
        JSON with ``usages`` list, each entry having file, line, text,
        and a ``definition`` boolean that is False for call sites.
    """
    _log("find_usages", name=name, path=path)

    # Determine symbol type(s) from the index
    entries = _SYMBOL_INDEX.get(name, [])
    symbol_types = {e["type"] for e in entries}
    primary_type = next(iter(symbol_types), None) if len(symbol_types) == 1 else None

    # Build the search regex based on type
    pattern = _build_usages_regex(name, primary_type)

    # Collect definition sites to filter out
    def_sites: set[tuple[str, int]] = set()
    for entry in entries:
        def_sites.add((entry["file"], entry["line"]))

    # Run ripgrep
    repo_root = _get_repo_root()
    rg_args = ["--no-heading", "-n", "--color", "never", pattern, path]

    retcode, stdout, stderr = _run_rg(rg_args, str(repo_root))

    if retcode == 2:
        return json.dumps({"error": f"rg error: {stderr.strip()}"})
    if retcode == -1:
        return json.dumps({"error": stderr.strip()})

    usages: list[dict] = []
    files_seen: dict[str, int] = {}
    def_count = 0
    for line in stdout.splitlines():
        parsed = _parse_rg_line(line)
        if parsed is None:
            continue
        fpath = _normalize_rg_path(parsed["file"])
        ln = parsed["line"]
        txt = parsed["text"]

        # Skip definition sites
        if (fpath, ln) in def_sites and _looks_like_definition(
            txt, name, entries[0]["language"] if entries else None
        ):
            def_count += 1
            continue

        usages.append({
            "file": fpath,
            "line": ln,
            "text": txt.strip(),
            "definition": False,
        })
        files_seen[fpath] = files_seen.get(fpath, 0) + 1

        if len(usages) >= RG_MAX_RESULTS:
            break

    _log("find_usages.result",
         usages=len(usages),
         files=len(files_seen),
         definitions_filtered=def_count)

    result: dict = {
        "symbol": name,
        "type": primary_type or list(symbol_types) if symbol_types else "unknown",
        "usages": usages,
        "total": len(usages),
        "files_affected": len(files_seen),
    }
    if len(usages) >= RG_MAX_RESULTS:
        result["truncated"] = True
        result["hint"] = (
            f"Results capped at {RG_MAX_RESULTS}. Narrow with the path parameter."
        )
    if not usages:
        result["hint"] = (
            f"No usages found for '{name}' outside its definition site(s). "
            "Try a broader path scope or check the symbol name."
        )
    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# read_symbol — read the full source of a function or class
# ---------------------------------------------------------------------------


def _read_symbol_source(
    file_path: Path, start_line: int, language: str
) -> str | None:
    """Read the complete source of a symbol starting at *start_line*.

    For Python: tracks indentation — reads until a non-blank line with
    indentation <= the definition line's indentation.
    For JS/TS: tracks brace nesting from the first ``{``.

    Returns the source text or None on error.
    """
    try:
        lines = file_path.read_text().splitlines()
    except (OSError, UnicodeDecodeError):
        return None

    if start_line < 1 or start_line > len(lines):
        return None

    idx = start_line - 1  # 0-based

    if language == "python":
        # Measure indentation of the definition line
        def_line = lines[idx]
        if not def_line.strip():
            return None
        base_indent = len(def_line) - len(def_line.lstrip())

        # Collect lines until we hit a non-blank line with indentation <= base_indent
        # (skipping the definition line itself)
        out = [def_line]
        for i in range(idx + 1, len(lines)):
            line = lines[i]
            stripped = line.strip()
            if not stripped:
                out.append(line)
                continue
            indent = len(line) - len(line.lstrip())
            if indent <= base_indent:
                break
            out.append(line)
        return "\n".join(out)

    # JavaScript / TypeScript: brace-counting approach
    out = [lines[idx]]
    brace_depth = 0
    started = False
    for i in range(idx, len(lines)):
        line = lines[i]
        if i == idx:
            # Count braces on the definition line
            brace_depth += line.count("{") - line.count("}")
            if brace_depth > 0:
                started = True
            continue

        if not started:
            # Haven't seen an opening brace — look for one on subsequent lines
            # (handles multi-line signatures)
            brace_depth += line.count("{") - line.count("}")
            out.append(line)
            if brace_depth > 0:
                started = True
            continue

        brace_depth += line.count("{") - line.count("}")
        out.append(line)
        if brace_depth <= 0:
            break

    return "\n".join(out)


@server.tool(
    name="read_symbol",
    description=(
        "Read the full source code of a named symbol (function, method, class) "
        "from its definition to the closing brace / outdent. "
        "Uses the in-memory symbol index to find the file and line number, "
        "then extracts the complete body. "
        "For Python: indentation-based boundary detection. "
        "For JavaScript/TypeScript: brace-counting boundary detection. "
        "If multiple definitions of the same name exist, reads the first one. "
        "Use this instead of read_file + line counting for quickly viewing "
        "a function or class implementation."
    ),
)
def read_symbol(
    name: str,
) -> str:
    """Read the source of a symbol defined in the codebase.

    Args:
        name: Exact symbol name to read.

    Returns:
        JSON with ``symbol``, ``file``, ``line``, ``language``, and ``source``.
    """
    _log("read_symbol", name=name)

    entries = _SYMBOL_INDEX.get(name, [])
    if not entries:
        return json.dumps({
            "error": f"Symbol '{name}' not found in index. "
                      "Try rebuild_index if code was recently added."
        })

    # Pick the first entry (if multiple, caller can disambiguate by path)
    entry = entries[0]
    repo_root = _get_repo_root()
    file_path = repo_root / entry["file"]

    source = _read_symbol_source(file_path, entry["line"], entry["language"])
    if source is None:
        return json.dumps({"error": f"Could not read {entry['file']}"})

    line_count = source.count("\n") + 1
    _log("read_symbol.result", name=name, file=entry["file"],
         line=entry["line"], lines=line_count)

    return json.dumps({
        "symbol": name,
        "file": entry["file"],
        "line": entry["line"],
        "type": entry["type"],
        "language": entry["language"],
        "lines": line_count,
        "source": source,
    }, indent=2)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    repo_root = _get_repo_root()
    _log(
        "startup",
        repo_root=str(repo_root),
        cwd=str(Path.cwd()),
        config_dir=str(CONFIG_DIR) if CONFIG_DIR else "none",
    )
    _build_symbol_index(repo_root)

    # Write PID file so leanloop.py can find and kill stale servers.
    _write_pid_file()

    # Start parent-liveness monitor — exits the process when Qwen dies,
    # preventing orphaned servers that sit in select() forever.
    threading.Thread(target=_parent_monitor, daemon=True, name="parent-monitor").start()

    server.run(transport="stdio")


if __name__ == "__main__":
    main()
