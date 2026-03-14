# Recall

**A skill for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) and [Codex](https://github.com/openai/codex) that turns your past sessions into a searchable memory.**

> *"How did I fix that CUDA error last week?"*
> *"What did I work on yesterday?"*
> *"What do my notes say about reward shaping?"*

Your agent accumulates sessions, but they're write-once — invisible the moment you close the terminal. Recall exports them to markdown files, indexes them with [QMD](https://github.com/tobi/qmd), and teaches your agent to search them intelligently.

Invoke with `/recall` or ask your agent to "recall", "find past sessions", "did I ever...", or "how did I handle X".

## Table of Contents

- [How It Works](#how-it-works)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Setup](#setup)
- [Usage](#usage)
- [Multi-Machine Sync](#multi-machine-sync-optional)
- [Configuration](#configuration)
- [Contributors](#contributors)

---

## How It Works

```
Sessions (JSONL)          Markdown              Search
┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│ Claude Code  │───▶│              │    │              │
│ ~/.claude/   │    │ export_      │    │   QMD        │
│              │    │ sessions.py  │───▶│   index      │───▶ /recall
│ Codex CLI    │───▶│              │    │              │
│ ~/.codex/    │    │              │    │ BM25 +       │
└──────────────┘    └──────────────┘    │ semantic     │
                           │            └──────────────┘
                    ~/.recall/
                    ├── claude-code-local/
                    ├── codex-local/
                    └── obsidian-vault/ (optional)
```

1. **Export** — `export_sessions.py` converts JSONL session files into searchable markdown with YAML frontmatter (project, date, files modified, tools used, summary)
2. **Index** — QMD builds BM25 and semantic (embedding) indexes over the markdown
3. **Search** — the skill teaches your agent three search modes (keyword, semantic, hybrid) and when to use each, plus multi-query expansion for vague queries

The export script has **zero pip dependencies** — stdlib Python only.

---

## Prerequisites

- **Python 3.9+**
- **[QMD](https://github.com/tobi/qmd)** — semantic search over markdown files
- **jq** — only needed for multi-machine sync (optional)

---

## Installation

### 1. Install QMD

```bash
bun install -g @tobilu/qmd
# or: npm install -g @tobilu/qmd
```

Verify: `qmd --help`

QMD provides three search modes — BM25 (keyword), semantic (embedding), and hybrid (both combined). Keyword search works out of the box. Semantic and hybrid search require embeddings, which QMD generates locally using a small GGUF model (~600MB, downloaded on first `qmd embed`). No API keys needed.

To use a specific embedding model, set `QMD_EMBED_MODEL`:

```bash
# Default (auto-downloaded on first embed):
export QMD_EMBED_MODEL="hf:Qwen/Qwen3-Embedding-0.6B-GGUF/Qwen3-Embedding-0.6B-Q8_0.gguf"
```

### 2. Install the skill

**Claude Code:**

```bash
git clone https://github.com/chrisliu298/recall.git ~/.claude/skills/recall
```

**Codex:**

```bash
git clone https://github.com/chrisliu298/recall.git ~/.codex/skills/recall
```

---

## Setup

### 1. Export your sessions

```bash
python3 ~/.claude/skills/recall/export_sessions.py
```

This exports both Claude Code and Codex sessions to `~/.recall/` as searchable markdown. First run processes everything; subsequent runs are incremental (only new/changed sessions).

### 2. Create QMD collections and index

```bash
# Add your exported session directories as QMD collections
qmd collection add ~/.recall/claude-code-local --name local
qmd collection add ~/.recall/codex-local --name codex-local

# Optional: Obsidian vault
# qmd collection add ~/.recall/obsidian-vault --name vault

# Build the BM25 index (fast, enables keyword search)
qmd update

# Generate embeddings (slower first time, enables semantic + hybrid search)
qmd embed
```

Collection names are yours to choose — the skill discovers them at runtime via `qmd collection list`. You can verify your setup:

```bash
qmd status              # show index health, file counts, last update
qmd collection list     # show all collections and their names
```

### 3. Test it

```bash
# Keyword search (works immediately after qmd update)
qmd search "tmux config" -n 3

# Hybrid search (works after qmd embed)
qmd query "how did I fix that bug" -n 3
```

### 3. Auto-export on session close (optional)

Copy the hook script and wire it into Claude Code's settings:

```bash
cp ~/.claude/skills/recall/export-and-index.sh ~/.claude/hooks/
```

Add to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionEnd": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "~/.claude/hooks/export-and-index.sh"
          }
        ]
      }
    ]
  }
}
```

Every session is now automatically exported and indexed when you close it.

---

## Usage

```
/recall how did I set up tmux?
/recall what did I work on yesterday
/recall the training bug on the GPU server
/recall what do my notes say about reward shaping
/recall sessions that touched train.py
```

The skill routes queries automatically:

| Query type | Example | Search mode |
|-----------|---------|-------------|
| General topic | "pre-commit hook setup" | Hybrid (BM25 + semantic) |
| Date/time | "yesterday", "Feb 20" | Temporal (find by date, show timeline) |
| Project name | "nanochat auth issue" | Project-scoped search |
| File name | "sessions that touched train.py" | Grep for file path |
| Notes/knowledge | "notes about GRPO" | Vault collection |

For vague queries, the skill automatically expands into 2-3 related searches with different phrasings and merges results.

### Direct QMD usage

```bash
qmd query "gradient accumulation bug" -n 5      # hybrid (recommended)
qmd search "CUDA error" -n 5                     # keyword (exact match)
qmd vsearch "times I was stuck" -c vault -n 5    # semantic (fuzzy)
```

---

## Multi-Machine Sync (optional)

If you work across multiple machines, `sync_remotes.sh` pulls sessions via rsync, exports, and re-indexes:

```bash
bash ~/.claude/skills/recall/sync_remotes.sh
```

Configure remotes in `~/.recall/config.json`:

```json
{
  "remotes": [
    "myserver:/home/myuser",
    "workstation:/Users/myuser"
  ]
}
```

Each remote machine gets its own Claude Code and Codex collections under `~/.recall/`.

---

## Configuration

All configuration lives in `~/.recall/config.json` (optional — single-machine usage works with no config).

```json
{
  "hosts": {
    "": "claude-code-local",
    "myserver": "claude-code-myserver"
  },
  "codex_hosts": {
    "": "codex-local",
    "myserver": "codex-myserver"
  },
  "hostname_map": {
    "my-server-hostname": "myserver"
  },
  "home_prefixes": [
    "/home/myuser/"
  ],
  "remotes": [
    "myserver:/home/myuser"
  ]
}
```

| Key | Purpose | Default |
|-----|---------|---------|
| `hosts` | Claude Code host-to-directory mapping | `{"": "claude-code-local"}` |
| `codex_hosts` | Codex host-to-directory mapping | `{"": "codex-local"}` |
| `hostname_map` | Machine hostname to host key | `{}` |
| `home_prefixes` | Extra home directory prefixes for remote machines | `[]` (uses `$HOME` automatically) |
| `remotes` | SSH remotes for `sync_remotes.sh` | `[]` |

---

## Session Markdown Format

Each exported session has YAML frontmatter for structured queries:

```yaml
session-id: abc12345-...
slug: fix-gradient-bug
project: my-project
host: myserver          # only for remote sessions
date: 2026-02-20
start-time: 2026-02-20T09:15:00+00:00
end-time: 2026-02-20T09:45:00+00:00
message-count: 12
files-modified:
  - "src/train.py"
  - "config/model.yaml"
```

Followed by `## User` / `## Assistant` conversation turns with tool summaries.

---

## Files

| File | Purpose |
|------|---------|
| `SKILL.md` | Skill definition — teaches the agent how to search |
| `export_sessions.py` | JSONL to markdown exporter (Claude Code + Codex) |
| `sync_remotes.sh` | Rsync sessions from remote machines, then re-export and re-index |
| `export-and-index.sh` | SessionEnd hook — auto-export on session close |

---

## Contributors

- [@chrisliu298](https://github.com/chrisliu298)
- **Claude Code** — config architecture, sanitization, and audit
- **Codex** — adversarial review and regression testing
