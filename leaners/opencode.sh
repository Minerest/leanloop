#!/usr/bin/env bash
# leaners/opencode.sh — wrapper around OpenCode (https://opencode.ai).
# All settings come from LEAN_* env vars (populated by leanloop.py from config.toml).
#
# Requires: `opencode` on PATH.
#
# LEAN_MODEL: provider/model form, e.g. "anthropic/claude-sonnet-4-6",
#             "openai/gpt-4o", or anything that shows up in `opencode models`.
# LEAN_API_KEY: exported as OPENAI_API_KEY and ANTHROPIC_API_KEY — opencode
#               picks them up via its env-var fallback. Long-lived setup is
#               better done via `opencode auth login` (which writes
#               ~/.local/share/opencode/auth.json).
# LEAN_BASE_URL: NOT plumbed. OpenCode's custom / local provider setup
#                lives in its own config (`~/.config/opencode/config.json`
#                or `opencode.json` in the project). Register the provider
#                there once, then point LEAN_MODEL at "provider/model".
# LEAN_APPROVAL_MODE: anything except "off" / "interactive" / "" passes
#                    --dangerously-skip-permissions.
set -euo pipefail

# Translate leanloop's `-p <prompt>` to opencode's positional message form.
if [ "${1:-}" = "-p" ] && [ -n "${2:-}" ]; then
  PROMPT="$2"
  shift 2
else
  echo "leaners/opencode.sh: expected '-p <prompt>' as first two args" >&2
  exit 2
fi

export OPENAI_API_KEY="${LEAN_API_KEY:?LEAN_API_KEY not set (see config.toml)}"
export ANTHROPIC_API_KEY="$LEAN_API_KEY"

EXTRA=()
case "${LEAN_APPROVAL_MODE:-yolo}" in
  off|interactive|"") ;;
  *) EXTRA+=(--dangerously-skip-permissions) ;;
esac

exec opencode run \
  --model "${LEAN_MODEL:?LEAN_MODEL not set (see config.toml)}" \
  "${EXTRA[@]}" \
  "$PROMPT" \
  "$@"
