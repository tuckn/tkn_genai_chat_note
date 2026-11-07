"""Session digests: what a conversation contains, made without generative AI (design 3.2-3.5).

A digest keeps the person's requests, each turn's final answer, tool executions, the Vault
notes the conversation opened or changed and mechanically known facts. Every item carries an
evidence ID `<app>/<source_id>/<session_id>@<sha12>#L<line>`, where `sha12` starts the SHA-256 of
the decoded content: a `.jsonl` and its `.jsonl.zst`, or the application's folder and the
archive, give the same ID for the same content. A digest is an intermediate form, not data to
keep: it is cached below `<cache_root>/digests/` (unless `digests.cache` is false) and rebuilt
whenever the content, the settings or the application version changes.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from tkn_genai_chat_archiver.view import ArchiveError, ArchiveVersion
from tkn_genai_chat_search.library import Session, Title, ToolEvent, read_claude_code, read_codex

from . import __version__
from .chat_sources import LogFile, OpenSource, SourceReadError
from .path_text import PathText, last_folder_name
from .runtime import PipelineError, atomic_write_json

DIGEST_SCHEMA_VERSION = "2.1.0"
CACHE_FOLDER = "digests"
REQUEST_CHARS = 4000
FINAL_ANSWER_CHARS = 1000
TARGET_CHARS = 300
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
# Claude Code user turns that the application wrote (command output, notifications, interruptions).
GENERATED_USER_TEXT = re.compile(
    r"^\s*(?:<(?:local-command-stdout|local-command-stderr|local-command-caveat|bash-stdout|bash-stderr|"
    r"task-notification|side-sessions-event|ci-monitor-event)\b|\[Request interrupted by user)",
    re.IGNORECASE,
)
# Blocks the Codex app adds to a user turn (page opens, review actions); a turn of only these is not a request.
CODEX_APP_BLOCK = re.compile(
    r"<(external_[a-z_]+|user_action|turn_aborted)\b[^>]*>.*?</\1>|<(external_[a-z_]+|user_action|turn_aborted)\b[^>]*/>",
    re.IGNORECASE | re.DOTALL,
)
# A Claude Code slash command turn: `<command-name>/x</command-name>` with optional message and arguments.
SLASH_COMMAND = re.compile(
    r"^\s*<command-name>(?P<name>[^<]*)</command-name>\s*(?:<command-message>[^<]*</command-message>\s*)?"
    r"(?:<command-args>(?P<args>.*?)</command-args>)?\s*$",
    re.IGNORECASE | re.DOTALL,
)
PASTED_OPEN = re.compile(r"<pasted_content\b[^>]*>", re.IGNORECASE)
PASTED_BLOCK = re.compile(r"<pasted_content\b[^>]*>.*?</pasted_content>", re.IGNORECASE | re.DOTALL)
READ_COMMANDS = ("cat", "type", "get-content", "gc", "head", "tail", "less", "more", "sed -n", "bat")
SEARCH_COMMANDS = ("rg", "grep", "select-string", "sls", "findstr", "find", "fd", "git grep", "ag")
LIST_COMMANDS = ("ls", "dir", "get-childitem", "gci", "tree", "git ls-files")
FAILURE_KINDS = (
    ("rejected", ("user doesn't want to proceed", "user rejected", "denied by the user", "permission to use",
                  "was blocked", "user declined")),
    ("not-found", ("no such file", "does not exist", "cannot find path", "cannot find the path", "not found",
                   "enoent", "itemnotfound", "file not found")),
    ("permission", ("permission denied", "access is denied", "unauthorizedaccess", "eacces")),
    ("encoding", ("unicodedecodeerror", "codec can't", "invalid byte", "encoding")),
    ("timeout", ("timed out", "timeout")),
    ("too-large", ("exceeds maximum", "too large", "too many tokens", "file content (")),
)


@dataclass(frozen=True)
class DigestContext:
    """What digests need besides the log: the clock, the Vault location and path aliases."""

    utc_offset_minutes: int
    paths: PathText
    vault_root: str  # normalized folder text; empty when no Vault is configured
    actions_dir: str  # normalized relative folder, e.g. `notes/actions`
    aliases: tuple[tuple[str, ...], ...] = ()

    def signature(self) -> str:
        # The package version is part of the key: a release that changes a rule rebuilds cached digests.
        value = {"version": DIGEST_SCHEMA_VERSION, "application": __version__, "utc": self.utc_offset_minutes,
                 "vault": self.vault_root,
                 "actions": self.actions_dir, "aliases": [list(group) for group in self.aliases]}
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    def local(self, value: str) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (AttributeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(timezone(timedelta(minutes=self.utc_offset_minutes)))


def evidence_id(app: str, source_id: str, session_id: str, content_sha256: str, line: int) -> str:
    """`<app>/<source_id>/<session_id>@<first 12 hex of the decoded content's SHA-256>#L<line>`."""
    return f"{app}/{source_id}/{session_id}@{content_sha256[:12]}#L{line}"


def line_count(data: bytes) -> int:
    return data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)


def pasted_kind(text: str) -> str:
    """`whole` when the turn is only pasted text, `partial` when it mixes pasted and typed text."""
    if not PASTED_OPEN.search(text):
        return "none"
    return "whole" if not PASTED_BLOCK.sub("", text).strip() else "partial"


def _strings(value: Any, depth: int = 0) -> list[str]:
    if depth > 6:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _strings(item, depth + 1)]
    if isinstance(value, list):
        return [text for item in value for text in _strings(item, depth + 1)]
    return []


def _command_text(event: ToolEvent) -> str:
    arguments = event.arguments
    if isinstance(arguments, dict):
        command = arguments.get("command") or arguments.get("cmd")
        if isinstance(command, list):
            return " ".join(str(item) for item in command)
        if isinstance(command, str):
            return command
    return arguments if isinstance(arguments, str) else ""


def _shell_category(command: str) -> str:
    text = command.strip().casefold()
    # A shell wrapper such as `pwsh -Command "..."` names the real command after it.
    match = re.search(r"(?:-command|-c)\s+[\"']?(.+)", text)
    if match and re.match(r"^\S*(pwsh|powershell|bash|sh|cmd)(\.exe)?\b", text):
        text = match.group(1)
    for category, names in (("read", READ_COMMANDS), ("search", SEARCH_COMMANDS), ("list", LIST_COMMANDS)):
        if any(text == name or text.startswith(name + " ") for name in names):
            return category
    return "run"


def tool_category(event: ToolEvent) -> str:
    name = event.name
    lowered = name.casefold()
    if event.origin == "item":
        if name == "CommandExecution":
            parsed = event.arguments.get("parsed_cmd") if isinstance(event.arguments, dict) else None
            kinds = {str(item.get("type")) for item in parsed or [] if isinstance(item, dict)}
            for kind, category in (("read", "read"), ("search", "search"), ("list_files", "list")):
                if kinds == {kind}:
                    return category
            return _shell_category(_command_text(event))
        return {"FileChange": "edit", "McpToolCall": "mcp", "WebSearch": "web", "ImageView": "read"}.get(
            name, "web" if "web" in lowered else "other")
    if lowered in {"read", "notebookread", "view"}:
        return "read"
    if lowered in {"grep", "glob", "toolsearch", "tool_search", "tool_search_call"}:
        return "search"
    if lowered in {"ls", "list"}:
        return "list"
    if lowered in {"edit", "write", "multiedit", "notebookedit", "apply_patch", "str_replace_editor"}:
        return "edit"
    if lowered in {"bash", "powershell", "shell", "local_shell_call", "exec_command", "shell_command"}:
        return _shell_category(_command_text(event))
    if lowered in {"websearch", "webfetch", "web_search_call"} or lowered.startswith("mcp__claude_browser"):
        return "web"
    if lowered == "skill":
        return "skill"
    if lowered in {"task", "agent"}:
        return "agent"
    if lowered.startswith("mcp__"):
        return "mcp"
    if lowered == "exec":
        return "script"  # Codex's script runner; its executions appear as separate items.
    return "other"


def failure_kind(event: ToolEvent) -> str | None:
    if not event.is_error:
        return None
    text = event.error_preview.casefold()
    return next((kind for kind, markers in FAILURE_KINDS if any(marker in text for marker in markers)), "other")


def _targets(event: ToolEvent) -> list[str]:
    arguments = event.arguments
    values: list[str] = []
    if isinstance(arguments, dict):
        for key in ("file_path", "notebook_path", "path", "pattern", "glob", "command", "cmd", "url", "query",
                    "skill", "server", "tool"):
            item = arguments.get(key)
            if isinstance(item, list):
                item = " ".join(str(part) for part in item)
            if isinstance(item, str) and item:
                values.append(item)
        changes = arguments.get("changes")
        if isinstance(changes, dict):
            values.extend(str(key) for key in changes)
    elif isinstance(arguments, str):
        values.append(arguments)
    return [value[:TARGET_CHARS] for value in values[:5]]


class _VaultFinder:
    """Find Vault notes, and Action notes in particular, in path-bearing tool arguments."""

    STOP = "\"'\n\r\t`|<>*?"

    def __init__(self, context: DigestContext) -> None:
        self.context = context
        self.root = context.vault_root
        self.actions_marker = context.actions_dir + "/" if context.actions_dir else ""
        name = rf"([^{re.escape(self.STOP)}]*?\.md)(?![\w.])"
        self.pattern_root = re.compile(re.escape(self.root + "/") + name) if self.root else None
        self.pattern_actions = re.compile(re.escape(self.actions_marker) + name) if self.actions_marker else None

    def find(self, event: ToolEvent, *, relative_ok: bool) -> set[str]:
        """Vault-relative paths (normalized) of Markdown notes named in the call's arguments.

        Absolute paths count only below the Vault root; paths relative to the working folder count
        only when the conversation works in the Vault root (`relative_ok`).
        """
        found: set[str] = set()
        if not self.root:
            return found
        for raw in _strings(event.arguments):
            text = self.context.paths.text(raw)
            if self.pattern_root:
                found.update(match.group(1) for match in self.pattern_root.finditer(text))
            if self.pattern_actions and relative_ok:
                for match in self.pattern_actions.finditer(text):
                    before = text[: match.start()]
                    if before.endswith("/") and not before.endswith("./"):
                        continue  # Part of a longer absolute path; the root pattern decides those.
                    found.add(self.actions_marker + match.group(1))
        return {path for path in found if ".." not in path.split("/")}


def _access(category: str) -> str:
    return {"edit": "edit", "read": "read", "search": "read", "list": "read"}.get(category, "command")


def request_text(text: str, app: str) -> tuple[str | None, str]:
    """The request text of a user turn, or None with the reason when the application wrote it."""
    if app == "codex":
        remaining = CODEX_APP_BLOCK.sub("", text).strip()
        if not remaining:
            return None, "codex-app-block"
        return remaining, ""
    command = SLASH_COMMAND.match(text)
    if command:
        args = (command.group("args") or "").strip()
        if not args:
            return None, "slash-command"
        return f"{command.group('name').strip()} {args}", ""
    if GENERATED_USER_TEXT.match(text):
        return None, "generated"
    return text, ""


def _human_requests(session: Session, app: str) -> tuple[list[tuple[Any, str, str, str]], Counter[str]]:
    """Requests the person wrote: (message, text, confidence, pasted kind) and why others were excluded."""
    kept: list[tuple[Any, str, str, str]] = []
    excluded: Counter[str] = Counter()
    for message in session.messages:
        if message.role != "user":
            continue
        confidence = "high"
        if app == "claude-code":
            attributes = session.message_attributes.get(message.line_number, {})
            if message.is_sidechain or attributes.get("isSidechain") is True:
                excluded["sidechain"] += 1
                continue
            origin = attributes.get("turnOrigin")
            if origin is not None and origin != "human":
                excluded[f"turnOrigin:{origin}"] += 1
                continue
            if origin is None:
                confidence = "medium"  # Older records have no turnOrigin.
        text, reason = request_text(message.text, app)
        if text is None:
            excluded[reason] += 1
            continue
        kept.append((message, text, confidence, pasted_kind(text)))
    return kept, excluded


def titles_sha256(app: str, codex_titles: dict[str, list[Title]] | None, session_id: str | None) -> str | None:
    """What a Codex digest's title depends on besides the content: the thread's names in
    `session_index.jsonl`, which differ between locations and change when a thread is renamed."""
    if app != "codex" or not session_id:
        return None
    entries = (codex_titles or {}).get(session_id.casefold(), [])
    value = json.dumps([[entry.kind, entry.title] for entry in entries], ensure_ascii=False)
    return hashlib.sha256(value.encode()).hexdigest()


def _own_records(session: Session, app: str) -> tuple[Session, dict[str, Any]]:
    """A Claude Code main transcript without the records copied from another session.

    Forking or resuming a conversation copies the earlier session's records, with that session's
    `sessionId`, into the new transcript. They are the earlier conversation's work, which its own
    digest already holds; counting them again would repeat its requests, tool executions and
    clues. Subagent transcripts record their parent's ID and are left whole.
    """
    if app != "claude-code" or session.kind != "main":
        return session, {"records": 0, "from": []}
    own = session.session_id.casefold()

    def kept(recorded: str) -> bool:
        return not recorded or recorded.casefold() == own

    messages = [message for message in session.messages if kept(message.session_id)]
    events = [event for event in session.tool_events if kept(event.session_id)]
    recorded = [m.session_id for m in session.messages] + [e.session_id for e in session.tool_events]
    others = Counter(value for value in recorded if not kept(value))
    if not others:
        return session, {"records": 0, "from": []}
    lines = {message.line_number for message in messages}
    dated = sorted((message.timestamp for message in messages if message.timestamp and not message.is_sidechain),
                   key=lambda value: value.replace("Z", "+00:00"))
    trimmed = dataclasses.replace(
        session, messages=messages, tool_events=events,
        message_attributes={line: value for line, value in session.message_attributes.items() if line in lines},
        created=dated[0] if dated else "", updated=dated[-1] if dated else "")
    return trimmed, {"records": sum(others.values()), "from": [name for name, _count in others.most_common()]}


def build_digest(log: LogFile, data: bytes, content_sha256: str, context: DigestContext,
                 codex_titles: dict[str, list[Title]] | None = None) -> dict[str, Any]:
    """Digest one conversation file's decoded content. A file that is not a conversation gives `session: null`.

    `file` depends only on the content; `location` says where this copy was read (app or archive).
    """
    if log.app == "codex":
        result = read_codex(data, log.relative_path, titles=codex_titles or {})
    else:
        result = read_claude_code(data, log.relative_path)
    session = result.session
    base: dict[str, Any] = {
        "schemaVersion": DIGEST_SCHEMA_VERSION,
        "settingsSha256": context.signature(),
        "titlesSha256": titles_sha256(log.app, codex_titles, session.session_id if session else None),
        "file": {"app": log.app, "sourceId": log.source_id, "relativePath": log.relative_path,
                 "contentSha256": content_sha256, "lineCount": line_count(data)},
        "location": log.location_record(),
    }
    coverage = result.coverage.to_dict()
    base["coverage"] = {"error": result.error, "parserWarnings": coverage["parser_warnings"],
                        "ignoredRecordTypes": coverage["details"].get("ignored_record_types", {}),
                        "excludedGeneratedMessages": coverage["details"].get("excluded_generated_messages", 0)}
    if session is None:
        return {**base, "session": None}
    session, copied = _own_records(session, log.app)
    app, source_id, session_id = log.app, log.source_id, session.session_id
    cwd = context.paths.folder(session.cwd) if session.cwd else ""

    def evidence(line: int) -> str:
        return evidence_id(app, source_id, session_id, content_sha256, line)

    requests, excluded = _human_requests(session, app)
    human_lines = [message.line_number for message, _t, _c, _p in requests]
    finals = []
    for index, line in enumerate(human_lines):
        end = human_lines[index + 1] if index + 1 < len(human_lines) else None
        answers = [m for m in session.messages if m.role == "assistant" and m.line_number > line
                   and (end is None or m.line_number < end) and not m.is_sidechain]
        if answers:
            last = answers[-1]
            finals.append({"evidenceId": evidence(last.line_number), "line": last.line_number,
                           "timestamp": last.timestamp, "text": last.text[:FINAL_ANSWER_CHARS],
                           "truncated": len(last.text) > FINAL_ANSWER_CHARS, "requestLine": line})
    finder = _VaultFinder(context)
    tool_events: list[dict[str, Any]] = []
    touches: dict[tuple[str, str], dict[str, Any]] = {}
    written: dict[str, dict[str, Any]] = {}
    facts: list[dict[str, Any]] = []
    for event in session.tool_events:
        category = tool_category(event)
        succeeded = None if event.is_error is None else not event.is_error
        if event.result_line_number is None and event.origin == "call":
            succeeded = None
        targets = _targets(event)
        item: dict[str, Any] = {
            "evidenceId": evidence(event.line_number), "line": event.line_number, "timestamp": event.timestamp,
            "tool": event.name, "category": category, "origin": event.origin, "targets": targets,
            "succeeded": succeeded, "failureKind": failure_kind(event), "outputChars": event.output_chars,
            "hasResult": event.result_line_number is not None, "sidechain": event.is_sidechain,
        }
        tool_events.append(item)
        if succeeded is False and category in {"read", "edit"}:
            continue
        for path in sorted(finder.find(event, relative_ok=bool(context.vault_root) and cwd == context.vault_root)):
            access = _access(category)
            key = (path, access)
            if key not in touches:
                touches[key] = {"path": path, "access": access, "tool": event.name,
                                "evidenceId": item["evidenceId"], "sidechain": event.is_sidechain}
        if category == "edit" and succeeded is not False:
            for target in targets:
                normalized = context.paths.text(target)
                if "/" in normalized or "." in normalized.rsplit("/", 1)[-1]:
                    written.setdefault(normalized, {"kind": "file-written", "path": normalized,
                                                    "evidenceId": item["evidenceId"]})
        command = _command_text(event).casefold()
        if succeeded is not False and command:
            if re.search(r"\bgit\b[^\n|;&]*\bcommit\b", command):
                facts.append({"kind": "git-commit", "evidenceId": item["evidenceId"]})
            if re.search(r"(?<![\w-])register-scheduledtask\b|schtasks(\.exe)?\s+/create", command):
                facts.append({"kind": "scheduled-task-registered", "evidenceId": item["evidenceId"]})
    user_requests = []
    for message, text, confidence, pasted in requests:
        local = context.local(message.timestamp)
        user_requests.append({
            "evidenceId": evidence(message.line_number), "line": message.line_number,
            "timestamp": message.timestamp, "localDate": local.date().isoformat() if local else None,
            "text": text[:REQUEST_CHARS], "truncated": len(text) > REQUEST_CHARS,
            "pasted": pasted, "confidence": confidence,
        })
    dated = [context.local(m.timestamp) for m in session.messages]
    local_times = sorted(value for value in dated if value is not None)
    day_ranges: dict[str, list[str]] = {}
    for value in local_times:
        span = day_ranges.setdefault(value.date().isoformat(), [value.isoformat(), value.isoformat()])
        span[1] = value.isoformat()
    return {
        **base,
        "session": {
            "sessionRef": {"app": app, "sourceId": source_id, "sessionId": session_id},
            "kind": session.kind,
            "parentSessionId": session.parent_session_id,
            "copiedRecords": copied,
            # Codex: who started the thread (`codex_exec`, `Codex Desktop` ...); empty for Claude Code.
            "originator": str(session.details.get("originator") or "") if app == "codex" else "",
            "cwd": cwd,
            "repo": last_folder_name(cwd),
            "title": session.title,
            "created": session.created,
            "updated": session.updated,
            "localDates": sorted({value.date().isoformat() for value in local_times}),
            "firstLocalTime": local_times[0].isoformat() if local_times else None,
            "lastLocalTime": local_times[-1].isoformat() if local_times else None,
            "dayRanges": day_ranges,  # local date -> [first, last] message time of that date
            "userRequests": user_requests,
            "excludedUserMessages": dict(sorted(excluded.items())),
            "assistantFinals": finals,
            "toolEvents": tool_events,
            "toolsWithoutResult": sum(1 for e in tool_events if not e["hasResult"]),
            "vaultTouches": sorted(touches.values(), key=lambda t: (t["path"], t["access"])),
            "facts": [*written.values(), *facts],
        },
    }


def digest_cache_root(cache_root: Path) -> Path:
    return cache_root / CACHE_FOLDER


class DigestStore:
    """Load session digests, reusing the cache when the content, settings and version match (design 3.2),
    and for Codex also the thread's names, which live outside the rollout.

    `cache_root` None (`digests.cache: false`) reads and writes no cache. The same content read
    twice in one run (a `.jsonl` and its `.jsonl.zst`, or both locations of `auto`) is digested once.
    """

    def __init__(self, context: DigestContext, cache_root: Path | None) -> None:
        self.context = context
        self.signature = context.signature()
        self.root = digest_cache_root(cache_root) if cache_root is not None else None
        self.seen: set[tuple[str, str, str]] = set()

    def path(self, log: LogFile) -> Path:
        if self.root is None:
            raise PipelineError("the digest cache is off (digests.cache: false)")
        key = hashlib.sha256(log.relative_path.encode()).hexdigest()[:32]
        root = self.root.resolve()
        path = (self.root / log.app / log.source_id / f"{key}.json").resolve()
        if not path.is_relative_to(root):
            raise PipelineError(f"digest cache path leaves {root}: {path}")
        return path

    def _cached(self, log: LogFile, content_sha256: str,
                codex_titles: dict[str, list[Title]]) -> dict[str, Any] | None:
        if self.root is None:
            return None
        path = self.path(log)
        try:
            cached = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
        except (OSError, ValueError):
            return None
        file = cached.get("file") if isinstance(cached, dict) else None
        if not (isinstance(cached, dict) and isinstance(file, dict)
                and cached.get("schemaVersion") == DIGEST_SCHEMA_VERSION
                and cached.get("settingsSha256") == self.signature
                and file.get("contentSha256") == content_sha256
                and (file.get("app"), file.get("sourceId"), file.get("relativePath"))
                == (log.app, log.source_id, log.relative_path)):
            return None
        session = cached.get("session")
        reference = session.get("sessionRef") if isinstance(session, dict) else None
        session_id = reference.get("sessionId") if isinstance(reference, dict) else None
        if cached.get("titlesSha256") != titles_sha256(log.app, codex_titles, session_id):
            return None  # A Codex thread's name changed, or this location's index names it differently.
        # The content is the same; only where this copy was read may differ.
        return {**cached, "location": log.location_record()}

    def load(self, source: OpenSource, log: LogFile) -> tuple[dict[str, Any] | None, str]:
        """(digest, `cached` or `built`), `(None, "same-content")` or `(None, "unreadable: ...")`."""
        known = log.known_content_sha256
        if known is not None:
            if (log.app, log.source_id, known) in self.seen:
                return None, "same-content"
            cached = self._cached(log, known, source.codex_titles)
            if cached is not None:
                self.seen.add((log.app, log.source_id, known))
                return cached, "cached"
        try:
            data, content_sha256 = source.read(log)
        except (ArchiveError, SourceReadError) as exc:
            return None, f"unreadable: {exc}"
        key = (log.app, log.source_id, content_sha256)
        if key in self.seen:
            return None, "same-content"
        self.seen.add(key)
        if known is None:
            cached = self._cached(log, content_sha256, source.codex_titles)
            if cached is not None:
                return cached, "cached"
        digest = build_digest(log, data, content_sha256, self.context, source.codex_titles)
        if self.root is not None:
            atomic_write_json(self.path(log), digest)
        return digest, "built"


def continuation(versions: list[ArchiveVersion], processed_sha256: str | None, processed_lines: int) -> dict[str, Any]:
    """Where to continue reading a file (design 3.5): after the processed lines when every later
    version only appended to the processed one; from the start (and avoiding duplicates) otherwise."""
    if not versions:
        return {"fromLine": 1, "rewritten": False}
    latest = versions[-1]
    if processed_sha256 is None:
        return {"fromLine": 1, "rewritten": False}
    if processed_sha256 == latest.identity_sha256:
        return {"fromLine": processed_lines + 1, "rewritten": False}
    shas = [version.identity_sha256 for version in versions]
    if processed_sha256 in shas:
        later = versions[shas.index(processed_sha256) + 1:]
        if later and all(version.previous_relation == "appended" for version in later):
            return {"fromLine": processed_lines + 1, "rewritten": False}
    return {"fromLine": 1, "rewritten": True}
