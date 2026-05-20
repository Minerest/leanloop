#!/usr/bin/env bash
# leaners/qwen-ctx.sh — Qwen Code CLI with project-conventions injection.
#
# Same as leaners/qwen.sh but prepends a QWEN.md (or equivalent) to the
# system prompt so the model knows the project's conventions. Looks for
# QWEN.md in this order:
#   1. $LEAN_CTX_FILE  (override)
#   2. $PWD/QWEN.md    (per-project)
#   3. $HOME/qwen/QWEN.md  (legacy default location)
# Missing file is fatal — use leaners/qwen.sh if you want context-free.
set -euo pipefail

CTX_FILE="${LEAN_CTX_FILE:-}"
if [[ -z "$CTX_FILE" ]]; then
  if [[ -f "$PWD/QWEN.md" ]]; then
    CTX_FILE="$PWD/QWEN.md"
  elif [[ -f "$HOME/qwen/QWEN.md" ]]; then
    CTX_FILE="$HOME/qwen/QWEN.md"
  fi
fi

if [[ ! -f "$CTX_FILE" ]]; then
  echo "leaners/qwen-ctx.sh: no QWEN.md found (tried \$LEAN_CTX_FILE, \$PWD/QWEN.md, \$HOME/qwen/QWEN.md)" >&2
  exit 2
fi

CTX_PROMPT="$(cat "$CTX_FILE")"

exec qwen \
  --auth-type       "${LEAN_AUTH_TYPE:-openai}" \
  --openai-base-url "${LEAN_BASE_URL:?LEAN_BASE_URL not set (see config.toml)}" \
  --openai-api-key  "${LEAN_API_KEY:?LEAN_API_KEY not set (see config.toml)}" \
  --model           "${LEAN_MODEL:?LEAN_MODEL not set (see config.toml)}" \
  --approval-mode   "${LEAN_APPROVAL_MODE:-yolo}" \
  --append-system-prompt "$CTX_PROMPT" \
  "$@"
