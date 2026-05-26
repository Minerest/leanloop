#!/usr/bin/env python3
"""lean-mcp server — ask_about tool that routes queries through the local LLM.

Single tool: ask_about(question, files)
  - Reads each file from disk
  - Sends question + file content to the local LLM one at a time (serial)
  - Skips files exceeding the char limit with a note
  - Returns structured JSON with per-file results

Config reused from lean-loop's config.toml:
  [lean] base_url, model
  [mcp]  max_file_chars (default 32000)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
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

LEAN = CONFIG.get("lean", {})
MCP_CFG = CONFIG.get("mcp", {})

LLM_URL = f"{LEAN.get('base_url', 'http://127.0.0.1:8080/v1')}/chat/completions"
LLM_MODEL = LEAN.get("model", "unknown")
LLM_API_KEY = LEAN.get("api_key", "not-needed")
MAX_FILE_CHARS = MCP_CFG.get("max_file_chars", 32000)

# ---------------------------------------------------------------------------
# LLM caller — direct POST to /v1/chat/completions (stdlib only)
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You are a precise code analyst. Given a file and a question about it, "
    "answer concisely and directly. Reference specific lines, symbols, or "
    "patterns from the file. Keep the response under 2000 characters."
)


def _call_llm(question: str, file_path: str, file_content: str, *, think: bool = False) -> str:
    """Send a single prompt to the local LLM and return the response text.

    Retries up to 2 times on empty content (thinking models sometimes
    finish in reasoning_content without writing to content).
    """
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"File: {file_path}\n\n"
                    f"{file_content}\n\n"
                    f"Question: {question}"
                ),
            },
        ],
        "temperature": 0.1,
        "max_tokens": 32768,
        "stream": False,
        "chat_template_kwargs": {"enable_thinking": think},
    }

    req = urllib.request.Request(
        LLM_URL,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LLM_API_KEY}",
        },
        method="POST",
    )

    max_retries = 2
    for attempt in range(max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                body = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode(errors="replace")[:200]
            except Exception:
                pass
            return f"[error] HTTP {e.code}: {detail}"
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            return f"[error] {e}"

        choices = body.get("choices", [])
        if not choices:
            return "[error] empty response from LLM"

        message = choices[0].get("message", {})
        content = message.get("content", "")
        if not content or not content.strip():
            # Thinking models put the actual answer in reasoning_content
            reasoning = message.get("reasoning_content", "")
            if reasoning and reasoning.strip():
                content = reasoning.strip()

        if content and content.strip() and not content.startswith("[error]"):
            return content.strip()

        # Retry on empty — models can transiently produce empty content
        if attempt < max_retries:
            import time
            time.sleep(1)

    return "[error] empty content after retries"


# ---------------------------------------------------------------------------
# File reading utilities
# ---------------------------------------------------------------------------

def _resolve_path(raw: str) -> str:
    """Resolve a file path; absolute paths pass through, relative paths resolve
    from the project root (parent of lean-mcp/ or CWD as fallback)."""
    if os.path.isabs(raw):
        return raw

    # Heuristic: look for the directory containing lean-mcp/
    script_dir = Path(__file__).resolve().parent
    if script_dir.name == "lean-mcp":
        project_root = script_dir.parent
    else:
        project_root = Path.cwd()

    return str(project_root / raw)


def _read_file(path: str) -> str | None:
    """Read a file; return None on failure."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError as e:
        return None  # caller handles reporting


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


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

server = FastMCP(
    "lean-ask",
    instructions=(
        "Ask questions about source files through the local LLM. The LLM runs "
        "on this machine at port 8080 and processes one file at a time."
    ),
)


@server.tool(
    name="ask_about",
    description=(
        "Ask a question about each file in a list. "
        "Returns per-file AI responses in a JSON array. "
        "Files exceeding the 32K-char limit are reported as skipped."
    ),
)
def ask_about(question: str, files: list[str], think: bool = False) -> str:
    """Ask the local LLM the same question about each file, serially.

    Args:
        question: The question to ask about each file.
        files: List of file paths (absolute, or relative to project root).
        think: Enable thinking/reasoning. Default false — answer directly.

    Returns:
        JSON string with per-file results.
    """
    results: list[dict] = []

    for fpath in files:
        resolved = _resolve_path(fpath)
        content = _read_file(resolved)

        if content is None:
            results.append({
                "file": fpath,
                "status": "error",
                "reason": f"cannot read file: {resolved}",
            })
            continue

        n_chars = len(content)
        if n_chars > MAX_FILE_CHARS:
            results.append({
                "file": fpath,
                "status": "skipped",
                "reason": (
                    f"file exceeds {MAX_FILE_CHARS}-char limit ({n_chars} chars)"
                ),
            })
            continue

        # Serial LLM call
        response = _call_llm(question, fpath, content, think=think)

        results.append({
            "file": fpath,
            "status": "ok",
            "response": response,
            "input_chars": n_chars,
            "output_chars": len(response),
        })

    # Append summary
    ok_count = sum(1 for r in results if r["status"] == "ok")
    skipped_count = sum(1 for r in results if r["status"] == "skipped")
    err_count = sum(1 for r in results if r["status"] == "error")

    summary = {
        "total": len(results),
        "ok": ok_count,
        "skipped": skipped_count,
        "error": err_count,
        "model": LLM_MODEL,
    }

    return json.dumps({"results": results, "summary": summary}, indent=2)


# ---------------------------------------------------------------------------
# Test runner — summary-first
# ---------------------------------------------------------------------------


@server.tool(
    name="run_tests",
    description=(
        "Run a test command and return structured results. "
        "On success, returns a compact summary line. "
        "On failure, returns full stdout/stderr (last 100 lines each)."
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
    if not cwd:
        # Same heuristic as _resolve_path
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

    # Summary-first: on success, compress to just the summary line
    if retcode == 0 and not timed_out:
        summary = _extract_pytest_summary(stdout)
        if summary:
            stdout = summary
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

    return json.dumps({
        "returncode": retcode,
        "timed_out": timed_out,
        "stdout": stdout,
        "stderr": stderr,
        "cwd": cwd,
        "command": command,
    }, indent=2)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
