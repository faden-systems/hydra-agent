#!/usr/bin/env python3
"""Flatten engine transcripts to plain text for the transition read (loops/b3.md).

Two sources, both plain-text JSONL: Claude Code's session file (`$CLAUDE_CONFIG_DIR/projects/<encoded cwd>/
<session-id>.jsonl`) and a Codex rollout (`$CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl`). `flatten_claude` and
`flatten_codex` turn them into entries `{at, role, kind, text}` in file order with nothing summarized or reordered:
every user/assistant text whole, tool calls as `tool <name>(<arguments>)`, tool results whole up to a cap and then
cut with `[... N more chars omitted]`, compaction summaries as `summary:` entries. Reasoning and thinking blocks are
dropped, and so are the harnesses' own records (Claude's attachment/queue/cost lines, Codex's developer messages,
events and token counts): they are not conversation. `render` writes `HH:MMZ role: text` per entry with a blank
line between entries; `window` keeps the newest entries within a token budget (estimate: chars / 3.5), cutting at an
entry boundary, and prefixes one `Transcript window:` line. The finders locate the files for the supervisor.
"""
import datetime as _dt
import json
import math
import os
import re

TOOL_RESULT_MAX_CHARS = 4000
CHARS_PER_TOKEN = 3.5
WINDOW_LINE_RESERVE = 160  # chars reserved for the `Transcript window:` line inside the budget
UTC = _dt.timezone.utc
TEXT_BLOCKS = ("text", "input_text", "output_text")
HIDDEN_BLOCKS = ("thinking", "redacted_thinking", "reasoning")
CONVERSATION_ROLES = ("user", "assistant")
_STAMP = r"(?:\d\d:\d\dZ|--:--Z)"
_ENTRY_START_RE = re.compile(r"(?:\A|\n\n)(?=" + _STAMP + r" \S+: )")
_STAMP_RE = re.compile(r"\A(" + _STAMP + r") ")


# ----------------------------------------------------------------------------------------------- time

def parse_ts(value):
    """A transcript or ledger timestamp (ISO 8601 with or without fractional seconds, `Z` or an offset; also an
    aware datetime or a unix time) -> aware UTC datetime; None when absent or unparsable."""
    if value is None or value == "":
        return None
    if isinstance(value, _dt.datetime):
        d = value if value.tzinfo else value.replace(tzinfo=UTC)
        return d.astimezone(UTC)
    if isinstance(value, (int, float)):
        return _dt.datetime.fromtimestamp(value, UTC)
    s = str(value).strip()
    if s[-1:] in ("Z", "z"):
        s = s[:-1] + "+00:00"
    try:
        d = _dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    return (d if d.tzinfo else d.replace(tzinfo=UTC)).astimezone(UTC)


def iso(dt_):
    return dt_.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def stamp(at):
    """`HH:MMZ` for the render line; `--:--Z` when the entry has no usable timestamp."""
    d = parse_ts(at)
    return d.strftime("%H:%MZ") if d else "--:--Z"


def _keep(at, since):
    """Entries at or after `since` are kept; an entry without a timestamp is kept (its moment is unknown)."""
    if since is None:
        return True
    d = parse_ts(at)
    return d is None or d >= since


# ----------------------------------------------------------------------------------------------- lines and text

def read_lines(path):
    """Every JSON object of a JSONL file, in order; blank and broken lines skipped; [] when unreadable."""
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
    except OSError:
        return []
    return out


def entry(at, role, kind, text):
    return {"at": at or "", "role": role, "kind": kind, "text": text}


def cap_text(text, max_chars):
    """`text` whole up to `max_chars`, then cut with the marker; `None` means no cap."""
    if max_chars is None or max_chars < 0 or len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n[... {len(text) - max_chars} more chars omitted]"


