"""`actions link --dry-run`: match recent conversations to Action notes with clues A-D only.

Clues (design 4.2): A the conversation opened or changed the Action file; B the Action's Notes cite
the conversation's session ID; C the person's request names the Action (WikiLink, file name, title
or alias); D the working folder's repository name matches exactly one target Action. A, B and C are
strong and link directly; a unique D links with medium strength; several D candidates are left for
the later AI step. An A that only names the file in a command's arguments is weak: it links only
together with another clue for the same Action. A conversation that links to nothing, but whose
repository matches a finished Action, is reported as follow-up work, a candidate for a new Action.
Conversations are read where `chat_sources` says (the application's folder, the archive or both).
Nothing is written to the Vault, an application's folder or an archive, and no generative AI is
called. The report goes below `processes.state_root`, the digest cache below `<cache_root>/digests/`.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .chat_sources import conversation_files, conversation_key, open_sources
from .config import AppConfig
from .digests import DigestContext, DigestStore, digest_cache_root
from .path_text import PathText
from .runtime import PipelineError, atomic_write_json, atomic_write_text
from .vault_actions import ActionNote, load_actions

LOGGER = logging.getLogger(__name__)
REPORT_SCHEMA_VERSION = "2.0.0"
DECISIONS = ("linked", "ambiguous", "followup", "unlinked")
WIKILINK = re.compile(r"\[\[([^\]|#^]+)")


@dataclass
class Conversation:
    key: tuple[str, str, str]
    digest: dict[str, Any]
    subagents: list[dict[str, Any]] = field(default_factory=list)

    @property
    def session(self) -> dict[str, Any]:
        value: dict[str, Any] = self.digest["session"]
        return value

    def touches(self) -> list[dict[str, Any]]:
        items = list(self.session["vaultTouches"])
        for subagent in self.subagents:
            items.extend({**touch, "subagent": True} for touch in subagent["session"]["vaultTouches"])
        return items


def _nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _token(name: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![0-9a-z_]){re.escape(name.casefold())}(?![0-9a-z_])")


class ActionIndex:
    def __init__(self, actions: list[ActionNote], actions_dir: str, min_chars: int) -> None:
        self.actions = actions
        self.by_path = {action.key(): action for action in actions}
        self.by_stem: dict[str, list[ActionNote]] = {}
        for action in actions:
            self.by_stem.setdefault(_nfc(action.stem), []).append(action)
        self.actions_dir = actions_dir
        self.names: list[tuple[str, str, ActionNote]] = []  # (kind, casefolded name, action)
        for action in actions:
            if len(_nfc(action.stem)) >= min_chars:
                self.names.append(("file", _nfc(action.stem), action))
            for kind, value in (("title", action.title), *(("alias", alias) for alias in action.aliases)):
                name = _nfc(value)
                if len(name) >= min_chars and name != _nfc(action.stem):
                    self.names.append((kind, name, action))

    def by_touch(self, path: str) -> ActionNote | None:
        """An Action from a Vault-relative path found in a tool call (normalized text)."""
        if not path.startswith(self.actions_dir + "/"):
            return None
        exact = self.by_path.get(_nfc(path))
        if exact is not None:
            return exact
        stem = path.rsplit("/", 1)[-1][: -len(".md")]
        matches = self.by_stem.get(_nfc(stem), [])
        return matches[0] if len(matches) == 1 else None


def _window(config: AppConfig, as_of: date | None, days: int) -> tuple[date, date, datetime]:
    zone = timezone(timedelta(minutes=config.utc_offset_minutes))
    end = as_of or datetime.now(zone).date()
    start = end - timedelta(days=days - 1)
    return start, end, files_since(config, start)


def files_since(config: AppConfig, start: date) -> datetime:
    """The oldest modification time of a conversation file worth reading for local dates from `start`.

    One extra day: a file's modification time can precede its last message's local date.
    """
    zone = timezone(timedelta(minutes=config.utc_offset_minutes))
    return datetime.combine(start - timedelta(days=1), time(), zone)


def check_work_roots(config: AppConfig, paths: PathText) -> None:
    """Refuse work folders that overlap a folder this command only reads (an application's folder, an
    archive or the Vault), as written and as the file system resolves them (`..`, junctions and
    symbolic links of the existing part of a path)."""
    vault = config.vault.root
    protected: list[Path] = [vault] if vault else []
    # Every configured location counts, whatever the mode and even when the entry is disabled.
    for settings in config.chat_sources.values():
        protected.append(settings.app_root.expanduser())
        if settings.archive is not None:
            protected.append(settings.archive)
    work = [("processes.data_root", config.processes.data_root), ("processes.state_root", config.processes.state_root)]
    if config.digests.cache:
        work.append(("the digest cache (cache_root/digests)", digest_cache_root(config.cache_root)))
    for kind, root in work:
        for folder in protected:
            for left, right in ((str(root), str(folder)), (str(root.resolve()), str(folder.resolve()))):
                if paths.is_within(left, right) or paths.is_within(right, left):
                    raise PipelineError(f"{kind} must not overlap an application's folder, an archive or "
                                        f"the Vault: {root}")


PASTED_TAG = re.compile(r"</?pasted_content\b[^>]*>", re.IGNORECASE)


def _first_line(text: str, limit: int) -> str:
    single = " ".join(PASTED_TAG.sub(" ", text).split())
    return single if len(single) <= limit else single[: limit - 1] + "…"


def _clue_c(conversation: Conversation, index: ActionIndex) -> dict[str, list[dict[str, Any]]]:
    """Actions named in the person's requests: WikiLinks, file names, titles and aliases."""
    found: dict[str, list[dict[str, Any]]] = {}
    requests = conversation.session["userRequests"]
    texts = [(request, _nfc(request["text"])) for request in requests]
    joined = "\n".join(text for _request, text in texts)
    for request, text in texts:
        for target in WIKILINK.findall(text):
            name = target.strip().rsplit("/", 1)[-1]
            name = name[:-3] if name.endswith(".md") else name
            for action in index.by_stem.get(_nfc(name), []):
                found.setdefault(action.key(), []).append(
                    {"clue": "C", "via": "wikilink", "evidenceId": request["evidenceId"]})
    for kind, name, action in index.names:
        if name not in joined:
            continue
        request = next(request for request, text in texts if name in text)
        items = found.setdefault(action.key(), [])
        if not any(item["via"] in {"wikilink", kind} for item in items):
            items.append({"clue": "C", "via": kind, "evidenceId": request["evidenceId"]})
    return found


