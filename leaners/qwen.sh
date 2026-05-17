#!/usr/bin/env bash
# leaners/qwen.sh — minimal one-shot wrapper around the Qwen Code CLI.
# All settings come from LEAN_* env vars (populated by leanloop.py from config.toml).
set -euo pipefail

exec qwen \
  --auth-type       "${LEAN_AUTH_TYPE:-openai}" \
  --openai-base-url "${LEAN_BASE_URL:?LEAN_BASE_URL not set (see config.toml)}" \
  --openai-api-key  "${LEAN_API_KEY:?LEAN_API_KEY not set (see config.toml)}" \
  --model           "${LEAN_MODEL:?LEAN_MODEL not set (see config.toml)}" \
  --approval-mode   "${LEAN_APPROVAL_MODE:-yolo}" \
  "$@"
