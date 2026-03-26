---
name: recall
user-invocable: true
description: |
  Search past Claude Code sessions and Obsidian notes to recover decisions, debugging insights, and context.
  Use when asked to recall, find, or search past sessions/notes, or when user says "/recall".
  Triggers on "recall", "past session", "did I", "how did I", "find the session where".
allowed-tools: Bash(qmd *), Bash(grep *), Bash(ls *), Bash(find *), Read, Grep, Glob
effort: medium
---

# /recall — Search Past Sessions & Notes

Search indexed Claude Code and Codex CLI sessions (local and remote machines) and Obsidian research notes.

## Context

- Available collections: !'qmd collection list 2>/dev/null || echo "QMD not available"'

## Collections

Sessions are organized by agent and host under `~/.recall/`.

| Directory | Content |
|-----------|---------|
| `claude-code-local/` | Claude Code sessions — this machine |
| `claude-code-<host>/` | Claude Code sessions — remote machine |
| `codex-local/` | Codex CLI sessions — this machine |
| `codex-<host>/` | Codex CLI sessions — remote machine |
| `obsidian-<vault>/` | Obsidian research notes (optional) |

Each machine gets one Claude Code directory and one Codex directory. QMD collection names are user-defined when running `qmd collection add` — always check `qmd collection list` for the actual names to pass to `-c`. Omit `-c` to search all collections at once.

## Search Modes

### 1. Topic Search (default)

QMD provides three search commands. Pick the right one for the query:

| Command | Type | Best for | Example |
|---------|------|----------|---------|
| `qmd query` | Hybrid (BM25 + semantic) | General queries, best overall ranking — **use for ~80% of searches** | "gradient accumulation", "QMD video project status" |
| `qmd search` | BM25 (keyword) | Exact strings, known identifiers, error messages | "tmux prefix", "CUDA error", "FileNotFoundError" |
| `qmd vsearch` | Semantic (embeddings) | Conceptual/fuzzy queries where exact words won't appear in the text | "times I was stuck on a bug", "ideas I never followed up on" |

```bash
# BM25 — fast, deterministic, best for known keywords
qmd search "<query>" -n 5
qmd search "<query>" -c local -n 5

# Semantic — finds meaning even without exact word matches
qmd vsearch "<query>" -n 5
qmd vsearch "<query>" -c vault -n 5

# Hybrid — combines both, best ranking
qmd query "<query>" -n 5
qmd query "<query>" -c <collection> -n 5
```

**Multi-query expansion**: For vague or conceptual queries, expand into 2-3 related searches with different phrasings and merge results. Increase `-n` beyond 5 (up to 15) for broad recall. Example — user asks "when was I happy":

```bash
qmd vsearch "happy, grateful, excited" -c vault -n 10
qmd vsearch "energy, great day, feeling good" -c vault -n 10
qmd vsearch "satisfaction, accomplishment, shipped" -c vault -n 10
```

For BM25 synonym expansion (exact keywords with variation):
```bash
qmd search "CUDA error" -n 10
qmd search "GPU out of memory" -n 10
qmd search "RuntimeError device" -n 10
```

For hybrid broad recall:
```bash
qmd query "debugging training failures" -n 15
```

Merge and deduplicate before synthesizing.

### 2. Temporal Search — Structured Timeline

For date-based queries (`/recall yesterday`, `/recall what did I do on Feb 20`):

1. Resolve the date(s) — "yesterday" → YYYY-MM-DD, "last week" → date range
2. Find all sessions for that date across all machines:
   ```bash
   find ~/.recall/claude-code-* -name "YYYY-MM-DD-*.md" | sort
   ```
3. For each session, extract from frontmatter: slug, project, host, message-count, start-time, end-time, files-modified
4. Present as a structured timeline:
   ```
   ## 2026-02-20 — 36 sessions

   | Time | Machine | Project | Msgs | Files | Slug | Summary |
   |------|---------|---------|------|-------|------|---------|
   | 09:15–09:45 | local | dotfiles | 8 | 3 | tmux-setup | Set up tmux config |
   | 10:00–11:30 | remote | my-project | 24 | 7 | grad-accum-fix | Fixed gradient accumulation bug |
   | ... | | | | | | |
   ```
5. Use the **slug** from frontmatter as a quick label; read the first user message to generate a one-line summary
6. Show **files-modified count** from frontmatter (list full paths only if the user asks to drill down)
7. Include the **file path** of each session markdown so the user can drill into any row

### 3. Project Search

Search within a specific project:

```bash
qmd search "<query>" -c local --path "claude-code-local/<project>/" -n 5
```

To list projects per machine:

```bash
ls ~/.recall/claude-code-local/
# ls ~/.recall/claude-code-<host>/  # additional machines if configured
```

### 4. File Search

Find sessions that touched a specific file:

```bash
grep -rl "filename.py" ~/.recall/claude-code-*/ | head -10
```

## Workflow

1. Parse the user's query to determine the search mode:
   - Date mentions ("yesterday", "last week", "Feb 20") → **temporal**
   - Project name → **project search**
   - File name → **file search**
   - "notes about X" or "what do I know about X" → **vault collection**
   - Otherwise → **topic search** (all collections)
2. Choose the right QMD command (see topic search table above)
3. For vague/conceptual queries, run **2-3 parallel searches** with different phrasings
4. Read the top results (use the Read tool on returned file paths)
5. **Synthesize — don't just list results.** Extract patterns, surface forgotten decisions, and highlight actionable next steps:
   - **Date** and **project** for each relevant session
   - **Machine** (which host the session ran on)
   - **Key decisions** and their rationale — quote the exact words from the session
   - **Files modified** if relevant
   - **Patterns** across sessions (e.g., "you tried approach X three times and abandoned it each time")
   - **Unfinished threads** — ideas mentioned but never acted on
   - End with: *"What would you like to dig into?"*
6. If initial results aren't relevant, try alternate queries or combine modes

## Syncing Remote Machines

```bash
bash sync_remotes.sh  # run from the skill directory
```

## Notes

- Session frontmatter: session-id, slug, project, host, cwd, git-branch, date, start-time, end-time, message-count, files-modified
- If QMD returns no results, fall back to grep:
  ```bash
  grep -rli "<query>" ~/.recall/claude-code-*/ ~/.recall/codex-*/ ~/.recall/obsidian-*/ | head -10
  ```
