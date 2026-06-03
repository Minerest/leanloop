#!/usr/bin/env python3
"""leanloop.py — task-driven auto-fix loop with local LLM.

Reads leanfile.toml. Supports two modes:

  1. Task mode ([[tasks]] in config) — iterate over bite-sized work items.
     Each task runs the model with a fresh context, applies the output,
     runs all tests, and enters an auto-fix loop if tests fail.

  2. Direct mode (no [[tasks]]) — legacy auto-fix loop that starts by
     running tests and fixing whatever breaks first.

The script assumes the LLM server (whatever serves the agent-CLI backend)
is already running and reachable at the configured health URL. It will
not start, stop, or escalate between servers. This keeps the runtime
fully cross-platform (no process-group / SIGKILL games).

Each model call is self-contained — messages array built fresh per call,
no history carryover between tasks or iterations.
"""

from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

# ===============================================================================
# Config
# ===============================================================================

# Default locations of the static config (lean runtime, health, base defaults).
# Per-project leanfile.toml deep-merges on top of this. Checked in order:
#   1. config.toml next to leanloop.py (works for clone-and-run)
#   2. ~/.config/lean-loop/config.toml  (works for pipx install)
STATIC_CONFIG_DEFAULTS = (
    Path(__file__).resolve().parent / "config.toml",
    Path.home() / ".config" / "lean-loop" / "config.toml",
)


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Deep-merge overlay into a copy of base.

    Tables (dicts) merge recursively. Arrays and scalars in overlay replace
    whatever is in base — we do not concatenate lists, since that would make
    it impossible to *override* a list value.
    """
    out = dict(base)
    for k, v in overlay.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _load_toml(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def load_config(task_path: str = "leanfile.toml", static_path: str | None = None) -> dict:
    """Load the static config + task config and deep-merge them.

    Static config discovery (first hit wins):
      1. `static_path` argument (from --global-config)
      2. $LEANLOOP_CONFIG env var
      3. config.toml alongside leanloop.py

    Static config is optional — if none is found we just return the task
    config. Task config is required.
    """
    task_p = Path(task_path)
    if not task_p.exists():
        print(f"[err] Task config not found: {task_path}")
        sys.exit(1)

    static_p: Path | None = None
    if static_path:
        static_p = Path(static_path)
        if not static_p.exists():
            print(f"[err] Static config not found: {static_path}")
            sys.exit(1)
    elif os.environ.get("LEANLOOP_CONFIG"):
        static_p = Path(os.environ["LEANLOOP_CONFIG"])
        if not static_p.exists():
            print(f"[err] $LEANLOOP_CONFIG points at missing file: {static_p}")
            sys.exit(1)
    else:
        for candidate in STATIC_CONFIG_DEFAULTS:
            if candidate.exists():
                static_p = candidate
                break

    task_cfg = _load_toml(task_p)
    if static_p is None:
        return task_cfg

    static_cfg = _load_toml(static_p)
    merged = _deep_merge(static_cfg, task_cfg)

    # Resolve lean.binary relative to the static config's directory (the
    # natural anchor — leaners/ ships alongside config.toml).
    lean_bin = merged.get("lean", {}).get("binary")
    if lean_bin and not os.path.isabs(lean_bin):
        merged.setdefault("lean", {})["binary"] = str(
            (static_p.parent / lean_bin).resolve()
        )

    return merged


# ===============================================================================
# Server health (cross-platform: uses urllib, no curl dependency)
# ===============================================================================

def health_check(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            return 200 <= resp.status < 500
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return False


def wait_for_server(url: str, max_wait: int) -> bool:
    print(f"Waiting for server (up to {max_wait}s)...", flush=True)
    for _ in range(max_wait // 2):
        if health_check(url):
            print("[ok] Server up")
            return True
        time.sleep(2)
    print("[err] Server not reachable — aborting")
    return False


def ensure_server(health_url: str, max_wait: int) -> bool:
    """Check server health; briefly wait if it's not up yet."""
    if health_check(health_url):
        return True
    return wait_for_server(health_url, max_wait)


# ---------------------------------------------------------------------------
# PID-file based stale-server cleanup
# ---------------------------------------------------------------------------

_LEAN_MCP_PID_FILE = "/tmp/lean-mcp.pid"