def _clue_d(
    conversation: Conversation, targets: list[ActionNote], paths: PathText, vault_root: str,
    *, title_needs_identifier: bool = False,
) -> list[ActionNote]:
    session = conversation.session
    repo, cwd = session.get("repo") or "", session.get("cwd") or ""
    if not repo or (vault_root and paths.is_within(cwd, vault_root)):
        return []
    token = _token(repo)
    declared = [a for a in targets if any(_nfc(value) == _nfc(repo) for value in a.repositories)]
    if declared:
        return declared
    # Among the many finished Actions, a plain word such as "windows" would match unrelated titles.
    if title_needs_identifier and not re.search(r"[_-]", repo):
        return []
    return [a for a in targets if not a.repositories and token.search(_nfc(a.title))]


def _command_only(items: list[dict[str, Any]]) -> bool:
    """Only A clues that name the file in a command's arguments, without reading or changing it."""
    return all(item["clue"] == "A" and item["via"] == "command" for item in items)


@dataclass
class ConversationLoad:
    """The main conversations changed since a time, with their subagents, and how they were read."""

    conversations: dict[tuple[str, str, str], Conversation]
    sources: list[dict[str, Any]]
    skipped: list[dict[str, Any]]
    digest_status: Counter[str]
    read_from: Counter[str]
    unreadable: list[dict[str, str]]
    kinds: Counter[str]
    duplicates: int
    orphan_subagents: int

    def on_dates(self, dates: set[str]) -> list[Conversation]:
        """Conversations with a message on one of the local dates (ISO), the latest first."""
        selected = [c for c in self.conversations.values() if dates.intersection(c.session["localDates"])]
        selected.sort(key=lambda c: c.session.get("lastLocalTime") or "", reverse=True)
        return selected


def digest_context(config: AppConfig, paths: PathText) -> DigestContext:
    vault_root = paths.folder(str(config.vault.root)) if config.vault.root else ""
    return DigestContext(config.utc_offset_minutes, paths, vault_root, _nfc(config.vault.actions.dir),
                         tuple(tuple(group) for group in config.path_aliases))


