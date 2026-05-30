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

Config reused from lean-loop's config.toml:
  [mcp]  rg_max_results (default 50)
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
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
    """Find and return the merged static config (ignores per-project overrides)."""
    for candidate in STATIC_CONFIG_DEFAULTS:
        if candidate.exists():
            return _load_toml(candidate)
    print(
        "lean-mcp: no config.toml found",
        file=sys.stderr,
    )
    sys.exit(1)


CONFIG = _find_config()

MCP_CFG = CONFIG.get("mcp", {})

RG_MAX_RESULTS = MCP_CFG.get("rg_max_results", 50)

_LOG_FILE = "/tmp/lean-mcp.log"
_LOG_FILE_READY = False


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
        "It has two tool families: (1) ripgrep-powered code search (rg_search, rg_files, "
        "rg_context) for finding symbols, patterns, definitions, usages, and understanding "
        "code structure across the repository; and (2) a test runner (run_tests) for "
        "executing test commands and getting structured pass/fail results. "
        "Use code search tools when you need to find where a function is defined, where "
        "a class is used, how an API is called, or what files reference a particular "
        "pattern. Use run_tests after making code changes to verify correctness. "
        "Prefer rg_files for 'which files contain X?' questions, rg_search for "
        "'show me every occurrence of X', and rg_context when you need to read the "
        "surrounding code around each match to understand how X is used."
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
        # Default to project root
        script_dir = Path(__file__).resolve().parent
        if script_dir.name == "lean-mcp":
            cwd = str(script_dir.parent)
        else:
            cwd = str(Path.cwd())

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


# Maximum unique files to scan for symbol extraction (per call)
_MAX_SYMBOL_FILES = 10


def _extract_symbols(file_path: str, repo_root: Path) -> list[str]:
    """Extract function and class names from a Python file using AST.

    Only scans ``.py`` files. Returns up to 15 symbols per file.
    """
    if not file_path.endswith(".py"):
        return []

    full_path = repo_root / file_path
    if not full_path.is_file():
        return []

    try:
        with open(full_path, "r", encoding="utf-8", errors="replace") as f:
            tree = ast.parse(f.read())
    except (SyntaxError, OSError):
        return []

    symbols: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append(f"def {node.name}")
        elif isinstance(node, ast.ClassDef):
            symbols.append(f"class {node.name}")
        if len(symbols) >= 15:
            break
    return symbols


def _build_summary(
    matches: list[dict],
    repo_root: Path,
) -> dict:
    """Build an enriched summary from a list of rg matches.

    Returns a dict with top-level context: files affected, symbol map,
    and match distribution.
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

    # Extract symbols from N most-hit files
    sorted_files = sorted(files_seen, key=files_seen.get, reverse=True)
    symbol_map: dict[str, list[str]] = {}
    for f in sorted_files[:_MAX_SYMBOL_FILES]:
        syms = _extract_symbols(f, repo_root)
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
    """Return the project root directory (parent of ``lean-mcp/``)."""
    script_dir = Path(__file__).resolve().parent
    if script_dir.name == "lean-mcp":
        return script_dir.parent
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
        "-C", f"{before},{after}",
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
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
