#!/usr/bin/env bash
# leaners/gemini.sh — wrapper around Google's Gemini CLI.
# All settings come from LEAN_* env vars (populated by leanloop.py from config.toml).
#
# Requires: `gemini` on PATH. https://github.com/google-gemini/gemini-cli
#
# Talks to the Gemini API directly (not OpenAI-compatible — for a local /
# OpenAI-style endpoint, use leaners/qwen.sh which is the Gemini CLI fork
# with --openai-base-url support).
#
# LEAN_MODEL examples: gemini-2.5-pro, gemini-2.5-flash
# LEAN_APPROVAL_MODE: anything truthy enables --yolo (auto-approve tools);
#                    set to "off" or unset to keep gemini's interactive prompts.
set -euo pipefail

export GEMINI_API_KEY="${LEAN_API_KEY:?LEAN_API_KEY not set (see config.toml)}"

EXTRA=()
case "${LEAN_APPROVAL_MODE:-yolo}" in
  off|interactive|"") ;;
  *) EXTRA+=(--yolo) ;;
esac

exec gemini \
  --model "${LEAN_MODEL:?LEAN_MODEL not set (see config.toml)}" \
  "${EXTRA[@]}" \
  "$@"