def load_conversations(config: AppConfig, context: DigestContext, since: datetime) -> ConversationLoad:
    """Digest the conversation files of every chat source changed since `since` and group them:
    one entry per main conversation (the longest file when a session has several), subagents
    attached to their parent."""
    scan = open_sources(config)
    store = DigestStore(context, config.cache_root if config.digests.cache else None)
    digests: list[dict[str, Any]] = []
    digest_status: Counter[str] = Counter()
    read_from: Counter[str] = Counter()
    unreadable: list[dict[str, str]] = []
    for source in scan.sources:
        files = conversation_files(source, modified_since=since)
        LOGGER.info("Chat source %s (%s, %s): %s conversation files changed since %s",
                    source.name, source.app, source.settings.mode, len(files), since.date().isoformat())
        for log in files:
            digest, status = store.load(source, log)
            digest_status[status.split(":", 1)[0]] += 1
            if digest is None:
                if status.startswith("unreadable"):
                    unreadable.append({"source": log.source, "location": log.location,
                                       "relativePath": log.relative_path, "detail": status})
                continue
            read_from[log.location] += 1
            if digest.get("session") is not None:
                digests.append(digest)
    conversations: dict[tuple[str, str, str], Conversation] = {}
    subagents: list[dict[str, Any]] = []
    kinds: Counter[str] = Counter()
    duplicates = 0
    for digest in digests:
        session = digest["session"]
        kinds[session["kind"]] += 1
        ref = session["sessionRef"]
        session_key = (ref["app"], ref["sourceId"], ref["sessionId"].casefold())
        if session["kind"] == "subagent":
            subagents.append(digest)
            continue
        if session["kind"] != "main":
            continue
        if session_key in conversations:
            duplicates += 1
            if digest["file"]["lineCount"] <= conversations[session_key].digest["file"]["lineCount"]:
                continue
        conversations[session_key] = Conversation(session_key, digest)
    def parent_of(digest: dict[str, Any]) -> tuple[str, str, str]:
        session = digest["session"]
        ref = session["sessionRef"]
        return ref["app"], ref["sourceId"], (session.get("parentSessionId") or "").casefold()

    # A subagent can run after its parent's file was last written: read those parents whatever
    # their modification time, so the subagent's work is not lost.
    missing = {parent_of(digest) for digest in subagents if parent_of(digest) not in conversations}
    for source in scan.sources:
        wanted = {key[2] for key in missing if key[:2] == (source.app, source.source_id) and key[2]}
        if not wanted:
            continue
        for log in conversation_files(source):
            name = conversation_key(source.app, log.relative_path)
            if log.kind == "subagent" or not any(session_id in name for session_id in wanted):
                continue
            digest, status = store.load(source, log)
            if digest is None or digest.get("session") is None:
                continue
            digest_status[status.split(":", 1)[0]] += 1
            read_from[log.location] += 1
            session = digest["session"]
            ref = session["sessionRef"]
            session_key = (ref["app"], ref["sourceId"], ref["sessionId"].casefold())
            if session["kind"] == "main" and session_key in missing and (
                    session_key not in conversations
                    or digest["file"]["lineCount"] > conversations[session_key].digest["file"]["lineCount"]):
                kinds[session["kind"]] += 1
                conversations[session_key] = Conversation(session_key, digest)
    orphan_subagents = 0
    for digest in subagents:
        parent = parent_of(digest)
        if parent in conversations:
            conversations[parent].subagents.append(digest)
        else:
            orphan_subagents += 1
    return ConversationLoad(conversations, [source.describe() for source in scan.sources], scan.skipped,
                            digest_status, read_from, unreadable, kinds, duplicates, orphan_subagents)


@dataclass
class LinkResult:
    rows: list[dict[str, Any]]
    targets: list[ActionNote]
    clue_counts: Counter[str]
    decisions: Counter[str]

    def by_conversation(self) -> dict[tuple[str, str, str], dict[str, Any]]:
        return {(row["app"], row["sourceId"], row["sessionId"].casefold()): row for row in self.rows}