def blocks_text(content):
    """The plain text of a content field: a string, or a list of blocks carrying text/input_text/output_text.
    Hidden (thinking) blocks give nothing; any other block is named in brackets so that it is not lost silently."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        content = [content]
    if not isinstance(content, list):
        return str(content)
    parts = []
    for b in content:
        if isinstance(b, str):
            parts.append(b)
        elif isinstance(b, dict):
            kind = b.get("type")
            if kind in HIDDEN_BLOCKS:
                continue
            text = b.get("text")
            if isinstance(text, str):
                parts.append(text)
            elif kind in TEXT_BLOCKS:
                parts.append(str(text or ""))
            else:
                parts.append(f"[{kind or 'block'}]")
    return "\n".join(p for p in parts if p)


def args_text(value):
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def tool_call_text(name, arguments):
    return f"tool {name or '?'}({args_text(arguments)})"


# ----------------------------------------------------------------------------------------------- flatteners

def flatten_claude(path, since_ts=None, tool_result_max_chars=TOOL_RESULT_MAX_CHARS):
    """Entries of a Claude Code session file. Lines with `type` user/assistant become text, tool_call (one per
    `tool_use` block) and tool_result (one per `tool_result` block, capped) entries; `summary` lines become
    `summary` entries; thinking blocks and the harness's other line types are dropped."""
    since = parse_ts(since_ts)
    out = []
    for obj in read_lines(path):
        kind = obj.get("type")
        at = obj.get("timestamp") or ""
        if kind == "summary":
            text = obj.get("summary")
            if isinstance(text, str) and text.strip() and _keep(at, since):
                out.append(entry(at, "summary", "text", text))
            continue
        if kind not in CONVERSATION_ROLES or not _keep(at, since):
            continue
        msg = obj.get("message")
        if not isinstance(msg, dict):
            continue
        role = msg.get("role") or kind
        content = msg.get("content")
        if isinstance(content, str):
            if content.strip():
                out.append(entry(at, role, "text", content))
            continue
        if not isinstance(content, list):
            continue
        buf = []

        def flush():
            if buf:
                out.append(entry(at, role, "text", "\n".join(buf)))
                buf.clear()

        for b in content:
            if isinstance(b, str):
                if b.strip():
                    buf.append(b)
                continue
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt in HIDDEN_BLOCKS:
                continue
            if bt == "tool_use":
                flush()
                out.append(entry(at, role, "tool_call", tool_call_text(b.get("name"), b.get("input"))))
            elif bt == "tool_result":
                flush()
                out.append(entry(at, "tool", "tool_result",
                                 cap_text(blocks_text(b.get("content")) or "(no output)", tool_result_max_chars)))
            else:
                text = blocks_text([b])
                if text.strip():
                    buf.append(text)
        flush()
    return out


def flatten_codex(path, since_ts=None, tool_result_max_chars=TOOL_RESULT_MAX_CHARS):
    """Entries of a Codex rollout. `payload.type == "message"` with role user/assistant becomes text; both tool-call
    shapes (`function_call` with `arguments`, `custom_tool_call` with `input`) become tool_call entries and both
    output shapes tool_result entries (capped); reasoning, developer messages, events and token counts are dropped."""
    since = parse_ts(since_ts)
    out = []
    for obj in read_lines(path):
        payload = obj.get("payload")
        if not isinstance(payload, dict):
            continue
        at = obj.get("timestamp") or payload.get("timestamp") or ""
        pt = payload.get("type")
        if pt == "message":
            if payload.get("role") not in CONVERSATION_ROLES or not _keep(at, since):
                continue
            text = blocks_text(payload.get("content"))
            if text.strip():
                out.append(entry(at, payload["role"], "text", text))
        elif pt in ("function_call", "custom_tool_call"):
            if not _keep(at, since):
                continue
            arguments = payload.get("arguments") if pt == "function_call" else payload.get("input")
            out.append(entry(at, "assistant", "tool_call", tool_call_text(payload.get("name"), arguments)))
        elif pt in ("function_call_output", "custom_tool_call_output"):
            if not _keep(at, since):
                continue
            out.append(entry(at, "tool", "tool_result",
                             cap_text(blocks_text(payload.get("output")) or "(no output)", tool_result_max_chars)))
    return out


# ----------------------------------------------------------------------------------------------- render and window

def render(entries):
    """`HH:MMZ role: text` per entry, a blank line between entries."""
    return "\n\n".join(f"{stamp(e.get('at'))} {e.get('role') or '?'}: {e.get('text') or ''}" for e in entries)


def estimate_tokens(text):
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def split_entries(text):
    """The rendered entries back as a list: an entry starts at the text start or after a blank line with a
    `HH:MMZ role: ` stamp, so a blank line inside a tool result does not split it."""
    if not text:
        return []
    starts = [m.end() for m in _ENTRY_START_RE.finditer(text)]
    if not starts or starts[0] != 0:
        starts.insert(0, 0)
    chunks = []
    for i, s in enumerate(starts):
        end = starts[i + 1] - 2 if i + 1 < len(starts) else len(text)
        chunks.append(text[s:end])
    return chunks