def _cleanup_stale_servers() -> int:
    """Kill any orphaned lean-mcp servers found via the PID file.

    An orphaned server is one whose parent process no longer exists
    (Qwen exited but the server didn't notice). Returns the number of
    servers killed.

    Leaves manually-started servers alone — if the parent is still alive,
    the server is assumed to be intentionally running.
    """
    if not os.path.exists(_LEAN_MCP_PID_FILE):
        return 0

    try:
        with open(_LEAN_MCP_PID_FILE) as f:
            pid_str = f.read().strip()
        pid = int(pid_str)
    except (OSError, ValueError):
        # Stale/unreadable PID file — remove it.
        try:
            os.remove(_LEAN_MCP_PID_FILE)
        except OSError:
            pass
        return 0

    # Check if the process still exists.
    try:
        os.kill(pid, 0)
    except OSError:
        # Process is gone — just clean up the stale PID file.
        try:
            os.remove(_LEAN_MCP_PID_FILE)
        except OSError:
            pass
        return 0

    # Process exists. Check if it's orphaned (parent is init, PID 1).
    # We use `ps -o ppid= -p <pid>` which works on both Linux and macOS.
    try:
        result = subprocess.run(
            ["ps", "-o", "ppid=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5,
        )
        ppid_str = result.stdout.strip()
        ppid = int(ppid_str) if ppid_str else 0
    except (subprocess.TimeoutExpired, ValueError, OSError):
        ppid = 0

    if ppid == 0:
        # Can't determine parent — be conservative, don't kill.
        return 0

    # Check if the parent is still alive.
    try:
        os.kill(ppid, 0)
    except OSError:
        # Parent is dead — this server is orphaned. Kill it.
        print(f"[mcp] killing orphaned server (pid={pid}, ppid={ppid} dead)")
        _kill_process(pid)
        try:
            os.remove(_LEAN_MCP_PID_FILE)
        except OSError:
            pass
        return 1

    # Parent is alive — server is intentionally running (manual or another
    # Qwen session). Leave it alone.
    return 0


def _kill_process(pid: int) -> None:
    """Kill a process gracefully (SIGTERM), then forcefully (SIGKILL)."""
    try:
        os.kill(pid, signal.SIGTERM)
        # Wait up to 2 seconds for graceful shutdown.
        for _ in range(20):
            time.sleep(0.1)
            try:
                os.kill(pid, 0)
            except OSError:
                return  # Process exited.
        # Force kill.
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass  # Process already gone.


# ===============================================================================
# Agent CLI harness (handles model calls + file writes natively)
# ===============================================================================

# Fallback if config has no [lean] section at all.
LEAN_DEFAULT_BINARY = str(Path(__file__).resolve().parent / "leaners" / "qwen.sh")
LEAN_MAX_EMPTY_RETRIES = 5


def preflight_lean(config: dict) -> bool:
    """Verify the wrapper exists, is executable, and has a model set.

    Catches the common misconfigurations once at startup instead of letting
    them surface as 5x retry warnings per fix iteration.
    """
    lcfg = config.get("lean", {})
    lean_bin = lcfg.get("binary", LEAN_DEFAULT_BINARY)

    if not os.path.isfile(lean_bin):
        print(f"[err] lean.binary not found: {lean_bin}")
        print("   Set [lean] binary in config.toml (or use the default at leaners/qwen.sh).")
        return False
    if not os.access(lean_bin, os.X_OK):
        print(f"[err] lean.binary is not executable: {lean_bin}")
        print(f"   Try: chmod +x {lean_bin}")
        return False
    if not lcfg.get("model"):
        print("[err] [lean] model is not set (config.toml or leanfile.toml).")
        return False
    return True


def _lean_env(config: dict) -> dict:
    """Build the env vars that the wrapper expects, from the merged [lean] config."""
    lcfg = config.get("lean", {})
    env = os.environ.copy()
    if "base_url" in lcfg:
        env["LEAN_BASE_URL"] = str(lcfg["base_url"])
    if "api_key" in lcfg:
        env["LEAN_API_KEY"] = str(lcfg["api_key"])
    if "model" in lcfg:
        env["LEAN_MODEL"] = str(lcfg["model"])
    if "approval_mode" in lcfg:
        env["LEAN_APPROVAL_MODE"] = str(lcfg["approval_mode"])
    if "auth_type" in lcfg:
        env["LEAN_AUTH_TYPE"] = str(lcfg["auth_type"])
    return env


# Token accounting (per task + grand total). Chars are accumulated at each
# lean_call site so both the generating call and any fix-loop iterations
# are credited to the active task. Tokens are estimated as chars/4 — the
# industry-standard rule of thumb for BPE-style tokenizers.
_TOKEN_STATS: dict = {
    "task_in": 0, "task_out": 0, "task_calls": 0,
    "total_in": 0, "total_out": 0, "total_calls": 0,
    "n_tasks": 0,
}


def _tok_reset_task() -> None:
    _TOKEN_STATS["task_in"] = 0
    _TOKEN_STATS["task_out"] = 0
    _TOKEN_STATS["task_calls"] = 0


def _tok_record(chars_in: int, chars_out: int) -> None:
    _TOKEN_STATS["task_in"] += chars_in
    _TOKEN_STATS["task_out"] += chars_out
    _TOKEN_STATS["task_calls"] += 1
    _TOKEN_STATS["total_in"] += chars_in
    _TOKEN_STATS["total_out"] += chars_out
    _TOKEN_STATS["total_calls"] += 1


def _fmt_k(n_tokens: float) -> str:
    """Compact 'k'-suffixed token count: 540 -> '0.5k', 12345 -> '12.3k'."""
    if n_tokens < 100:
        return f"{int(n_tokens)}t"
    return f"{n_tokens / 1000:.1f}k"


def _print_token_summary(label: str) -> None:
    """One-line token summary for the current scope (task or grand total)."""
    is_total = label.startswith("grand")
    chars_in = _TOKEN_STATS["total_in"] if is_total else _TOKEN_STATS["task_in"]
    chars_out = _TOKEN_STATS["total_out"] if is_total else _TOKEN_STATS["task_out"]
    n_calls = _TOKEN_STATS["total_calls"] if is_total else _TOKEN_STATS["task_calls"]
    if n_calls == 0:
        return  # nothing to report — task skipped or empty

    tok_in = chars_in / 4
    tok_out = chars_out / 4

    print(
        f"  [tok] {label}: ~{_fmt_k(tok_in)} in / ~{_fmt_k(tok_out)} out"
        f" [{n_calls} call{'s' if n_calls != 1 else ''}]"
    )


def lean_call(prompt: str, config: dict) -> str | None:
    """Run the wrapper with `-p prompt` and return stdout.

    The wrapped agent CLI handles model calls AND file writes natively — the
    model emits edit_file/write_file tool calls and the CLI executes them.

    Known quirk: some CLI/model combos occasionally exit 0 with empty stdout
    even though they ran tool calls successfully. Retry up to
    LEAN_MAX_EMPTY_RETRIES times on empty before giving up.
    Timeout and missing-binary failures are NOT retried.
    """
    lcfg = config.get("lean", {})
    lean_bin = lcfg.get("binary", LEAN_DEFAULT_BINARY)
    timeout = lcfg.get("timeout", 600)
    env = _lean_env(config)

    # Prepend global_message from [defaults] if set — applied to every
    # model call (task prompt, error summary, fix prompt). The constraint
    # (MCP tool-use requirement, etc.) lives in the toml config alongside
    # QWEN.md which still carries per-project conventions as a system prompt.
    _global = config.get("defaults", {}).get("global_message", "").strip()
    if _global:
        prompt = f"{_global}\n\n{prompt}"

    for attempt in range(1, LEAN_MAX_EMPTY_RETRIES + 1):
        try:
            result = subprocess.run(
                [lean_bin, "-p", prompt],
                capture_output=True, text=True, timeout=timeout, env=env,
            )
        except subprocess.TimeoutExpired:
            print("  [warn] wrapper timed out")
            return None
        except FileNotFoundError:
            print(f"  [warn] wrapper binary not found: {lean_bin}")
            return None

        output = result.stdout.strip() if result.stdout else ""
        if output:
            _tok_record(len(prompt), len(output))
            return output

        if attempt < LEAN_MAX_EMPTY_RETRIES:
            print(f"  [retry] wrapper returned empty (attempt {attempt}/{LEAN_MAX_EMPTY_RETRIES}), retrying...")

    return None


def lean_oneshot(prompt: str, config: dict, label: str = "") -> str | None:
    """Call the wrapper, print a status line, return the output."""
    if label:
        print(f"  {label}...", end=" ", flush=True)
    result = lean_call(prompt, config)
    if label:
        if result:
            print(f"ok ({len(result)} chars)")
        else:
            print("[warn] empty")
    return result


# ===============================================================================
# Git helpers
# ===============================================================================

def git_diff_short(filepath: str | None, root: str) -> str:
    """First 30 lines of git diff for a file, or whole project if None."""
    try:
        cmd = ["git", "diff"]
        if filepath:
            cmd.append(filepath)
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=5, cwd=root,
        )
        lines = result.stdout.splitlines()[:30]
        return "\n".join(lines) if lines else "(no diff)"
    except Exception:
        return "(no git diff available)"