def _existed(action: ActionNote, last: date) -> bool:
    """The Action had been created or started by the conversation's last date.

    An Action written down after its work began says so with an earlier `started`; with neither
    date, it is assumed to have existed.
    """
    known = [day for day in (action.created, action.started) if day is not None]
    return not known or min(known) <= last


def _closed_before(action: ActionNote, first: date) -> bool:
    """Closed now and, as far as `completed` tells, already before the conversation's first date."""
    return action.closed and (action.completed is None or action.completed < first)


def link_conversations(actions: list[ActionNote], index: ActionIndex, selected: list[Conversation],
                       window_start: datetime, paths: PathText, vault_root: str,
                       *, dates_of: Callable[[Conversation], list[str]] | None = None) -> LinkResult:
    """Decide which Actions each conversation belongs to (design 4.2, clues A-D only).

    With `dates_of` (a conversation's local dates, oldest first), clue D and the follow-up check
    take the Actions as they were on those dates: an Action created and started after the last date
    is left out, and an Action closed now but completed on or after the first date was still in
    progress.
    Without it, as for `actions link`, they take the Actions as they are now.
    """
    touched_keys: set[str] = set()
    for conversation in selected:
        for touch in conversation.touches():
            if touch["access"] == "command":
                continue  # Naming a file in a command's arguments does not make its Action a target.
            action = index.by_touch(touch["path"])
            if action is not None:
                touched_keys.add(action.key())
    targets = [a for a in actions if not a.closed and (
        a.in_progress or (a.updated is not None and a.updated >= window_start) or a.key() in touched_keys)]
    target_keys = {a.key() for a in targets}
    finished = [a for a in actions if a.closed]
    by_key = {a.key(): a for a in actions}
    rows: list[dict[str, Any]] = []
    clue_counts: Counter[str] = Counter()
    decisions: Counter[str] = Counter()
    for conversation in selected:
        session = conversation.session
        clues: dict[str, list[dict[str, Any]]] = {}
        for touch in conversation.touches():
            action = index.by_touch(touch["path"])
            if action is not None:
                clues.setdefault(action.key(), []).append(
                    {"clue": "A", "via": touch["access"], "evidenceId": touch["evidenceId"],
                     "subagent": bool(touch.get("subagent") or touch.get("sidechain"))})
        session_id = session["sessionRef"]["sessionId"].casefold()
        for action in actions:
            if session_id in action.notes_session_ids:
                clues.setdefault(action.key(), []).append({"clue": "B", "via": "notes-evidence"})
        for key, items in _clue_c(conversation, index).items():
            clues.setdefault(key, []).extend(items)
        days = dates_of(conversation) if dates_of is not None else []
        if days:
            first, last = date.fromisoformat(days[0]), date.fromisoformat(days[-1])
            then = [a for a in actions if _existed(a, last) and (
                a.key() in target_keys or (a.closed and not _closed_before(a, first)))]
            then_keys = {a.key() for a in then}
            then_finished = [a for a in finished if _existed(a, last) and _closed_before(a, first)]
        else:
            then, then_keys, then_finished = targets, target_keys, finished
        d_candidates = _clue_d(conversation, then, paths, vault_root)
        links: list[dict[str, Any]] = []
        weak: dict[str, dict[str, Any]] = {}
        for key, items in clues.items():
            action = by_key[key]
            link: dict[str, Any] = {"action": action.relative_path, "title": action.title, "status": action.status,
                    "target": key in then_keys, "strength": "strong",
                    "clues": sorted({item["clue"] for item in items}), "details": items}
            if _command_only(items):
                weak[key] = {**link, "strength": "weak"}
            else:
                links.append(link)
        finished_candidates: list[ActionNote] = []
        if links:
            decision = "linked"
        elif len(d_candidates) == 1:
            decision = "linked"
            action = d_candidates[0]
            details: list[dict[str, Any]] = [{"clue": "D", "via": "repositories" if action.repositories
                                              else "title", "repo": session["repo"]}]
            supporting = weak.pop(action.key(), None)  # A command naming the same Action backs the match.
            links.append({"action": action.relative_path, "title": action.title, "status": action.status,
                          "target": True, "strength": "medium", "clues": ["A", "D"] if supporting else ["D"],
                          "details": (supporting["details"] if supporting else []) + details})
        elif d_candidates:
            decision = "ambiguous"
        else:
            finished_candidates = _clue_d(conversation, then_finished, paths, vault_root, title_needs_identifier=True)
            decision = "followup" if finished_candidates else "unlinked"
        decisions[decision] += 1
        for link in links:
            clue_counts.update(link["clues"])
        requests = session["userRequests"]
        rows.append({
            "app": session["sessionRef"]["app"], "sourceId": session["sessionRef"]["sourceId"],
            "sessionId": session["sessionRef"]["sessionId"], "title": session["title"],
            "cwd": session["cwd"], "repo": session["repo"],
            "firstLocalTime": session["firstLocalTime"], "lastLocalTime": session["lastLocalTime"],
            "humanRequestCount": len(requests), "excludedUserMessages": session["excludedUserMessages"],
            "mediumConfidenceRequests": sum(1 for r in requests if r["confidence"] != "high"),
            "toolEventCount": len(session["toolEvents"]) + sum(len(s["session"]["toolEvents"])
                                                                for s in conversation.subagents),
            "subagentCount": len(conversation.subagents),
            "firstRequest": _first_line(requests[0]["text"], 100) if requests else "",
            "decision": decision, "links": links, "weakLinks": list(weak.values()),
            "dCandidates": [{"action": a.relative_path, "title": a.title} for a in d_candidates]
            if decision == "ambiguous" else [],
            "finishedActions": [{"action": a.relative_path, "title": a.title, "status": a.status}
                                for a in finished_candidates],
            "digest": {"source": conversation.digest["location"]["source"],
                       "location": conversation.digest["location"]["location"],
                       "relativePath": conversation.digest["file"]["relativePath"],
                       "contentSha256": conversation.digest["file"]["contentSha256"]},
        })
    return LinkResult(rows, targets, clue_counts, decisions)


