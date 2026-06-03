"""Symbol indexer — pure AST + regex crawl, no server dependencies.

Extracted from server.py so leanloop.py can build the index and push it
to a running MCP server via the control port.
"""
from __future__ import annotations

import ast
import re
import subprocess
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SKIP_DIRS: set[str] = {
    ".git", "__pycache__", "node_modules", "venv", ".venv",
    "dist", "build", ".tox", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", "site-packages", "egg-info",
}

_JS_INDEX_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(?:^|\n)\s*function\s+(\w+)\s*\(", re.MULTILINE), "function"),
    (re.compile(r"(?:^|\n)\s*class\s+(\w+)\b", re.MULTILINE), "class"),
    (re.compile(r"(?:^|\n)\s*(?:export\s+)?const\s+(\w+)\s*=", re.MULTILINE), "constant"),
    (re.compile(r"(?:^|\n)\s*(?:export\s+)?let\s+(\w+)\s*=", re.MULTILINE), "variable"),
]

_JS_EXTENSIONS: set[str] = {
    ".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs", ".mts", ".cts",
}


# ---------------------------------------------------------------------------
# Git-aware file filtering
# ---------------------------------------------------------------------------

def _git_ls_files(repo_root: Path) -> set[str] | None:
    """Return the set of non-ignored files according to git, or None when
    the directory is not a git repository."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files",
             "--cached", "--others", "--exclude-standard"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return None
        return {line.strip() for line in result.stdout.splitlines() if line.strip()}
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


def _should_skip_dir(parts: tuple[str, ...]) -> bool:
    """Check whether any path component matches a skip directory."""
    return any(p.startswith(".") or p in _SKIP_DIRS for p in parts)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def build_index(repo_root: Path) -> dict:
    """Full AST + regex crawl of *repo_root*.

    Returns a plain dict:
        {
          "symbols":   {name: [{file, line, type, language} ...]},
          "by_file":   {file_path: [name ...]},
          "stats":     {py_files, js_files, symbols, unique_names, elapsed_ms},
        }
    """
    start = time.monotonic()
    py_count = 0
    js_count = 0

    symbols: dict[str, list[dict]] = {}
    by_file: dict[str, list[str]] = {}

    git_files = _git_ls_files(repo_root)

    def _should_scan(file_path: Path) -> bool:
        rel = str(file_path.relative_to(repo_root))
        if git_files is not None:
            return rel in git_files
        return not _should_skip_dir(file_path.relative_to(repo_root).parts)

    def _add(rel_path: str, line: int, name: str, typ: str, language: str) -> None:
        entry = {"file": rel_path, "line": line, "type": typ, "language": language}
        symbols.setdefault(name, []).append(entry)

    # --- Python: AST crawl ---
    for py_file in repo_root.rglob("*.py"):
        if not _should_scan(py_file):
            continue
        try:
            tree = ast.parse(py_file.read_text())
        except (SyntaxError, OSError, UnicodeDecodeError):
            continue

        rel_path = str(py_file.relative_to(repo_root))
        py_count += 1
        file_names: list[str] = []

        class_scopes: dict[int, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        class_scopes[child.lineno] = node.name

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                _add(rel_path, node.lineno, node.name, "class", "python")
                file_names.append(node.name)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                typ = "method" if node.lineno in class_scopes else "function"
                _add(rel_path, node.lineno, node.name, typ, "python")
                file_names.append(node.name)

        if file_names:
            by_file[rel_path] = file_names

    # --- JavaScript / TypeScript: regex crawl ---
    for js_file in repo_root.rglob("*"):
        if js_file.suffix not in _JS_EXTENSIONS:
            continue
        if not _should_scan(js_file):
            continue
        try:
            text = js_file.read_text()
        except (OSError, UnicodeDecodeError):
            continue

        rel_path = str(js_file.relative_to(repo_root))
        js_count += 1
        file_names: list[str] = []

        for pat, typ in _JS_INDEX_PATTERNS:
            for m in pat.finditer(text):
                name = m.group(1)
                line = text[:m.start()].count("\n") + 1
                _add(rel_path, line, name, typ, "javascript")
                file_names.append(name)

        if file_names:
            by_file[rel_path] = file_names

    elapsed = time.monotonic() - start
    total = sum(len(v) for v in symbols.values())

    return {
        "symbols": symbols,
        "by_file": by_file,
        "stats": {
            "py_files": py_count,
            "js_files": js_count,
            "symbols": total,
            "unique_names": len(symbols),
            "elapsed_ms": int(elapsed * 1000),
        },
    }