# ===============================================================================
# (Model API removed — all model calls go through the wrapper's `-p` now)
# ===============================================================================


# ===============================================================================
# Traceback parsing
# ===============================================================================

def parse_project_frames(
    traceback_text: str, project_root: str
) -> list[dict]:
    """Extract all project-relative frames from a traceback.

    Returns list of {"file", "line", "function", "relative"}, outermost first.
    """
    project_root = os.path.abspath(project_root)
    frames = []
    for line in traceback_text.splitlines():
        m = re.match(r'  File "([^"]+)", line (\d+)(?:, in (\S+))?', line)
        if not m:
            continue
        filepath = os.path.abspath(m.group(1))
        lineno = int(m.group(2))
        funcname = m.group(3) or "?"

        rel = os.path.relpath(filepath, project_root)
        if rel.startswith(".."):
            continue
        if "venv" in rel.split(os.sep) or "site-packages" in rel.split(os.sep):
            continue

        frames.append({
            "file": filepath,
            "line": lineno,
            "function": funcname,
            "relative": rel,
        })
    return frames


def _as_prefix_list(value) -> list[str]:
    """Normalize a string-or-list config value into a list of strings."""
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


def pick_target_frame(frames: list[dict], cfg: dict) -> dict | None:
    """Pick the best frame to target for a fix.

    Preference order:
      1. deepest frame under any path in [defaults] source_prefix
      2. deepest frame NOT under any path in [defaults] test_prefix
      3. first frame
    """
    if not frames:
        return None
    defaults = cfg.get("defaults", {})
    source_prefixes = _as_prefix_list(defaults.get("source_prefix"))
    test_prefixes = _as_prefix_list(defaults.get("test_prefix")) or ["tests/", "test_", "scripts/"]

    if source_prefixes:
        for f in reversed(frames):
            if any(f["relative"].startswith(p) for p in source_prefixes):
                return f

    for f in reversed(frames):
        rel = f["relative"]
        if not any(p in rel for p in test_prefixes):
            return f
    return frames[0]


def resolve_called_function(
    frame: dict, project_root: str, cfg: dict
) -> dict | None:
    """If frame is a test file, find the actual function being called.

    Reads the failing line, extracts function calls, searches for definitions
    inside the configured source_prefix (or project_root if unset).
    """
    defaults = cfg.get("defaults", {})
    test_prefixes = _as_prefix_list(defaults.get("test_prefix")) or ["test_", "scripts/"]
    if not any(p in frame["relative"] for p in test_prefixes):
        return None

    try:
        with open(frame["file"]) as f:
            lines = f.readlines()
    except OSError:
        return None

    line_idx = frame["line"] - 1
    if line_idx < 0 or line_idx >= len(lines):
        return None

    line_text = lines[line_idx]
    calls = re.findall(r"(?:\.)?(\w+)\s*\(", line_text)
    if not calls:
        return None

    search_roots = _as_prefix_list(defaults.get("source_prefix")) or ["."]

    for func_name in reversed(calls):
        for search_root in search_roots:
            search_abs = os.path.join(project_root, search_root)
            if not os.path.isdir(search_abs):
                continue
            match = _find_def(func_name, search_abs)
            if not match:
                continue
            source_abs, source_line = match
            source_rel = os.path.relpath(source_abs, project_root)
            return {
                "file": source_abs,
                "line": source_line,
                "function": func_name,
                "relative": source_rel,
            }
    return None


def _find_def(func_name: str, root: str) -> tuple[str, int] | None:
    """Pure-Python `grep -rn "def func_name"` — portable to Windows.

    Walks `root`, scans .py files, returns (abs_path, line_number) of the
    first match.
    """
    needle = f"def {func_name}"
    for dirpath, dirnames, filenames in os.walk(root):
        # Skip the usual suspects.
        dirnames[:] = [d for d in dirnames if d not in {".git", "venv", ".venv", "__pycache__", "node_modules", ".tox"}]
        for fname in filenames:
            if not fname.endswith(".py"):
                continue
            fpath = os.path.join(dirpath, fname)
            try:
                with open(fpath, encoding="utf-8", errors="replace") as f:
                    for i, line in enumerate(f, 1):
                        if needle in line:
                            return fpath, i
            except OSError:
                continue
    return None


