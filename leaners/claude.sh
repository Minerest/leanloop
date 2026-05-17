#!/usr/bin/env bash
# leaners/claude.sh — wrapper around Anthropic's Claude Code CLI.
# All settings come from LEAN_* env vars (populated by leanloop.py from config.toml).
#
# Requires: `claude` on PATH. https://docs.claude.com/en/docs/claude-code
#
# LEAN_MODEL examples: claude-opus-4-7, claude-sonnet-4-6, claude-haiku-4-5-20251001
# LEAN_APPROVAL_MODE: default | acceptEdits | plan | bypassPermissions
#                    (defaults to bypassPermissions so file edits don't prompt)
# LEAN_BASE_URL: optional — set for Bedrock / Vertex / proxy setups.
set -euo pipefail

export ANTHROPIC_API_KEY="${LEAN_API_KEY:?LEAN_API_KEY not set (see config.toml)}"

if [ -n "${LEAN_BASE_URL:-}" ]; then
  export ANTHROPIC_BASE_URL="$LEAN_BASE_URL"
fi

exec claude \
  --model            "${LEAN_MODEL:?LEAN_MODEL not set (see config.toml)}" \
  --permission-mode  "${LEAN_APPROVAL_MODE:-bypassPermissions}" \
  "$@"
