#!/usr/bin/env bash
# leaners/aider.sh — wrapper around aider (https://aider.chat).
# All settings come from LEAN_* env vars (populated by leanloop.py from config.toml).
#
# Requires: `aider` on PATH.
#
# LEAN_MODEL examples: gpt-4o, claude-sonnet-4-6, deepseek/deepseek-coder,
#                      openai/Qwen3.6-35B-A3B-UD-Q4_K_M.gguf (for local).
# LEAN_BASE_URL: optional — for OpenAI-compatible local endpoints (llama-server etc).
# LEAN_APPROVAL_MODE: anything except "off" / "interactive" / "" passes
#                    --yes-always (auto-confirm aider's prompts).
#
# Note: aider only edits files it can "see" via its repomap. For a tiny
# project that's automatic; for a big repo you may need to point [defaults]
# project_root at the relevant subtree, or extend this wrapper to pass
# --file flags from the task's files list.
set -euo pipefail

# Translate leanloop's `-p <prompt>` convention to aider's `--message`.
if [ "${1:-}" = "-p" ] && [ -n "${2:-}" ]; then
  PROMPT="$2"
  shift 2
else
  echo "leaners/aider.sh: expected '-p <prompt>' as first two args" >&2
  exit 2
fi

# Hand the API key to whichever provider aider routes to via the model name.
export OPENAI_API_KEY="${LEAN_API_KEY:?LEAN_API_KEY not set (see config.toml)}"
export ANTHROPIC_API_KEY="$LEAN_API_KEY"

EXTRA=()
if [ -n "${LEAN_BASE_URL:-}" ]; then
  EXTRA+=(--openai-api-base "$LEAN_BASE_URL")
fi
case "${LEAN_APPROVAL_MODE:-yolo}" in
  off|interactive|"") ;;
  *) EXTRA+=(--yes-always) ;;
esac

# --no-auto-commits / --no-dirty-commits: lean-loop owns the git workflow;
#   aider's commits would interfere with the diff-based context the fix
#   loop assembles.
# --no-stream / --no-pretty: clean stdout for the parent to capture.
exec aider \
  --model              "${LEAN_MODEL:?LEAN_MODEL not set (see config.toml)}" \
  --no-auto-commits \
  --no-dirty-commits \
  --no-stream \
  --no-pretty \
  --no-check-update \
  "${EXTRA[@]}" \
  --message            "$PROMPT" \
  "$@"
