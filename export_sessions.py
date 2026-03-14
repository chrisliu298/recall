#!/usr/bin/env python3
"""Export Claude Code and Codex CLI JSONL sessions to searchable Obsidian markdown."""

from __future__ import annotations

import json
import os
import re
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECTS_DIR = Path.home() / ".claude" / "projects"
CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"
RECALL_DIR = Path.home() / ".recall"
MANIFEST_PATH = RECALL_DIR / ".export-manifest.json"
CONFIG_PATH = RECALL_DIR / "config.json"


def _load_config() -> dict:
    """Load optional config from ~/.recall/config.json."""
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


_config = _load_config()

# Host → top-level output directory (Claude Code)
# Configure via ~/.recall/config.json "hosts" key for multi-machine setups
HOST_DIRS = _config.get("hosts", {"": "claude-code-local"})

# Hostname → host key for Codex (runs locally per machine)
# Configure via ~/.recall/config.json "hostname_map" key
_HOSTNAME_TO_HOST = _config.get("hostname_map", {})

# Host key → output directory (Codex)
# Configure via ~/.recall/config.json "codex_hosts" key
CODEX_HOST_DIRS = _config.get("codex_hosts", {"": "codex-local"})

# Known home dir prefixes to strip when deriving project names from cwd
# Configure via ~/.recall/config.json "home_prefixes" key for remote machines
_HOME_PREFIXES = [str(Path.home()) + "/"] + _config.get("home_prefixes", [])

# XML tags to strip from user messages (Claude Code protocol artifacts)
_XML_TAGS_RE = re.compile(
    r"<(?:command-message|command-name|command-args|local-command-stdout|"
    r"task-notification|system-reminder)>.*?</(?:command-message|command-name|"
    r"command-args|local-command-stdout|task-notification|system-reminder)>",
    re.DOTALL,
)

# ANSI escape codes
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# File extension → language mapping
_EXT_LANGUAGES = {
    ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".sh": "shell", ".rs": "rust", ".go": "go", ".lua": "lua",
    ".md": "markdown", ".yaml": "yaml", ".yml": "yaml",
    ".json": "json", ".toml": "toml", ".zsh": "zsh",
    ".css": "css", ".html": "html", ".jsx": "jsx", ".tsx": "tsx",
    ".rb": "ruby", ".java": "java", ".c": "c", ".cpp": "cpp",
    ".tex": "latex", ".sql": "sql", ".r": "r",
}

# Continuation marker
_CONTINUATION_PREFIX = "This session is being continued from a previous conversation"

# Codex system-injected user message prefixes to skip
_CODEX_SYSTEM_PREFIXES = (
    "# AGENTS.md instructions for ",
    "<environment_context>",
    "<user_shell_command>",
)


def load_manifest() -> dict:
    if MANIFEST_PATH.exists():
        return json.loads(MANIFEST_PATH.read_text())
    return {"exported": {}}


def save_manifest(manifest: dict):
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2) + "\n")


def _build_home_prefix_re() -> re.Pattern:
    """Build regex to strip home directory prefixes from project path names."""
    stems = set()
    for prefix in _HOME_PREFIXES:
        parts = prefix.strip("/").split("/")
        if len(parts) >= 2:
            stems.add("-".join(parts[:2]))
    if not stems:
        return re.compile(r"^$")
    escaped = [re.escape(s) for s in stems]
    return re.compile(r"^-(" + "|".join(escaped) + r")-?")


_HOME_PREFIX_RE = _build_home_prefix_re()


def project_name_from_path(project_dir: str) -> str:
    """Convert '-Users-foo-Developer-GitHub-bar' to 'bar'.

    Handles multiple home dir prefixes for local and remote machines.
    """
    name = os.path.basename(project_dir)
    name = _HOME_PREFIX_RE.sub("", name)
    if not name or name == "-":
        return "home"
    # Keep last meaningful segments for deeply nested paths
    parts = [p for p in name.split("-") if p]
    if len(parts) > 3:
        parts = parts[-3:]
    return "-".join(parts).lower()