# ===============================================================================
# Source context
# ===============================================================================

def read_source_window(filepath: str, line: int, window: int = 30) -> str:
    """Read a window of lines around the given line number.
    Auto-shrinks if >12k chars. Marks the failing line with >>>.
    """
    try:
        with open(filepath) as f:
            lines = f.readlines()
    except FileNotFoundError:
        return f"(file not found: {filepath})"

    start = max(0, line - window - 1)
    end = min(len(lines), line + window)
    result = []
    for i in range(start, end):
        prefix = ">>>" if i == line - 1 else "   "
        result.append(f"{prefix} {i + 1:5d} | {lines[i]}")

    out = "".join(result)
    if len(out) > 12000:
        mid = line - 1
        half = window // 2
        start = max(0, mid - half)
        end = min(len(lines), mid + half)
        result = []
        for i in range(start, end):
            prefix = ">>>" if i == line - 1 else "   "
            result.append(f"{prefix} {i + 1:5d} | {lines[i]}")
        out = "".join(result)
    return out


_NOISE_SUBSTRINGS = (
    "site-packages",   # Python venv
    "venv/", "venv\\", # Python venv
    "node_modules",    # JS / TS
    "/vendor/",        # Go / PHP / Rust vendored deps
    ".cargo/registry", # Rust deps
    "For further",     # pytest "For further information" tail
)


def compress_traceback(tb_text: str, cfg: dict | None = None) -> str:
    """Strip well-known dependency-noise lines, keep the tail.

    Tail size is `defaults.error_tail_lines` (default 40). This is the
    language-agnostic fallback — most test runners (pytest, go test, jest,
    cargo test, rspec) put the failing assertion at the bottom, so a tail
    captures the meat regardless of language.
    """
    tail = 40
    if cfg is not None:
        tail = cfg.get("defaults", {}).get("error_tail_lines", 40)
    lines = tb_text.splitlines()
    filtered = [l for l in lines if not any(s in l for s in _NOISE_SUBSTRINGS)]
    return "\n".join(filtered[-tail:])


# ===============================================================================
# Test runner
# ===============================================================================

def run_tests(config: dict, cwd: str | None = None) -> tuple[int, str]:
    """Run the test command from config. Returns (returncode, output).

    Config:
      [runner]
      command = "./venv/bin/pytest"     # any executable or shell command
      args = ["tests/", "-x", "-q"]     # optional list of args
      timeout = 30                      # optional, seconds
      shell = false                     # optional, run via shell (default false)
      env = { FOO = "bar" }             # optional, extra env vars

    Args:
      cwd: Override working directory. Falls back to config's
           defaults.project_root when not set.
    """
    runner = config["runner"]
    cmd = runner["command"]
    args = runner.get("args", [])
    use_shell = bool(runner.get("shell", False))

    env = None
    if runner.get("env"):
        env = os.environ.copy()
        env.update({str(k): str(v) for k, v in runner["env"].items()})

    if use_shell:
        # Treat command as a shell line; ignore args.
        full_cmd = cmd
        printable = cmd
    else:
        full_cmd = [cmd] + list(args)
        printable = " ".join(full_cmd)

    if cwd is None:
        cwd = config.get("defaults", {}).get("project_root", ".")

    print(f"  $ {printable}")
    try:
        result = subprocess.run(
            full_cmd,
            shell=use_shell,
            capture_output=True, text=True,
            timeout=runner.get("timeout", 30),
            cwd=cwd,
            env=env,
        )
        return result.returncode, result.stdout + "\n" + result.stderr
    except subprocess.TimeoutExpired:
        return -1, "TIMEOUT"
    except FileNotFoundError as e:
        return -2, f"Command not found: {e}"


# ===============================================================================
# Model prompt stages
# ===============================================================================

def check_quality(diagnosis: str) -> bool:
    """Reject empty, too-short, or vagued-out diagnoses."""
    d = diagnosis.strip()
    if not d or len(d) < 20:
        return False
    if not re.search(r"(line \d+|function|\.py|error|exception)", d, re.I):
        return False
    return True


def summarize_error(tb_compressed: str, config: dict) -> str | None:
    """Stage 1: summarize the error output in one sentence via the wrapper."""
    prompt = config.get("prompts", {}).get(
        "summary",
        "You are a debugger. Summarize this test failure in ONE sentence. "
        "Name the file, line number, and root cause when discoverable. "
        "Output ONLY the summary, no code, no markdown.",
    )
    full_prompt = f"{prompt}\n\n```\n{tb_compressed}\n```"
    result = lean_call(full_prompt, config)
    return result.strip() if result else None


def generate_fix(
    diagnosis: str,
    source_context: str,
    git_diff: str,
    target_file: str,
    config: dict,
) -> bool:
    """Stage 2: fix the bug using the wrapped agent CLI.

    The prompt includes the diagnosis and source context. The wrapper runs
    the model and applies any file writes directly. Returns True if the
    output was non-empty (file writes handled by the wrapped CLI).
    """
    prompt = (
        f"Bug in {target_file}:\n{diagnosis}\n\n"
        f"Git diff:\n{git_diff}\n\n"
        f"Source code (>>> marks the failing line):\n{source_context}\n\n"
        f"Fix the root cause. Apply the fix to the file."
    )
    result = lean_oneshot(prompt, config, label="fix")
    return bool(result)





# ===============================================================================
# Fix loop (one per task or direct mode)
# ===============================================================================

