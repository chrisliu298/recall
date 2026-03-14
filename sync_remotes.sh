#!/usr/bin/env bash
# Sync Claude Code + Codex CLI sessions from remote machines, then export and re-index.
# Usage: sync_remotes.sh [--export-only]
#   --export-only  Skip rsync, just re-export and re-index local files

set -euo pipefail

export PATH="$HOME/.bun/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

LOCAL_PROJECTS="$HOME/.claude/projects"
LOCAL_CODEX_SESSIONS="$HOME/.codex/sessions"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
EXPORT_SCRIPT="$SCRIPT_DIR/export_sessions.py"
PYTHON="${PYTHON:-python3}"

# Remote hosts loaded from ~/.recall/config.json "remotes" array.
# Each entry is "ssh-alias:/remote/home/dir". Example:
#   "myserver:/home/myuser"
CONFIG_FILE="$HOME/.recall/config.json"
REMOTES=()
if [ -f "$CONFIG_FILE" ]; then
    if command -v jq &>/dev/null; then
        while IFS= read -r line; do
            [ -n "$line" ] && REMOTES+=("$line")
        done < <(jq -r '.remotes[]? // empty' "$CONFIG_FILE" 2>/dev/null)
    else
        echo "WARNING: jq not found — cannot read remotes from config.json" >&2
    fi
fi

sync_remote() {
    local entry="$1"
    local host="${entry%%:*}"
    local remote_home="${entry#*:}"
    local failed=0

    # Claude Code sessions
    local remote_projects="${remote_home}/.claude/projects"
    local staging="$LOCAL_PROJECTS/.remote-${host}"

    echo "[$host] Syncing..."
    mkdir -p "$staging"
    if ! rsync -azh \
        --include='*/' \
        --include='*.jsonl' \
        --exclude='*' \
        "${host}:${remote_projects}/" "$staging/" >/dev/null 2>&1; then
        echo "WARNING: [$host] Failed to sync Claude Code sessions" >&2
        failed=1
    fi

    # Codex CLI sessions
    local remote_codex="${remote_home}/.codex/sessions"
    local codex_staging="$LOCAL_CODEX_SESSIONS/.remote-${host}"

    mkdir -p "$codex_staging"
    if ! rsync -azh \
        --include='*/' \
        --include='*.jsonl' \
        --exclude='*' \
        "${host}:${remote_codex}/" "$codex_staging/" >/dev/null 2>&1; then
        echo "WARNING: [$host] Failed to sync Codex sessions" >&2
        failed=1
    fi

    if [[ "$failed" -eq 1 ]]; then
        echo "[$host] Done (with warnings)."
    else
        echo "[$host] Done."
    fi
}

# ── Sync ──────────────────────────────────────────────────────────────
if [[ "${1:-}" != "--export-only" ]]; then
    for remote in "${REMOTES[@]}"; do
        sync_remote "$remote" &
    done
    wait
    echo ""
fi

# ── Export + Index ────────────────────────────────────────────────────
echo "Exporting sessions..."
"$PYTHON" "$EXPORT_SCRIPT"

echo ""
echo "Updating QMD index..."
if command -v qmd &>/dev/null; then
    qmd update 2>&1 | tail -2
    qmd embed 2>&1 | tail -2
else
    echo "qmd not found — skipping indexing"
fi

echo ""
echo "Sync complete."