def extract_text(content) -> str:
    """Extract plain text from message content (str or list of blocks)."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        texts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "").strip()
                if text:
                    texts.append(text)
        return "\n\n".join(texts)
    return ""


def strip_tags(text: str) -> str:
    """Remove system-reminder and other XML protocol tags."""
    text = _XML_TAGS_RE.sub("", text)
    return text.strip()


def strip_ansi(text: str) -> str:
    """Remove ANSI escape codes."""
    return _ANSI_RE.sub("", text)


def shorten_path(path: str) -> str:
    """Truncate absolute paths to parent/basename for tool summaries."""
    if not path or not path.startswith("/"):
        return path
    parts = Path(path).parts
    if len(parts) <= 2:
        return path
    return str(Path(parts[-2]) / parts[-1])


# Regexes for slash command extraction
_COMMAND_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.DOTALL)
_COMMAND_NAME_RE = re.compile(r"<command-name>(.*?)</command-name>", re.DOTALL)

# Noise commands whose first user message should be skipped for summary extraction
_NOISE_COMMANDS = {"atomic-push", "push", "commit", "login", "plugin", "init", "clear", "help"}


def extract_slash_command(text: str) -> tuple[str, str] | None:
    """Extract (command_name, args) from slash-command XML tags."""
    name_match = _COMMAND_NAME_RE.search(text)
    if not name_match:
        return None
    cmd_name = name_match.group(1).strip()
    args_match = _COMMAND_ARGS_RE.search(text)
    args = args_match.group(1).strip() if args_match else ""
    return (cmd_name, args)


def extract_tool_summaries(content: list) -> list[str]:
    """Extract compact one-liner summaries from tool_use blocks."""
    summaries = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        name = block.get("name", "")
        inp = block.get("input", {})
        if name == "Bash":
            cmd = inp.get("command", "")
            desc = inp.get("description", "")
            if desc:
                summaries.append(f"> Bash: {strip_ansi(desc)}")
            elif cmd:
                # First line of command, truncated
                first_line = cmd.split("\n")[0][:120]
                summaries.append(f"> Bash: `{first_line}`")
        elif name == "Read":
            path = inp.get("file_path", "")
            if path:
                summaries.append(f"> Read: `{shorten_path(path)}`")
        elif name == "Edit":
            path = inp.get("file_path", "")
            if path:
                summaries.append(f"> Edit: `{shorten_path(path)}`")
        elif name == "Write":
            path = inp.get("file_path", "")
            if path:
                summaries.append(f"> Write: `{shorten_path(path)}`")
        elif name == "NotebookEdit":
            path = inp.get("notebook_path", "")
            if path:
                summaries.append(f"> NotebookEdit: `{shorten_path(path)}`")
        elif name == "Grep":
            pattern = inp.get("pattern", "")
            path = inp.get("path", ".")
            summaries.append(f"> Grep: `{pattern}` in `{shorten_path(path)}`")
        elif name == "Glob":
            pattern = inp.get("pattern", "")
            summaries.append(f"> Glob: `{pattern}`")
        elif name == "WebSearch":
            query = inp.get("query", "")
            summaries.append(f"> WebSearch: `{query}`")
        elif name == "WebFetch":
            url = inp.get("url", "")
            summaries.append(f"> WebFetch: `{url}`")
        elif name in ("Agent", "SendMessage", "TaskCreate", "TaskUpdate",
                       "TeamCreate", "TeamDelete", "ExitPlanMode",
                       "AskUserQuestion", "EnterPlanMode"):
            # Skip meta/orchestration tools — low search value
            pass
        else:
            summaries.append(f"> {name}")
    return summaries


# Patterns that indicate tool output failure
_FAILURE_PATTERNS = re.compile(
    r"traceback|assertion.error|failed|error:|panic|segfault|oom|FAILED|"
    r"Exception|TypeError|ValueError|KeyError|AttributeError|ImportError|"
    r"ModuleNotFoundError|RuntimeError|FileNotFoundError",
    re.IGNORECASE,
)


def _looks_like_failure(text: str) -> bool:
    """Check if output text looks like a failure."""
    return bool(_FAILURE_PATTERNS.search(text[:500]))


def extract_tool_result_summaries(content) -> list[str]:
    """Extract error/failure summaries from tool_result content blocks in user messages."""
    summaries = []
    if not isinstance(content, list):
        return summaries
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        is_error = block.get("is_error", False)
        result_content = block.get("content", "")
        if isinstance(result_content, list):
            result_content = "\n".join(
                b.get("text", "") for b in result_content
                if isinstance(b, dict) and b.get("type") == "text"
            )
        result_content = strip_ansi(result_content)
        if is_error:
            truncated = result_content[:200].strip()
            if truncated:
                summaries.append(f"> Error: {truncated}")
        elif _looks_like_failure(result_content):
            truncated = result_content[:200].strip()
            if truncated:
                summaries.append(f"> stderr: {truncated}")
    return summaries


def extract_files_modified(messages: list) -> list[str]:
    """Extract unique file paths from tool_use blocks (Edit, Write, NotebookEdit)."""
    WRITE_TOOLS = {"Edit", "Write", "NotebookEdit"}
    files = set()
    for msg in messages:
        if msg.get("type") != "assistant":
            continue
        content = msg.get("message", {}).get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            if block.get("name") not in WRITE_TOOLS:
                continue
            inp = block.get("input", {})
            path = inp.get("file_path") or inp.get("notebook_path") or ""
            if path:
                files.add(path)
    return sorted(files)


def extract_files_read(messages: list) -> list[str]:
    """Extract unique file paths from Read and Grep tool_use blocks."""
    READ_TOOLS = {"Read", "Grep"}
    files = set()
    for msg in messages:
        if msg.get("type") != "assistant":
            continue
        content = msg.get("message", {}).get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            if block.get("name") not in READ_TOOLS:
                continue
            inp = block.get("input", {})
            path = inp.get("file_path") or inp.get("path") or ""
            if path and path != ".":
                files.add(path)
    return sorted(files)


def extract_tools_used(messages: list) -> list[str]:
    """Extract distinct tool names from assistant messages."""
    tools = set()
    for msg in messages:
        if msg.get("type") != "assistant":
            continue
        content = msg.get("message", {}).get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                name = block.get("name", "")
                if name:
                    tools.add(name)
    return sorted(tools)


def extract_languages(files: list[str]) -> list[str]:
    """Extract language tags from file extensions."""
    langs = set()
    for fp in files:
        ext = os.path.splitext(fp)[1].lower()
        if ext in _EXT_LANGUAGES:
            langs.add(_EXT_LANGUAGES[ext])
    return sorted(langs)


def is_subagent_session(messages: list) -> bool:
    """Detect subagent sessions: no user messages with userType='external'."""
    for msg in messages:
        if msg.get("type") == "user" and msg.get("userType") == "external":
            return False
    return True


def is_low_quality_session(meta: dict, turns: list) -> bool:
    """Detect low-quality sessions that add noise to search results."""
    summary = meta.get("summary", "")
    for pattern in ("API Error:", "OAuth token has expired", "Please run /login"):
        if pattern in summary:
            return True
    # Quality gate: few messages, no files, no tools, minimal content
    msg_count = meta.get("message_count", 0)
    if msg_count <= 2 and not meta.get("files_modified") and not meta.get("tools_used"):
        total_content = sum(len(content) for _, content in turns)
        if total_content < 500:
            return True
    return False


# ---------------------------------------------------------------------------
# Codex CLI helpers
# ---------------------------------------------------------------------------

def _get_codex_host() -> str:
    """Return the host key for Codex based on the current machine's hostname."""
    hostname = socket.gethostname().split(".")[0].lower()
    return _HOSTNAME_TO_HOST.get(hostname, "")