def window(text, max_tokens):
    """The newest entries of a rendered transcript within `max_tokens` (chars / 3.5), cut at an entry boundary,
    behind one `Transcript window:` line. Returns (text, meta) with meta = {entries_total, entries_kept, first_at,
    last_at, est_tokens, cut}. A single newest entry larger than the whole budget keeps its tail."""
    chunks = split_entries(text)
    total = len(chunks)
    budget = max(int(max_tokens * CHARS_PER_TOKEN) - WINDOW_LINE_RESERVE, 0)
    kept, used = [], 0
    for c in reversed(chunks):
        need = len(c) + (2 if kept else 0)
        if used + need > budget:
            break
        kept.insert(0, c)
        used += need
    head_cut = False
    if not kept and chunks:
        c = chunks[-1]
        kept = [c]
        if len(c) > budget:
            marker = "[... {n} earlier chars of this entry omitted]\n"
            omitted = max(len(c) - budget + len(marker) + 8, 0)
            kept = [marker.format(n=omitted) + c[omitted:]]
            head_cut = True
    first_at = _STAMP_RE.match(chunks[total - len(kept)]).group(1) if kept and _STAMP_RE.match(chunks[total - len(kept)]) else ""
    last_at = _STAMP_RE.match(chunks[-1]).group(1) if kept and _STAMP_RE.match(chunks[-1]) else ""
    earlier = total - len(kept)
    line = (f"Transcript window: {first_at or 'none'} to {last_at or 'none'}, {len(kept)} of {total} entries; "
            f"{earlier} earlier entries not included.")
    out = line + ("\n\n" + "\n\n".join(kept) if kept else "")
    meta = {"entries_total": total, "entries_kept": len(kept), "first_at": first_at, "last_at": last_at,
            "est_tokens": estimate_tokens(out), "cut": bool(earlier or head_cut)}
    return out, meta


# ----------------------------------------------------------------------------------------------- finders

def claude_project_dirs(config_dir, cwd):
    """Candidate `$CLAUDE_CONFIG_DIR/projects/<encoded cwd>` directories for the engine's working directory."""
    cwd = os.path.abspath(cwd)
    encodings = [cwd.replace("/", "-"), re.sub(r"[^A-Za-z0-9]", "-", cwd)]
    seen, out = set(), []
    for enc in encodings:
        if enc not in seen:
            seen.add(enc)
            out.append(os.path.join(config_dir, "projects", enc))
    return out


def _newest(paths):
    paths = [p for p in paths if os.path.isfile(p)]
    return max(paths, key=lambda p: (os.path.getmtime(p), p)) if paths else None


def find_claude_transcript(config_dir, cwd, session_id=None):
    """The Claude Code session file: `projects/<encoded cwd>/<session-id>.jsonl`, else that session id under any
    project directory, else the newest session file of the cwd's project; None when there is none."""
    dirs = claude_project_dirs(config_dir, cwd)
    if session_id:
        for d in dirs:
            p = os.path.join(d, f"{session_id}.jsonl")
            if os.path.isfile(p):
                return p
        projects = os.path.join(config_dir, "projects")
        if os.path.isdir(projects):
            hit = _newest(os.path.join(projects, n, f"{session_id}.jsonl") for n in os.listdir(projects))
            if hit:
                return hit
    return _newest(os.path.join(d, n) for d in dirs if os.path.isdir(d) for n in os.listdir(d) if n.endswith(".jsonl"))


def rollout_cwd(path):
    """The `cwd` of a rollout's `session_meta` line (looked for in the first few lines); None when absent."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for _ in range(8):
                line = f.readline()
                if not line:
                    break
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict) and obj.get("type") == "session_meta":
                    cwd = (obj.get("payload") or {}).get("cwd")
                    return os.path.abspath(cwd) if cwd else None
    except OSError:
        return None
    return None


def find_codex_rollout(codex_home, cwd, hint=None):
    """The Codex rollout to read: the file `codex exec` reported (`hint`) when it exists, else the latest
    `sessions/**/rollout-*.jsonl` whose `session_meta` cwd is the manager's, else the latest rollout at all."""
    if hint and os.path.isfile(hint):
        return hint
    root = os.path.join(codex_home or "", "sessions")
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        files.extend(os.path.join(dirpath, n) for n in filenames if n.startswith("rollout-") and n.endswith(".jsonl"))
    if not files:
        return None
    want = os.path.abspath(cwd) if cwd else None
    matched = [p for p in files if want and rollout_cwd(p) == want]
    return _newest(matched or files)