def run_fix_loop(cfg: dict, label: str = "fix loop", task: dict | None = None) -> bool:
    """Run the auto-fix loop: test -> diagnose -> fix -> retry.

    Two paths per iteration:
      Deluxe (Python frames parse): extract failing file:line, dump source
        window around it, plus the called function definition. Sharp context.
      Fallback (no frames): use the compressed error tail + git diff + the
        task's `files` (if any). Language-agnostic, coarser context.

    Returns True if tests pass within the iteration budget, False otherwise.
    """
    defaults = cfg.get("defaults", {})
    max_iters = defaults.get("max_iters_per_task", 5)
    source_window = defaults.get("source_window", 30)
    project_root = os.path.abspath(defaults.get("project_root", "."))
    health_url = cfg.get("health", {}).get("check_url", "http://127.0.0.1:8080/health")
    max_wait = cfg.get("health", {}).get("max_wait", 60)

    print(f"  {label}: {max_iters} max iterations")

    for i in range(1, max_iters + 1):
        print(f"  --- iteration {i}/{max_iters} ---")

        if not ensure_server(health_url, max_wait):
            return False

        retcode, output = run_tests(cfg)
        if retcode == 0:
            print("  [ok] Tests passed")
            return True
        if retcode == 5:
            # pytest exit code 5 = no tests collected or all skipped.
            # (importorskip when the implementation module doesn't exist yet.)
            # Not a real failure — treat as pass.
            print("  [ok] All tests skipped (exit 5) — module not ready yet")
            return True
        if retcode == -1:
            print("  [warn] Timeout")
            if i >= max_iters:
                return False
            continue
        if retcode == -2:
            print(f"  [warn] Runner error:\n{output}")
            return False

        compressed = compress_traceback(output, cfg)
        if not compressed.strip():
            print("  [warn] No output to diagnose")
            if i >= max_iters:
                return False
            continue

        # -- Try the Python frame-based deluxe path -------------------
        frames = parse_project_frames(output, project_root)
        frame = pick_target_frame(frames, cfg) if frames else None

        source = ""
        diff = ""
        target = "(unknown)"

        if frame:
            source_frame = resolve_called_function(frame, project_root, cfg)
            if source_frame:
                print(f"  {frame['relative']}:{frame['line']} -> {source_frame['relative']}:{source_frame['line']}")
                test_src = read_source_window(frame["file"], frame["line"], source_window)
                func_src = read_source_window(source_frame["file"], source_frame["line"], source_window)
                source = (
                    f"--- Test code ({frame['relative']}) ---\n{test_src}\n"
                    f"--- Source function ({source_frame['relative']}) ---\n{func_src}"
                )
                diff = git_diff_short(None, project_root)
                target = source_frame["relative"]
            else:
                print(f"  {frame['relative']}:{frame['line']} in {frame['function'] or '?'}")
                window = read_source_window(frame["file"], frame["line"], source_window)
                if not window.startswith("(file not found"):
                    source = window
                    diff = git_diff_short(frame["relative"], project_root)
                    target = frame["relative"]
                else:
                    frame = None  # force fallback below

        if not frame:
            # -- Fallback: language-agnostic, no source window ---------
            print("  Fallback: no parseable frame — using error tail + diff")
            diff = git_diff_short(None, project_root)
            if task and task.get("files"):
                source = build_task_context(task, cfg)
                target = ", ".join(task["files"])
            else:
                source = "(no source context available)"

        # -- Stage 1: summarize (optional) -----------------------------
        if defaults.get("summarize_errors", True):
            print("  Stage 1: summarize...", end=" ", flush=True)
            diagnosis = summarize_error(compressed, cfg)
            if not diagnosis:
                print("[warn] Empty")
                if ensure_server(health_url, max_wait):
                    diagnosis = summarize_error(compressed, cfg)
                if not diagnosis:
                    if not ensure_server(health_url, max_wait):
                        return False
                    continue
            if not check_quality(diagnosis):
                print(f"[warn] Poor: {diagnosis[:80]}")
                if i >= max_iters:
                    return False
                continue
            print(f"ok {diagnosis}")
        else:
            diagnosis = compressed
            print(f"  Stage 1 skipped — passing trimmed error tail ({len(compressed)} chars)")

        # -- Stage 2: fix via wrapper (handles file writes natively) -----
        ok = generate_fix(diagnosis, source, diff, target, cfg)
        if not ok:
            if not ensure_server(health_url, max_wait):
                return False
            continue

    print(f"  [err] {label}: iterations exhausted")
    return False


# ===============================================================================
# Task system
# ===============================================================================

def build_task_context(task: dict, cfg: dict) -> str:
    """Read the task's files and combine into a context string.

    Each file is annotated with its path. The total stays within
    the task's context_budget, or a default of 24k chars.
    """
    project_root = os.path.abspath(
        cfg.get("defaults", {}).get("project_root", ".")
    )
    budget = task.get("context_budget", 24000)
    files = task.get("files", [])
    parts = []
    remaining = budget

    for fpath in files:
        abs_path = os.path.join(project_root, fpath) if not os.path.isabs(fpath) else fpath
        try:
            with open(abs_path) as f:
                content = f.read()
        except OSError as e:
            parts.append(f"# {fpath} — ERROR: {e}")
            continue

        header = f"# -- {fpath} --\n"
        total = len(header) + len(content)
        if remaining - total < 0 and parts:
            # Truncate to fit budget
            max_content = max(0, remaining - len(header) - 200)
            if max_content > 100:
                content = content[:max_content] + "\n# ... (truncated)"
                total = len(header) + len(content)

        parts.append(header + content)
        remaining -= total
        if remaining < 200:
            break

    return "\n".join(parts)