def project_name_from_cwd(cwd: str) -> str:
    """Derive a short project name from a Codex session's cwd path."""
    path = cwd.rstrip("/")
    for prefix in _HOME_PREFIXES:
        if path.startswith(prefix):
            path = path[len(prefix):]
            break
    if not path or path == "/":
        return "home"
    parts = [p for p in path.split("/") if p]
    if len(parts) > 3:
        parts = parts[-3:]
    return "-".join(parts).lower()


def slug_from_summary(summary: str, max_len: int = 50) -> str:
    """Derive a filesystem-safe slug from a session summary."""
    if not summary:
        return ""
    slug = re.sub(r"[^a-z0-9]+", "-", summary.lower())
    slug = re.sub(r"-+", "-", slug).strip("-")
    if len(slug) > max_len:
        slug = slug[:max_len].rsplit("-", 1)[0]
    return slug


def _extract_codex_patch_files(patch_input: str) -> list[str]:
    """Extract file paths from an apply_patch input string."""
    files = []
    for line in patch_input.split("\n"):
        line = line.strip()
        for prefix in ("*** Add File: ", "*** Update File: ", "*** Delete File: "):
            if line.startswith(prefix):
                files.append(line[len(prefix):].strip())
    return files


def _extract_codex_tool_summary(name: str, arguments: str) -> str | None:
    """Return a one-liner summary for a Codex function/tool call."""
    if name == "shell_command":
        try:
            args = json.loads(arguments)
        except (json.JSONDecodeError, TypeError):
            return "> shell: (unparseable)"
        cmd = args.get("command", "")
        first_line = cmd.split("\n")[0][:120]
        return f"> shell: `{first_line}`"
    if name == "apply_patch":
        # arguments is the raw patch string for custom_tool_call
        files = _extract_codex_patch_files(arguments)
        if files:
            return "> apply_patch: " + ", ".join(f"`{f}`" for f in files)
        return "> apply_patch"
    if name in ("update_plan", "spawn_agent"):
        return None  # skip orchestration tools
    return f"> {name}"