def load_link_actions(config: AppConfig, paths: PathText) -> tuple[list[ActionNote], list[str], int, ActionIndex]:
    """Action notes that can be linked, the reading issues, the notes without status and the index."""
    vault = config.vault
    actions, action_issues = load_actions(vault, paths, config.utc_offset_minutes)
    without_status = sum(1 for a in actions if not a.status)
    if vault.actions.require_status:
        actions = [a for a in actions if a.status]
    index = ActionIndex(actions, _nfc(vault.actions.dir), vault.actions.min_name_chars)
    return actions, action_issues, without_status, index


def link_actions(config: AppConfig, *, dry_run: bool, as_of: date | None = None, days: int | None = None,
                 progress: Any = None) -> dict[str, Any]:
    if not dry_run:
        raise PipelineError("actions link writes nothing yet; run it with --dry-run (stage 0)")
    paths = PathText(config.path_aliases)
    check_work_roots(config, paths)
    window_days = days or config.processes.actions.lookback_days
    start, end, since = _window(config, as_of, window_days)
    actions, action_issues, without_status, index = load_link_actions(config, paths)
    context = digest_context(config, paths)
    run_id = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z") + "-" + uuid4().hex[:6]
    loaded = load_conversations(config, context, since)
    window = {day.isoformat() for day in (start + timedelta(days=n) for n in range(window_days))}
    selected = loaded.on_dates(window)
    zone = timezone(timedelta(minutes=config.utc_offset_minutes))
    window_start = datetime.combine(start, time(), zone)
    linked = link_conversations(actions, index, selected, window_start, paths, context.vault_root)
    rows, targets, decisions = linked.rows, linked.targets, linked.decisions
    linked_actions: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        for link in row["links"]:
            linked_actions.setdefault(link["action"], []).append(
                {"sessionId": row["sessionId"], "app": row["app"], "clues": link["clues"], "title": row["title"]})
    summary = {
        "schemaVersion": REPORT_SCHEMA_VERSION,
        "runId": run_id,
        "dryRun": True,
        "asOf": end.isoformat(),
        "windowStart": start.isoformat(),
        "lookbackDays": window_days,
        "vaultWritten": False,
        "generativeAiUsed": False,
        "sources": loaded.sources,
        "skippedSources": loaded.skipped,
        "digests": dict(loaded.digest_status),
        "digestCache": config.digests.cache,
        "readFrom": {location: loaded.read_from.get(location, 0) for location in ("app", "archive")},
        "unreadableFiles": loaded.unreadable,
        "sessionKinds": dict(loaded.kinds),
        "duplicateSessions": loaded.duplicates,
        "orphanSubagents": loaded.orphan_subagents,
        "conversationCount": len(rows),
        "decisions": {key: decisions.get(key, 0) for key in DECISIONS},
        "conversationsWithWeakLinks": sum(1 for r in rows if r["weakLinks"]),
        "linkedStrong": sum(1 for r in rows if any(link["strength"] == "strong" for link in r["links"])),
        "linkedMediumOnly": sum(1 for r in rows if r["links"] and all(link["strength"] == "medium"
                                                                         for link in r["links"])),
        "clueCounts": dict(sorted(linked.clue_counts.items())),
        "actionCount": len(actions),
        "notesWithoutStatus": without_status,
        "targetActionCount": len(targets),
        "linkedActionCount": len(linked_actions),
        "actionIssues": action_issues,
        "conversationsWithoutHumanRequests": sum(1 for r in rows if not r["humanRequestCount"]),
    }
    state = config.processes.state_root / "actions-link" / run_id
    report = {**summary, "conversations": rows, "actions": linked_actions}
    atomic_write_json(state / "report.json", report)
    atomic_write_text(state / "links.jsonl", "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    atomic_write_text(state / "report.md", render_markdown(report, targets))
    atomic_write_json(config.processes.state_root / "actions-link" / "last-run.json",
                      {"runId": run_id, "reportPath": str(state / "report.md")})
    return {**summary, "ok": True, "complete": True, "reportPath": str(state / "report.md"),
            "reportJsonPath": str(state / "report.json"), "linksPath": str(state / "links.jsonl")}


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _stem(path: str) -> str:
    return Path(path).stem


def render_markdown(report: dict[str, Any], targets: list[ActionNote]) -> str:
    """Japanese report for the person; the JSON next to it is the machine-readable form."""
    decisions = report["decisions"]
    lines = [
        f"# actions link --dry-run（{report['windowStart']}〜{report['asOf']}、{report['lookbackDays']}日）",
        "",
        f"実行 `{report['runId']}`。Vaultには書いていない。生成AIは使っていない。手がかりはA〜Dだけ。",
        "",
        "## 1. 集計",
        "",
        "| 項目 | 件数 |",
        "| --- | --- |",
        f"| 期間内の会話（main） | {report['conversationCount']} |",
        f"| 対応づいた（A・B・Cのどれか） | {report['linkedStrong']} |",
        f"| 対応づいた（Dが1件だけ） | {report['linkedMediumOnly']} |",
        f"| 決まらない（Dが複数。段階1で生成AIに選ばせる） | {decisions['ambiguous']} |",
        f"| 終了したActionの続き（段階1で新しいActionの候補にする） | {decisions['followup']} |",
        f"| 対応なし | {decisions['unlinked']} |",
        f"| A(実行)だけの弱い手がかりがあった会話（対応にしない） | {report['conversationsWithWeakLinks']} |",
        f"| うち本人の発言のない会話 | {report['conversationsWithoutHumanRequests']} |",
        f"| 手がかりの件数（A・B・C・D） | {', '.join(f'{k} {v}' for k, v in report['clueCounts'].items()) or '0'} |",
        f"| Action（全体・対象・対応づいた） | {report['actionCount']}・{report['targetActionCount']}・"
        f"{report['linkedActionCount']} |",
        f"| 会話の要点（作った・再利用・読めない） | {report['digests'].get('built', 0)}・"
        f"{report['digests'].get('cached', 0)}・{report['digests'].get('unreadable', 0)} |",
        f"| 読んだ場所（アプリの保存場所・アーカイブ） | {report['readFrom']['app']}・"
        f"{report['readFrom']['archive']} |",
        "",
        "対象のAction：statusが作業中、期間内に更新日時が変わった、または期間内の会話が開いた・変えたもの（終了したものを除く）。"
        "A・B・Cは対象の外のActionにも対応づける（表の「対象外」）。",
        "",
        "## 2. 会話ごと（新しい順）",
        "",
        "| 最後の発言 | アプリ | タイトル | リポジトリ | 判定 | Action（手がかり） | 最初の依頼 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    labels = {"linked": "対応", "ambiguous": "曖昧", "followup": "続き", "unlinked": "なし"}
    access_labels = {"read": "読", "edit": "変", "command": "実行"}

    def clue_text(link: dict[str, Any]) -> str:
        parts = []
        for clue in link["clues"]:
            if clue == "A":
                accesses = sorted({access_labels.get(item["via"], item["via"])
                                   for item in link["details"] if item["clue"] == "A"})
                parts.append(f"A({'・'.join(accesses)})")
            else:
                parts.append(clue)
        return "・".join(parts)

    for row in report["conversations"]:
        actions = "<br>".join(
            f"[[{_stem(link['action'])}]]（{clue_text(link)}"
            f"{'' if link['target'] else '、対象外'}{'、中' if link['strength'] == 'medium' else ''}）"
            for link in row["links"]
        )
        if row["decision"] == "ambiguous":
            actions = "候補：" + "<br>".join(f"[[{_stem(c['action'])}]]" for c in row["dCandidates"][:5])
        if row["decision"] == "followup":
            actions = "終了：" + "<br>".join(f"[[{_stem(a['action'])}]]（{a['status']}）"
                                           for a in row["finishedActions"][:3])
        weak = "<br>".join(f"弱い：[[{_stem(link['action'])}]]（A(実行)）" for link in row["weakLinks"])
        actions = "<br>".join(part for part in (actions, weak) if part)
        when = (row["lastLocalTime"] or "")[:16].replace("T", " ")
        lines.append(
            f"| {when} | {row['app']} | {_cell(row['title'] or row['sessionId'])} | {_cell(row['repo'])} | "
            f"{labels[row['decision']]} | {actions} | {_cell(row['firstRequest'])} |"
        )
    lines += ["", "## 3. Actionごと", "", "| Action | status | 会話 | 手がかり |", "| --- | --- | --- | --- |"]
    status_by_path = {a.relative_path: a.status for a in targets}
    for path, items in sorted(report["actions"].items(), key=lambda item: -len(item[1])):
        clue_set = sorted({clue for item in items for clue in item["clues"]})
        lines.append(f"| [[{_stem(path)}]] | {status_by_path.get(path, '（対象外）')} | {len(items)} | "
                     f"{'・'.join(clue_set)} |")
    unlinked_targets = [a for a in targets if a.relative_path not in report["actions"] and a.in_progress]
    lines += ["", f"作業中で、期間内の会話と対応づかなかったAction：{len(unlinked_targets)}件。", ""]
    lines += [
        "## 4. 読み方と限界",
        "",
        "- A：会話中にAIがActionのファイルを開いた・変えた（サブエージェントを含む。読＝読む・探す、変＝編集、"
        "実行＝コマンドの引数に現れた）。"
        "B：ActionのNotesの根拠にこの会話のセッションIDがある。"
        "C：本人の発言にActionのファイル名・WikiLink・title・aliasesがある。"
        "D：作業フォルダの最後のフォルダ名が、対象のActionの `repositories`（なければtitle）に1件だけ一致する。",
        "- A(実行)だけの手がかりは弱い。そのActionにほかの手がかりがあるときだけ対応にし、なければ「弱い」と示す。"
        "検索・一覧のコマンドで出たファイルは「読」に数える。",
        "- 続き：どのActionにも対応しない会話で、作業フォルダの名前が終了したActionの `repositories`"
        "（なければtitle。titleで比べるのは `_` か `-` を含む名前だけ）に一致したもの。"
        "段階1では、そのActionを再開する案ではなく、新しいActionの候補にする。",
        "- 会話は `chat_sources` の読む場所から読む（`app`：アプリの保存場所、`archive`：アーカイブだけ、"
        "`auto`：アプリの保存場所を先に、ない会話はアーカイブから）。`archive` だけの取得元では、"
        "archiverがまだ取り込んでいない会話（使用中など）は入らない。"
        "`app` だけの取得元では、アプリが消した会話は入らない。",
        "- 本人の発言：Claude Codeは `turnOrigin` が `human` 以外の依頼文（別のセッションからの依頼など）を除く。"
        "古い記録でこの欄がないものは本人の発言として扱い、確度を1段下げる。",
        "- 詳細はこのフォルダの `report.json` と `links.jsonl`（会話ごとの手がかりと根拠ID）にある。",
        "",
    ]
    return "\n".join(lines)