def _run_after_commands(commands: list[str], cwd: str, timeout: int = 600) -> bool:
    """Run a sequence of shell commands after a task succeeds.

    Each command runs via bash -c with cwd=project_root. Stdout is echoed.
    Returns False on the first non-zero exit, True if all succeed.

    `timeout` is per-command (seconds). Overridable via the toml's
    `[defaults] after_command_timeout`.
    """
    for cmd in commands:
        print(f"  > {cmd}")
        try:
            result = subprocess.run(
                cmd, shell=True, cwd=cwd,
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            print("    [warn] timed out")
            return False
        for line in (result.stdout or "").splitlines():
            print(f"    {line}")
        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            if stderr:
                print(f"    [err] stderr: {stderr[:300]}")
            print(f"  [err] after-command failed (exit {result.returncode})")
            return False
    return True


def _task_cfg(cfg: dict, task: dict) -> dict:
    """Return cfg with [runner] deep-merged from task.runner if present.

    Lets a task scope which tests run (or use a different runner entirely)
    without touching the global [runner]. Anything the task omits inherits
    from the top-level table.
    """
    task_runner = task.get("runner")
    if not task_runner:
        return cfg
    merged = dict(cfg)
    merged["runner"] = _deep_merge(cfg.get("runner", {}), task_runner)
    return merged


def process_task(task: dict, cfg: dict) -> bool:
    """Process one task: model call -> tests -> fix loop -> after-commands."""

    _tok_reset_task()

    # -- Build context -------------------------------------------------
    print(f"\n  Reading files...", flush=True)
    context = build_task_context(task, cfg)
    ctx_len = len(context)
    print(f"     Context: {ctx_len} chars across {len(task.get('files', []))} file(s)")

    task_prompt = task["prompt"]
    project_root = os.path.abspath(
        cfg.get("defaults", {}).get("project_root", ".")
    )

    # -- Fresh wrapper call (handles file writes natively) -----------------

    # Global message is injected by lean_call() via [defaults] global_message
    # in the toml config — no longer handled here.

    wrapper_prompt = (
        f"Task: {task_prompt}\n\n"
        f"Project files:\n{context}\n\n"
        f"Current git diff:\n```diff\n{git_diff_short(None, project_root)}\n```"
    )
    output = lean_oneshot(wrapper_prompt, cfg, label="generating")
    if not output:
        print("  [warn] Model returned nothing — skipping task")
        return False
    print(f"  {output[:200]}")

    # -- Run tests ------------------------------------------------------
    eff_cfg = _task_cfg(cfg, task)
    if task.get("skip_tests", False):
        print(f"  [skip] tests skipped (skip_tests=true)")
        passed = True
    else:
        test_files = task.get("test_files")
        if test_files:
            # Per-task scoping — run only the listed test files instead of
            # the global runner.args. Keeps feedback fast and eliminates
            # importorskip noise from unrelated features.
            task_cfg = dict(eff_cfg)
            task_cfg["runner"] = dict(eff_cfg.get("runner", {}))
            task_cfg["runner"]["args"] = list(test_files)
            print(f"  Running tests ({len(test_files)} file(s))...", flush=True)
            retcode, test_output = run_tests(task_cfg)
        else:
            print(f"  Running tests...", flush=True)
            retcode, test_output = run_tests(eff_cfg)
        if retcode == 0:
            print(f"  [ok] All tests pass")
            passed = True
        else:
            print(f"  [err] Tests failed — entering fix loop")
            passed = run_fix_loop(eff_cfg, label=f"fix loop for '{task['name']}'", task=task)

    if not passed:
        if cfg.get("defaults", {}).get("log_token_savings", True):
            _print_token_summary(f"task tokens ({task.get('name', '?')}, FAILED)")
        return False

    # -- Post-task hooks -----------------------------------------------
    after = task.get("after", [])
    if after:
        print(f"  Running {len(after)} after-command(s)")
        after_timeout = cfg.get("defaults", {}).get("after_command_timeout", 600)
        if not _run_after_commands(after, project_root, timeout=after_timeout):
            return False

    if cfg.get("defaults", {}).get("log_token_savings", True):
        _print_token_summary(f"task tokens ({task.get('name', '?')})")
    _TOKEN_STATS["n_tasks"] += 1
    return True


# ===============================================================================
# Sample config generator
# ===============================================================================

SAMPLE_CONFIG = """\
# leanfile.toml — per-project task config.
# Generated by leanloop.py --init
#
# This file deep-merges on top of the static config.toml that ships with
# leanloop.py (lean runtime, health URL, base defaults). Only put
# project-specific stuff here; override static defaults as needed.
#
# Two modes:
#   Task mode: define one or more [[tasks]] — each is a bite-sized work
#              item with a fresh model context. After each task, tests run.
#              If they fail, an auto-fix loop kicks in for that task.
#   Direct mode: no [[tasks]] -> runs the fix loop straight against tests.
#
# The LLM server (e.g. llama-server) MUST be running and reachable at the
# configured health URL before launching leanloop.py.

[runner]
# Fully generic. `command` is run with `args` appended. Whatever your
# language's test runner is — pytest, go test, npm test, cargo test,
# make check — point it here.
command = "./venv/bin/pytest"          # any executable or absolute path
args    = ["tests/", "-x", "--tb=native", "-q"]
timeout = 30                           # seconds
# shell = false                        # set true to run `command` as a shell line (ignores args)
# env = { PYTHONDONTWRITEBYTECODE = "1" }  # optional extra env vars

[defaults]
# Where your *source* code lives. Optional. If set, the (Python) traceback
# parser prefers frames under these paths and searches them for function
# defs. Accepts a single string or a list.
# source_prefix = "src/"
# source_prefix = ["src/", "lib/"]

# Path fragments that mark *test* files. Used to fall back from test frames
# to the underlying source. Defaults to ["tests/", "test_", "scripts/"].
# test_prefix = ["tests/", "test_"]

# Override any static default here:
# project_root      = "."
# source_window     = 30
# error_tail_lines  = 40
# max_iters_per_task = 5
# summarize_errors  = true   # set false to skip the Stage 1 summarization
                             # LLM call and feed the trimmed error tail
                             # straight into the fix call.

# -- Lean overrides (optional) ------------------------------------------------
# Anything not set here falls back to config.toml.
# [lean]
# model   = "some-other-model.gguf"
# timeout = 900

# -- Prompt overrides (optional, advanced) ------------------------------------
# Replace the stock summarization prompt fed to the model on Stage 1 of the
# fix loop. Useful if you want domain-specific framing (e.g. "you are a Rust
# debugger" instead of the generic default).
# [prompts]
# summary = "You are a Go debugger. Summarize this test failure in ONE sentence. Name the failing test, the assertion, and the likely cause. Output ONLY the summary."

# -- Tasks --------------------------------------------------------------------
# Each [[tasks]] block defines one work item. Uncomment and edit to use.

# [[tasks]]
# name = "my-task"
# prompt = "Describe what to do here. Be specific about the file and function."
# files = ["path/to/file.py"]
# # context_budget = 8000  # optional: max chars of file content sent to the model
# # after = [                  # optional: shell commands run after tests pass
# #   "wc -l data/seed.yaml",
# # ]
# # Optional per-task runner override — deep-merges over the top-level
# # [runner]. Scope tests to just this task so fix-loop iterations don't
# # run the whole suite. Any pytest selector / -k expr / file path works.
# # runner = { args = ["tests/test_my_feature.py", "-x", "--tb=native", "-q"] }
# #
# # Optional: skip the test phase entirely (and the fix loop). Use when the
# # held-out check lives in `after` instead — e.g. convention probes, doc
# # tasks, or bugfixes where you don't want the full bench suite re-run.
# # skip_tests = true
"""


def _write_sample_config(dest: str) -> None:
    """Write the sample config to stdout or a file."""
    if dest == "-":
        print(SAMPLE_CONFIG)
    else:
        path = Path(dest)
        path.write_text(SAMPLE_CONFIG)
        print(f"[ok] Wrote sample config to {path.resolve()}")


# ===============================================================================
# Leaner management (--list-leaners / --set-leaner)
# ===============================================================================

def _bundled_leaners_dir() -> Path:
    return Path(__file__).resolve().parent / "leaners"


def _find_static_config() -> Path | None:
    """Mirror load_config's static-config discovery, minus the CLI override."""
    env_p = os.environ.get("LEANLOOP_CONFIG")
    if env_p:
        p = Path(env_p)
        return p if p.exists() else None
    for candidate in STATIC_CONFIG_DEFAULTS:
        if candidate.exists():
            return candidate
    return None


def _update_lean_binary(config_path: Path, new_binary: str) -> None:
    """Regex-update `binary = "..."` under [lean] in a TOML file.

    Preserves surrounding comments and formatting. If [lean] is missing,
    appends it. If the key is missing inside [lean], inserts the line at the
    top of the section.
    """
    text = config_path.read_text()
    lean_header = re.compile(r"^\[lean\]\s*$", re.MULTILINE)
    m = lean_header.search(text)

    if not m:
        suffix = "" if text.endswith("\n") else "\n"
        config_path.write_text(f'{text}{suffix}\n[lean]\nbinary = "{new_binary}"\n')
        return

    section_start = m.end()
    next_header = re.search(r"^\[[^\]]+\]\s*$", text[section_start:], re.MULTILINE)
    section_end = section_start + next_header.start() if next_header else len(text)
    section = text[section_start:section_end]

    binary_line = re.compile(r'^(\s*binary\s*=\s*)"[^"]*"(.*)$', re.MULTILINE)
    if binary_line.search(section):
        new_section = binary_line.sub(
            lambda mm: f'{mm.group(1)}"{new_binary}"{mm.group(2)}',
            section, count=1,
        )
    else:
        new_section = f'\nbinary = "{new_binary}"' + section

    config_path.write_text(text[:section_start] + new_section + text[section_end:])


def _list_leaners() -> int:
    leaners_dir = _bundled_leaners_dir()
    if not leaners_dir.is_dir():
        print(f"[err] No leaners/ directory at {leaners_dir}")
        return 1

    active = None
    static_p = _find_static_config()
    if static_p:
        cfg = _load_toml(static_p)
        binary = cfg.get("lean", {}).get("binary")
        if binary:
            if not os.path.isabs(binary):
                binary = str((static_p.parent / binary).resolve())
            active = Path(binary).resolve()

    wrappers = sorted(
        p for p in leaners_dir.iterdir()
        if p.is_file() and os.access(p, os.X_OK)
    )
    if not wrappers:
        print(f"(no executable wrappers in {leaners_dir})")
        return 0

    print(f"Wrappers in {leaners_dir}:")
    for w in wrappers:
        mark = " <- active" if active and active == w.resolve() else ""
        print(f"  {w.name}{mark}")
    if static_p:
        print(f"\nActive config: {static_p}")
    return 0


def _set_leaner(name: str) -> int:
    leaners_dir = _bundled_leaners_dir()
    target = leaners_dir / name
    if not target.exists() and (leaners_dir / f"{name}.sh").exists():
        target = leaners_dir / f"{name}.sh"
    if not target.exists():
        print(f"[err] No wrapper '{name}' (or '{name}.sh') in {leaners_dir}")
        return 1
    if not os.access(target, os.X_OK):
        print(f"[err] {target} is not executable. Try: chmod +x {target}")
        return 1

    static_p = _find_static_config() or STATIC_CONFIG_DEFAULTS[0]
    if not static_p.exists():
        print(f"[err] No static config to update. Expected at {static_p}")
        return 1

    abs_target = str(target.resolve())
    _update_lean_binary(static_p, abs_target)
    print(f'[ok] {static_p}: [lean] binary = "{abs_target}"')
    return 0


# ===============================================================================
# Main
# ===============================================================================

def _run_workload(cfg: dict, args) -> bool:
    """Run the configured workload (tasks or direct fix loop)."""
    tasks = cfg.get("tasks", [])

    if not tasks:
        print("=== leanloop.py — direct fix loop ===")
        return run_fix_loop(cfg, label="direct loop")

    task_list = tasks
    if args.tasks:
        indices: list[int] = []
        for part in args.tasks.split(","):
            part = part.strip()
            try:
                idx = int(part)
            except ValueError:
                print(f"  [warn] Invalid task index '{part}' — skipping")
                continue
            if idx < 1 or idx > len(tasks):
                print(f"  [warn] Task index {idx} is out of range (1–{len(tasks)}) — skipping")
                continue
            indices.append(idx)
        task_list = [tasks[i - 1] for i in indices]
        if not task_list:
            print("[err] No valid task indices specified")
            return False
    elif args.task:
        task_list = [t for t in tasks if t.get("name") == args.task]
        if not task_list:
            print(f"[err] No task named '{args.task}'")
            return False

    print(f"=== leanloop.py — {len(task_list)} task(s) ===")

    for idx, task in enumerate(task_list):
        name = task.get("name", f"unnamed-{idx + 1}")
        print(f"\n{'=' * 60}")
        print(f"  Task {idx + 1}/{len(task_list)}: {name}")
        print(f"{'=' * 60}")

        if not process_task(task, cfg):
            print(f"\n[err] Task '{name}' failed")
            return False

    print(f"\n{'=' * 60}")
    print("[ok] All tasks completed")

    if cfg.get("defaults", {}).get("log_token_savings", True):
        n = _TOKEN_STATS["n_tasks"]
        _print_token_summary(f"grand total across {n} task{'s' if n != 1 else ''}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Task-driven auto-fix loop using a local LLM. "
                    "Requires the LLM server to already be running at the "
                    "configured health URL.",
    )
    parser.add_argument(
        "-c", "--config", nargs="+", default=["leanfile.toml"],
        metavar="CONFIG",
        help="Path(s) to per-project task config (default: leanfile.toml). "
             "When multiple are given, they run sequentially in order. "
             "If any config fails, remaining configs are skipped.",
    )
    parser.add_argument(
        "-g", "--global-config", default=None, dest="global_config",
        help="Path to static config (default: $LEANLOOP_CONFIG or "
             "config.toml alongside this script)",
    )
    parser.add_argument(
        "--task", help="Run only this task name (skips others)",
    )
    parser.add_argument(
        "--tasks", metavar="INDICES",
        help="Comma-separated 1-based task indices to run, e.g. '4,5,6,7'. "
             "Out-of-range indices are logged and skipped. Overrides --task if both are given.",
    )
    parser.add_argument(
        "--init", nargs="?", const="-", metavar="FILE",
        help="Write a sample task config FILE and exit (default: stdout)",
    )
    parser.add_argument(
        "--list-leaners", action="store_true", dest="list_leaners",
        help="List wrapper scripts in leaners/ (marks the active one) and exit",
    )
    parser.add_argument(
        "--set-leaner", metavar="NAME", dest="set_leaner",
        help="Set [lean] binary in the active static config to leaners/NAME and exit",
    )
    args = parser.parse_args()

    if args.init:
        _write_sample_config(args.init)
        return 0

    if args.list_leaners:
        return _list_leaners()

    if args.set_leaner:
        return _set_leaner(args.set_leaner)

    # Kill any orphaned lean-mcp servers left over from crashed Qwen sessions.
    # A fresh server will be spawned by Qwen when we make our first lean_call().
    _cleanup_stale_servers()

    config_paths = args.config
    multi = len(config_paths) > 1

    # Cross-config token totals — only meaningful when multi=True.
    cross_in = cross_out = cross_calls = cross_tasks = 0
    last_cfg: dict | None = None

    for idx, path in enumerate(config_paths):
        if multi:
            print(f"\n{'#' * 60}")
            print(f"# Config {idx + 1}/{len(config_paths)}: {path}")
            print(f"{'#' * 60}")

        cfg = load_config(path, args.global_config)
        last_cfg = cfg

        if not preflight_lean(cfg):
            return 1

        health_url = cfg.get("health", {}).get("check_url", "http://127.0.0.1:8080/health")
        max_wait = cfg.get("health", {}).get("max_wait", 60)
        if not ensure_server(health_url, max_wait):
            print(
                f"[err] LLM server not reachable at {health_url}. "
                f"Start it (e.g. `./leaners/qwen.sh` or your llama-server launcher) and rerun."
            )
            return 1

        # Reset per-config totals so each config gets its own "grand total" line.
        # The cross-config grand-grand-total below sums the deltas.
        if idx > 0:
            _TOKEN_STATS["total_in"] = 0
            _TOKEN_STATS["total_out"] = 0
            _TOKEN_STATS["total_calls"] = 0
            _TOKEN_STATS["n_tasks"] = 0

        if not _run_workload(cfg, args):
            return 1

        cross_in += _TOKEN_STATS["total_in"]
        cross_out += _TOKEN_STATS["total_out"]
        cross_calls += _TOKEN_STATS["total_calls"]
        cross_tasks += _TOKEN_STATS["n_tasks"]

    if multi and last_cfg is not None:
        print(f"\n{'#' * 60}")
        print(f"[ok] All {len(config_paths)} configs completed")
        if last_cfg.get("defaults", {}).get("log_token_savings", True):
            _TOKEN_STATS["total_in"] = cross_in
            _TOKEN_STATS["total_out"] = cross_out
            _TOKEN_STATS["total_calls"] = cross_calls
            _TOKEN_STATS["n_tasks"] = cross_tasks
            _print_token_summary(
                f"grand total across {len(config_paths)} configs "
                f"({cross_tasks} task{'s' if cross_tasks != 1 else ''})",
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