def parse_codex_session(jsonl_path: Path) -> dict | None:
    """Parse a Codex CLI JSONL session file into structured data."""
    records = []
    with open(jsonl_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    if not records:
        return None

    # First record must be session_meta
    first = records[0]
    if first.get("type") != "session_meta":
        return None

    payload = first.get("payload", {})

    # Skip subagent and exec sessions
    source = payload.get("source", "")
    if isinstance(source, dict) and "subagent" in source:
        return None
    if source == "exec":
        return None

    session_id = payload.get("id", jsonl_path.stem)
    cwd = payload.get("cwd", "")
    git_info = payload.get("git", {})
    git_branch = git_info.get("branch", "") if isinstance(git_info, dict) else ""

    meta = {
        "session_id": session_id,
        "slug": "",
        "cwd": cwd,
        "git_branch": git_branch,
        "source": "codex",
    }

    # Extract timestamps
    timestamps = []
    for rec in records:
        ts = rec.get("timestamp")
        if ts:
            try:
                timestamps.append(datetime.fromisoformat(ts.replace("Z", "+00:00")))
            except (ValueError, TypeError):
                pass

    if not timestamps:
        return None

    meta["start_time"] = min(timestamps)
    meta["end_time"] = max(timestamps)
    meta["date"] = meta["start_time"].strftime("%Y-%m-%d")
    duration = meta["end_time"] - meta["start_time"]
    meta["duration_minutes"] = max(1, int(duration.total_seconds() / 60))

    # Extract conversation turns and tool usage
    turns = []
    msg_count = 0
    tools_used = set()
    files_modified = set()
    files_read = set()

    for rec in records:
        if rec.get("type") != "response_item":
            continue
        p = rec.get("payload", {})
        role = p.get("role")
        item_type = p.get("type")

        # User messages (skip system-injected AGENTS.md / environment_context)
        if role == "user" and item_type == "message":
            texts = []
            for block in (p.get("content") or []):
                if isinstance(block, dict) and block.get("type") == "input_text":
                    t = block.get("text", "").strip()
                    if t:
                        texts.append(t)
            content = "\n\n".join(texts)
            content = strip_tags(content)
            content = strip_ansi(content)
            if not content:
                continue
            if any(content.startswith(pfx) for pfx in _CODEX_SYSTEM_PREFIXES):
                continue
            turns.append(("user", content))
            msg_count += 1

        # Assistant messages
        elif role == "assistant" and item_type == "message":
            texts = []
            for block in (p.get("content") or []):
                if isinstance(block, dict) and block.get("type") == "output_text":
                    t = block.get("text", "").strip()
                    if t:
                        texts.append(t)
            content = "\n\n".join(texts)
            if content:
                turns.append(("assistant", content))
                msg_count += 1

        # Function calls (shell_command, update_plan)
        elif item_type == "function_call":
            name = p.get("name", "")
            arguments = p.get("arguments", "")
            if name:
                tools_used.add(name)
            # Track files read via shell_command (cat, head, sed -n)
            if name == "shell_command":
                try:
                    args = json.loads(arguments)
                    cmd = args.get("command", "")
                    # Simple heuristic: commands starting with cat/head/sed/tail reading files
                    for read_cmd in ("cat ", "head ", "tail ", "sed -n"):
                        if cmd.startswith(read_cmd):
                            parts = cmd.split()
                            if len(parts) >= 2:
                                candidate = parts[-1]
                                if "/" in candidate or "." in candidate:
                                    files_read.add(candidate)
                except (json.JSONDecodeError, TypeError):
                    pass
            summary = _extract_codex_tool_summary(name, arguments)
            if summary:
                # Append tool summary to previous assistant turn or create new one
                if turns and turns[-1][0] == "assistant":
                    turns[-1] = ("assistant", turns[-1][1] + "\n\n" + summary)
                else:
                    turns.append(("assistant", summary))

        # Custom tool calls (apply_patch)
        elif item_type == "custom_tool_call":
            name = p.get("name", "")
            patch_input = p.get("input", "")
            if name:
                tools_used.add(name)
            if name == "apply_patch" and patch_input:
                for fp in _extract_codex_patch_files(patch_input):
                    files_modified.add(fp)
            summary = _extract_codex_tool_summary(name, patch_input)
            if summary:
                if turns and turns[-1][0] == "assistant":
                    turns[-1] = ("assistant", turns[-1][1] + "\n\n" + summary)
                else:
                    turns.append(("assistant", summary))

    meta["message_count"] = msg_count
    meta["files_modified"] = sorted(files_modified)
    meta["files_read"] = sorted(files_read)
    meta["tools_used"] = sorted(tools_used)
    all_files = meta["files_modified"] + meta["files_read"]
    meta["languages"] = extract_languages(all_files)

    if not turns:
        return None

    # Extract summary from first user message (skip noise commands)
    for role, content in turns:
        if role == "user":
            clean = re.sub(r"<[^>]+>", "", content).strip()
            clean = re.sub(r"\s+", " ", clean)
            # Skip noise commands
            first_word = clean.split()[0] if clean else ""
            if first_word.lstrip("/") in _NOISE_COMMANDS:
                continue
            if clean and not clean.startswith("[Request interrupted"):
                meta["summary"] = clean[:200]
                break

    # Derive slug from summary
    meta["slug"] = slug_from_summary(meta.get("summary", ""))

    # Project from cwd
    meta["project"] = project_name_from_cwd(cwd) if cwd else "unknown"

    # Host: check for .remote-<host> in path (synced from remote), else use hostname
    host = ""
    for parent in jsonl_path.parents:
        if parent.name.startswith(".remote-"):
            host = parent.name.removeprefix(".remote-")
            break
    if not host:
        host = _get_codex_host()
    if host:
        meta["host"] = host

    if is_low_quality_session(meta, turns):
        return None

    return {"meta": meta, "turns": turns}


def find_all_codex_sessions() -> list[Path]:
    """Find all Codex CLI JSONL session files."""
    if not CODEX_SESSIONS_DIR.exists():
        return []
    return sorted(CODEX_SESSIONS_DIR.glob("**/*.jsonl"))


def codex_output_path(meta: dict) -> Path:
    """Generate output path for a Codex session."""
    host = meta.get("host", "")
    host_dir = CODEX_HOST_DIRS.get(host, f"codex-{host}" if host else "codex-local")
    session_id_short = meta["session_id"][:8]
    slug = meta.get("slug", "session")
    slug = re.sub(r"[^\w\-]", "-", slug).strip("-")
    if not slug:
        slug = "session"
    filename = f"{meta['date']}-{session_id_short}-{slug}.md"
    return RECALL_DIR / host_dir / meta["project"] / filename


def export_codex_session(jsonl_path: Path, manifest: dict) -> str | None:
    """Export a single Codex session. Returns session_id if exported, None if skipped."""
    session = parse_codex_session(jsonl_path)
    if not session:
        return None

    sid = session["meta"]["session_id"]
    current_size = jsonl_path.stat().st_size
    prev = manifest.get("exported", {}).get(sid)
    if prev and prev.get("source_size") == current_size:
        return None

    md = render_markdown(session)
    out = codex_output_path(session["meta"])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md)

    # Clean up old file if path changed
    if prev and prev.get("path"):
        old_path = RECALL_DIR / prev["path"]
        if old_path != out and old_path.exists():
            old_path.unlink()

    manifest.setdefault("exported", {})[sid] = {
        "path": str(out.relative_to(RECALL_DIR)),
        "date": session["meta"]["date"],
        "project": session["meta"]["project"],
        "source_size": current_size,
    }

    return sid


