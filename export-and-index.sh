#!/usr/bin/env bash
# SessionEnd hook: export the completed session and update QMD index.
# Reads session_id from stdin JSON, finds the matching JSONL, exports it.
# All heavy work is backgrounded so the hook returns instantly.

set -euo pipefail

export PATH="$HOME/.bun/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

# Resolve the export script — works from the skill directory or from ~/.claude/hooks/
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$SCRIPT_DIR/export_sessions.py" ]; then
    EXPORT_SCRIPT="$SCRIPT_DIR/export_sessions.py"
else
    EXPORT_SCRIPT="$HOME/.claude/skills/recall/export_sessions.py"
fi
PYTHON="${PYTHON:-python3}"

# Read payload synchronously (stdin closes when parent exits)
payload=$(cat)

# Background everything — nothing depends on this hook's output.
# Redirect all FDs so Claude Code doesn't wait for the subshell.
{
    session_id=$(echo "$payload" | jq -r '.session_id // empty' 2>/dev/null || true)
    [ -z "$session_id" ] && exit 0

    # Direct glob instead of find (3ms vs 81ms)
    shopt -s nullglob
    matches=("$HOME"/.claude/projects/*/"${session_id}.jsonl" "$HOME"/.claude/projects/.remote-*/*/"${session_id}.jsonl")
    [ ${#matches[@]} -eq 0 ] && exit 0
    jsonl_path="${matches[0]}"

    "$PYTHON" "$EXPORT_SCRIPT" --single "$jsonl_path" 2>/dev/null || true

    if command -v qmd &>/dev/null; then
        qmd update 2>/dev/null || true
        qmd embed 2>/dev/null || true
    fi
} </dev/null >/dev/null 2>&1 &
disown