def parse_session(jsonl_path: Path) -> dict | None:
    """Parse a JSONL session file into structured data."""
    messages = []
    with open(jsonl_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                messages.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    if not messages:
        return None

    # Skip file-history-only sessions (no user/assistant messages)
    msg_types = {m.get("type") for m in messages}
    if not (msg_types & {"user", "assistant"}):
        return None

    # Skip subagent sessions
    if is_subagent_session(messages):
        return None

    # Extract metadata from first user message
    meta = {}
    for msg in messages:
        if msg.get("type") == "user" and msg.get("userType") == "external":
            meta["session_id"] = msg.get("sessionId", jsonl_path.stem)
            meta["slug"] = msg.get("slug", "")
            meta["cwd"] = msg.get("cwd", "")
            meta["git_branch"] = msg.get("gitBranch", "")
            break

    if not meta:
        return None

    # Extract timestamps
    timestamps = []
    for msg in messages:
        ts = msg.get("timestamp")
        if ts:
            try:
                timestamps.append(datetime.fromisoformat(ts.replace("Z", "+00:00")))
            except (ValueError, TypeError):
                pass

    if not timestamps:
        return None

    meta["start_time"] = min(timestamps)
    meta["end_time"] = max(timestamps)
    meta["date"] = meta["start_time"].strftime("%Y-%m-%d")
    duration = meta["end_time"] - meta["start_time"]
    meta["duration_minutes"] = max(1, int(duration.total_seconds() / 60))

    # Extract conversation turns with tool summaries
    turns = []  # list of (role, content) where role is "user"|"assistant"|"continuation"
    msg_count = 0
    noise_turn_indices = set()
    for msg in messages:
        msg_type = msg.get("type")

        if msg_type == "user" and not msg.get("isMeta"):
            raw = msg.get("message", {}).get("content", "")
            content = extract_text(raw)
            # Extract slash command before stripping tags
            slash_cmd = extract_slash_command(content)
            content = strip_tags(content)
            content = strip_ansi(content)
            # Preserve slash command args when content got stripped
            if not content and slash_cmd and slash_cmd[1]:
                content = slash_cmd[1]
            # Track noise commands for summary skipping
            if slash_cmd and slash_cmd[0] in _NOISE_COMMANDS:
                noise_turn_indices.add(len(turns))
            if not content:
                # Still extract tool results even without text content
                if isinstance(raw, list):
                    tool_result_lines = extract_tool_result_summaries(raw)
                    if tool_result_lines:
                        turns.append(("assistant", "\n".join(tool_result_lines)))
                continue
            # Detect continuation summaries
            if content.startswith(_CONTINUATION_PREFIX):
                turns.append(("continuation", content))
            else:
                turns.append(("user", content))
            msg_count += 1
            # Extract tool results from user messages
            if isinstance(raw, list):
                tool_result_lines = extract_tool_result_summaries(raw)
                if tool_result_lines:
                    turns.append(("assistant", "\n".join(tool_result_lines)))

        elif msg_type == "assistant":
            raw_content = msg.get("message", {}).get("content", [])
            text = extract_text(raw_content)
            # Extract tool summaries from tool_use blocks
            tool_lines = []
            if isinstance(raw_content, list):
                tool_lines = extract_tool_summaries(raw_content)
            # Combine text + tool summaries
            parts = []
            if text:
                parts.append(text)
            if tool_lines:
                parts.append("\n".join(tool_lines))
            if parts:
                turns.append(("assistant", "\n\n".join(parts)))
                msg_count += 1

    meta["message_count"] = msg_count
    meta["files_modified"] = extract_files_modified(messages)
    meta["files_read"] = extract_files_read(messages)
    meta["tools_used"] = extract_tools_used(messages)
    all_files = meta["files_modified"] + meta["files_read"]
    meta["languages"] = extract_languages(all_files)

    if not turns:
        return None

    # Extract summary from first real user message (skip noise commands)
    for i, (role, content) in enumerate(turns):
        if role == "user" and i not in noise_turn_indices:
            clean = re.sub(r"<[^>]+>", "", content).strip()
            clean = re.sub(r"\s+", " ", clean)
            if clean and not clean.startswith("[Request interrupted"):
                meta["summary"] = clean[:200]
                break

    # Derive project from the parent directory name.
    # Detect remote host from staging dir pattern: .remote-<host>/<project>/<session>.jsonl
    host = ""
    parent = jsonl_path.parent
    grandparent = parent.parent
    if grandparent.name.startswith(".remote-"):
        host = grandparent.name.removeprefix(".remote-")
        meta["host"] = host
    meta["project"] = project_name_from_path(parent.name)

    if is_low_quality_session(meta, turns):
        return None

    return {"meta": meta, "turns": turns}


def merge_consecutive_turns(turns: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Merge consecutive same-role turns into single blocks."""
    if not turns:
        return turns
    merged = []
    for role, content in turns:
        if merged and merged[-1][0] == role:
            merged[-1] = (role, merged[-1][1] + "\n\n---\n\n" + content)
        else:
            merged.append((role, content))
    return merged


def render_markdown(session: dict) -> str:
    """Render a parsed session as Obsidian markdown."""
    meta = session["meta"]
    turns = session["turns"]

    # Frontmatter
    lines = ["---"]
    lines.append(f"session-id: {meta['session_id']}")
    if meta.get("source"):
        lines.append(f"source: {meta['source']}")
    if meta.get("slug"):
        lines.append(f"slug: {meta['slug']}")
    lines.append(f"project: {meta['project']}")
    if meta.get("host"):
        lines.append(f"host: {meta['host']}")
    if meta.get("cwd"):
        lines.append(f"cwd: {meta['cwd']}")
    if meta.get("git_branch"):
        lines.append(f"git-branch: {meta['git_branch']}")
    lines.append(f"date: {meta['date']}")
    lines.append(f"start-time: {meta['start_time'].isoformat()}")
    lines.append(f"end-time: {meta['end_time'].isoformat()}")
    lines.append(f"duration-minutes: {meta['duration_minutes']}")
    lines.append(f"message-count: {meta['message_count']}")
    if meta.get("summary"):
        # Escape quotes in summary for YAML
        safe_summary = meta["summary"].replace('"', '\\"')
        lines.append(f'summary: "{safe_summary}"')
    if meta.get("languages"):
        lines.append("languages:")
        for lang in meta["languages"]:
            lines.append(f"  - {lang}")
    if meta.get("tools_used"):
        lines.append("tools-used:")
        for tool in meta["tools_used"]:
            lines.append(f"  - {tool}")
    if meta.get("files_modified"):
        lines.append("files-modified:")
        for fp in meta["files_modified"]:
            lines.append(f'  - "{fp}"')
    if meta.get("files_read"):
        lines.append("files-read:")
        for fp in meta["files_read"]:
            lines.append(f'  - "{fp}"')
    lines.append("---")
    lines.append("")

    # Title
    slug_display = meta.get("slug") or meta["session_id"][:8]
    lines.append(f"# {meta['date']} — {slug_display}")
    lines.append("")

    # Topic summary
    if meta.get("summary"):
        lines.append(f"> **Topic**: {meta['summary']}")
        lines.append("")

    lines.append(f"**Project**: {meta['project']}  ")
    if meta.get("cwd"):
        lines.append(f"**CWD**: `{meta['cwd']}`  ")
    if meta.get("git_branch"):
        lines.append(f"**Branch**: `{meta['git_branch']}`")
    lines.append("")

    # Conversation — merge consecutive same-role turns
    merged = merge_consecutive_turns(turns)
    for role, content in merged:
        if role == "continuation":
            lines.append("## Context (continued)")
        elif role == "user":
            lines.append("## User")
        else:
            lines.append("## Assistant")
        lines.append("")
        lines.append(content)
        lines.append("")

    return "\n".join(lines)


def output_path(meta: dict) -> Path:
    """Generate output path: claude-code-{host}/{project}/{date}-{id8}-{slug}.md"""
    host = meta.get("host", "")
    host_dir = HOST_DIRS.get(host, f"claude-code-{host}")
    session_id_short = meta["session_id"][:8]
    slug = meta.get("slug", "session")
    # Sanitize slug for filesystem
    slug = re.sub(r"[^\w\-]", "-", slug).strip("-")
    if not slug:
        slug = "session"
    filename = f"{meta['date']}-{session_id_short}-{slug}.md"
    return RECALL_DIR / host_dir / meta["project"] / filename


def export_session(jsonl_path: Path, manifest: dict) -> str | None:
    """Export a single session. Returns session_id if exported, None if skipped."""
    session = parse_session(jsonl_path)
    if not session:
        return None

    sid = session["meta"]["session_id"]
    current_size = jsonl_path.stat().st_size
    prev = manifest.get("exported", {}).get(sid)
    if prev and prev.get("source_size") == current_size:
        return None

    md = render_markdown(session)
    out = output_path(session["meta"])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md)

    # Clean up old file if path changed
    if prev and prev.get("path"):
        old_path = RECALL_DIR / prev["path"]
        if old_path != out and old_path.exists():
            old_path.unlink()

    manifest.setdefault("exported", {})[sid] = {
        "path": str(out.relative_to(RECALL_DIR)),
        "date": session["meta"]["date"],
        "project": session["meta"]["project"],
        "source_size": current_size,
    }

    return sid


def find_all_sessions() -> list[Path]:
    """Find all JSONL session files, including remote staging dirs."""
    sessions = []
    if not PROJECTS_DIR.exists():
        return sessions
    for entry in PROJECTS_DIR.iterdir():
        if not entry.is_dir():
            continue
        if entry.name.startswith(".remote-"):
            # Remote staging: .remote-<host>/<project>/<session>.jsonl
            for project_dir in entry.iterdir():
                if project_dir.is_dir():
                    for jsonl in project_dir.glob("*.jsonl"):
                        sessions.append(jsonl)
        else:
            for jsonl in entry.glob("*.jsonl"):
                sessions.append(jsonl)
    return sorted(sessions)


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Export Claude Code and Codex CLI sessions to Obsidian markdown"
    )
    parser.add_argument("--single", type=str, help="Export a single Claude Code JSONL file")
    parser.add_argument("--single-codex", type=str, help="Export a single Codex CLI JSONL file")
    parser.add_argument("--codex", action="store_true", help="Export only Codex CLI sessions")
    parser.add_argument("--claude", action="store_true", help="Export only Claude Code sessions")
    parser.add_argument("--force", action="store_true", help="Force re-export all sessions")
    args = parser.parse_args()

    manifest = load_manifest()

    if args.force:
        for sid in manifest.get("exported", {}):
            manifest["exported"][sid].pop("source_size", None)

    # Single-file modes
    if args.single:
        path = Path(args.single)
        if not path.exists():
            print(f"File not found: {path}", file=sys.stderr)
            sys.exit(1)
        sid = export_session(path, manifest)
        if sid:
            save_manifest(manifest)
            print(f"Exported: {sid}")
        else:
            print("Skipped (already exported, subagent, or empty)")
        return

    if args.single_codex:
        path = Path(args.single_codex)
        if not path.exists():
            print(f"File not found: {path}", file=sys.stderr)
            sys.exit(1)
        sid = export_codex_session(path, manifest)
        if sid:
            save_manifest(manifest)
            print(f"Exported: {sid}")
        else:
            print("Skipped (already exported, subagent, or empty)")
        return

    # Default: export both unless --codex or --claude restricts scope
    do_claude = not args.codex or args.claude
    do_codex = not args.claude or args.codex
    # If neither flag is set, do both
    if not args.codex and not args.claude:
        do_claude = do_codex = True

    exported = 0
    skipped = 0

    if do_claude:
        sessions = find_all_sessions()
        print(f"Claude Code: found {len(sessions)} session files")
        for jsonl_path in sessions:
            sid = export_session(jsonl_path, manifest)
            if sid:
                exported += 1
            else:
                skipped += 1

    if do_codex:
        codex_sessions = find_all_codex_sessions()
        print(f"Codex CLI: found {len(codex_sessions)} session files")
        for jsonl_path in codex_sessions:
            sid = export_codex_session(jsonl_path, manifest)
            if sid:
                exported += 1
            else:
                skipped += 1

    save_manifest(manifest)
    print(f"Exported: {exported}, Skipped: {skipped}")


if __name__ == "__main__":
    main()
