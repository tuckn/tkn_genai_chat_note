"""`ai-dailies build`: the AI's diary of a local date, made without generative AI (design 5, stage 2a).

For each date the diary lists the day's Codex and Claude Code conversations (title, working
folder, linked Actions) and the counts of design 5.3; the other sections stay empty until
stage 2b. The counts are kept below `processes.data_root/ai-dailies/metrics/`.

The note goes to `vault.ai_dailies` only for a finished day. A new note is created; a note still
exactly as this command last wrote it is rebuilt; a note the person changed is not rebuilt, and
conversations it does not name yet are appended under `## 追加（<日時>）`. A note changed in the
last 30 minutes is left for the next run, and the note is read again right before it is
replaced. `--dry-run` writes previews and a report below `processes.state_root` only.
"""

from __future__ import annotations

import codecs
import hashlib
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import __version__
from .action_links import (
    Conversation,
    ConversationLoad,
    LinkResult,
    check_work_roots,
    digest_context,
    files_since,
    link_conversations,
    load_conversations,
    load_link_actions,
)
from .ai_daily_metrics import activity_dates, day_metrics, score, start_date, start_metrics, totals, work_dates
from .config import AppConfig
from .digests import PASTED_BLOCK, DigestContext, digest_cache_root
from .locks import exclusive_lock
from .path_text import PathText
from .runtime import PipelineError, atomic_write_bytes, atomic_write_json
from .safety import redact_secret_like_content

PROCESSOR = "tkn-genai-chat-note"
METRICS_SCHEMA_VERSION = "1.0.0"
STATE_SCHEMA_VERSION = "1.0.0"
REPORT_SCHEMA_VERSION = "1.0.0"
QUIET_MINUTES = 30
MAX_ITEMS = 30
MAX_ACTIONS = 3
TOP_CONVERSATIONS = 3
TITLE_CHARS = 60
APP_NAMES = {"codex": "Codex", "claude-code": "Claude Code"}
SECTIONS = ("今日やったこと", "うまくいったこと", "うまくいかなかったこと", "本人から学んだこと", "持ち越し")
FAILURE_LABELS = {"not-found": "ファイルがない", "permission": "権限", "encoding": "文字コード",
                  "timeout": "時間切れ", "too-large": "大きすぎる", "other": "その他"}
EXTRA_SECRETS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}"),
)
PLAIN = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")
UPDATED_LINE = re.compile(r"^updated:[^\r\n]*", re.MULTILINE)


def redact(text: str) -> str:
    value = redact_secret_like_content(text)
    for pattern in EXTRA_SECRETS:
        value = pattern.sub("[REDACTED]", value)
    return value


def _one_line(text: str, limit: int) -> str:
    single = " ".join(PASTED_BLOCK.sub(" ", text).replace("[[", "[ [").replace("]]", "] ]").split())
    single = redact(single)
    return single if len(single) <= limit else single[: limit - 1] + "…"


def _hhmm(value: str | None) -> str:
    return value[11:16] if value and len(value) >= 16 else "--:--"


def _stem(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    return name[:-3] if name.casefold().endswith(".md") else name


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class DiaryItem:
    """One conversation as the diary of one date sees it."""

    conversation: Conversation
    row: dict[str, Any] | None
    day: dict[str, Any]
    start: dict[str, Any] | None

    @property
    def app(self) -> str:
        return str(self.conversation.session["sessionRef"]["app"])

    @property
    def session_id(self) -> str:
        return str(self.conversation.session["sessionRef"]["sessionId"])

    @property
    def evidence(self) -> str:
        return f"{APP_NAMES.get(self.app, self.app)} {self.session_id}"

    def actions(self) -> list[str]:
        return [_stem(link["action"]) for link in (self.row or {}).get("links", [])]

    def title(self) -> str:
        session = self.conversation.session
        if session.get("title"):
            return _one_line(str(session["title"]), TITLE_CHARS)
        requests = session["userRequests"]
        if requests:
            return _one_line(requests[0]["text"], TITLE_CHARS)
        return "（codex exec）" if session.get("originator") == "codex_exec" else "（タイトルなし）"

    def line(self) -> str:
        session = self.conversation.session
        folder = f"作業フォルダ {session['repo']}" if session.get("repo") else "作業フォルダ不明"
        requests = self.day["humanRequests"]
        said = f"本人の発言 {requests}件" if requests else "本人の発言なし"
        actions = [f"[[{name}]]" for name in self.actions()]
        if len(actions) > MAX_ACTIONS:
            actions = [*actions[:MAX_ACTIONS], f"ほか{len(actions) - MAX_ACTIONS}件"]
        refs = "、".join([*actions, self.evidence])
        when = f"{_hhmm(self.day['firstLocalTime'])}〜{_hhmm(self.day['lastLocalTime'])}"
        return f"- {when} {APP_NAMES.get(self.app, self.app)}「{self.title()}」：{folder}、{said}（{refs}）"

    def record(self) -> dict[str, Any]:
        session = self.conversation.session
        return {
            "sessionRef": session["sessionRef"], "title": session.get("title"), "cwd": session["cwd"],
            "repo": session["repo"], "decision": (self.row or {}).get("decision"),
            "actions": [link["action"] for link in (self.row or {}).get("links", [])],
            "subagentFiles": len(self.conversation.subagents),
            "digest": {"relativePath": self.conversation.digest["file"]["relativePath"],
                       "contentSha256": self.conversation.digest["file"]["contentSha256"]},
            "day": self.day, "start": self.start,
        }


def diary_items(conversations: list[Conversation], day: date, links: LinkResult,
                context: DigestContext) -> tuple[list[DiaryItem], list[dict[str, Any]]]:
    """The day's conversations, and those that only had slash commands or lines the application
    wrote that day (no request, no answer, no tool execution), which the diary leaves out."""
    key = day.isoformat()
    rows = links.by_conversation()
    items = []
    idle = []
    for conversation in conversations:
        dates = work_dates(conversation, context)
        if key not in dates:
            if key in activity_dates(conversation, context):
                idle.append(conversation.session["sessionRef"])
            continue
        row = rows.get(conversation.key)
        started = start_date(conversation, context) == key
        items.append(DiaryItem(conversation, row, day_metrics(conversation, key, context),
                               start_metrics(conversation, row, context) if started else None))
    items.sort(key=lambda item: (item.day["firstLocalTime"] or "", item.session_id))
    return items, idle


def _trouble_lines(items: list[DiaryItem], summary: dict[str, Any]) -> list[str]:
    failures = summary["readFailures"]
    failure_text = f"読み込みの失敗 {sum(failures.values())}件"
    if failures:
        failure_text += "（" + "、".join(f"{FAILURE_LABELS.get(kind, kind)} {count}件"
                                       for kind, count in sorted(failures.items(), key=lambda x: -x[1])) + "）"
    lines = [
        f"- 資料探し：読む・探す・一覧 {summary['lookups']}回。{failure_text}、"
        f"同じ検索の繰り返し {summary['repeatedSearches']}件、3回以上読んだファイル {summary['rereadFiles']}件、"
        f"広すぎる走査 {summary['broadScans']}件",
        f"- 本人の拒否 {summary['rejections']}件。本人の訂正らしい発言 {summary['correctionCandidates']}件"
        "（語句で拾った候補で、確かめていない）",
    ]
    rules = summary["actionRules"]
    if rules["startedWithWork"]:
        lines.append(f"- Actionの規則：この日に始まり作業（commit・ファイルの作成など）をした会話 "
                     f"{rules['startedWithWork']}件のうち、作業の前にActionを読んだ {rules['readActionBeforeWork']}件、"
                     f"ActionのNotesにこの会話を根拠にした行がある {rules['recordedInNotes']}件")
    else:
        lines.append("- Actionの規則：この日に始まり作業（commit・ファイルの作成など）をした会話はない")
    ranked = sorted((item for item in items if score(item.day)), key=lambda item: -score(item.day))
    for item in ranked[:TOP_CONVERSATIONS]:
        day = item.day
        parts = [f"{label} {count}件" for label, count in (
            ("読み込みの失敗", sum(day["readFailures"].values())), ("同じ検索の繰り返し", day["repeatedSearches"]),
            ("3回以上読んだファイル", len(day["rereadFiles"])), ("広すぎる走査", len(day["broadScans"])),
            ("本人の拒否", day["rejections"])) if count]
        lines.append(f"- {APP_NAMES.get(item.app, item.app)}「{item.title()}」：{'、'.join(parts)}（{item.evidence}）")
    return lines


def _yaml(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


@dataclass(frozen=True)
class NoteIdentity:
    note_id: str
    created: str
    updated: str


def render_note(config: AppConfig, day: date, items: list[DiaryItem], summary: dict[str, Any],
                daily_stem: str | None, identity: NoteIdentity) -> str:
    settings = config.vault.ai_dailies
    actions = list(dict.fromkeys(name for item in items for name in item.actions()))
    apps = "・".join(APP_NAMES[app] for app in APP_NAMES if any(item.app == app for item in items))
    title = _stem(settings.filename.format(date=day))
    description = (f"{day.isoformat()}の{apps}の会話 {len(items)}件の一覧と、つまずきの数え上げ。"
                   "生成AIを使わずに作ったため、うまくいったこと・本人から学んだこと・持ち越しはまだ書いていない")
    note_type = settings.type if PLAIN.fullmatch(settings.type) else _yaml(settings.type)
    lines = ["---", f"type: {note_type}", f"schemaVersion: {_yaml(settings.schema_version)}",
             f"title: {_yaml(title)}", f"description: {_yaml(description)}"]
    lines += ["actions:", *(f"  - {_yaml(f'[[{name}]]')}" for name in actions)] if actions else ["actions: []"]
    lines += [f"generator: {_yaml(PROCESSOR)}", "reviewStatus: unreviewed",
              f"created: {_yaml(identity.created)}", f"updated: {_yaml(identity.updated)}",
              f"noteId: {_yaml(identity.note_id)}", "---", ""]
    if daily_stem:
        lines += [f"本人のDaily：[[{daily_stem}]]", ""]
    done = [item.line() for item in items[:MAX_ITEMS]]
    if len(items) > MAX_ITEMS:
        done.append(f"- ほか {len(items) - MAX_ITEMS}件（数え上げのJSONにある）")
    bodies = {"今日やったこと": done, "うまくいかなかったこと": _trouble_lines(items, summary)}
    for section in SECTIONS:
        lines += [f"## {section}", ""]
        if bodies.get(section):
            lines += [*bodies[section], ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def _frontmatter_value(text: str, key: str) -> str | None:
    match = re.match(r"\A---\r?\n(.*?)\r?\n---", text, re.DOTALL)
    if not match:
        return None
    found = re.search(rf"^{re.escape(key)}:\s*(.*)$", match.group(1), re.MULTILINE)
    return found.group(1).strip().strip("'\"") if found else None


def append_text(existing: bytes, items: list[DiaryItem], when: datetime) -> bytes | None:
    """The person's note with the conversations it does not name yet appended; None when there are none.

    Only the `updated` line of the Frontmatter changes; the BOM and the line breaks stay as they were.
    """
    bom = existing.startswith(codecs.BOM_UTF8)
    text = existing.decode("utf-8-sig", errors="strict")
    known = text.casefold()
    new = [item for item in items if item.session_id.casefold() not in known]
    if not new:
        return None
    newline = "\r\n" if "\r\n" in text else "\n"
    block = ["", f"## 追加（{when:%Y-%m-%d %H:%M}）", "", *(item.line() for item in new)]
    result = text.rstrip("\r\n") + newline + newline.join(block) + newline
    head = re.match(r"\A---\r?\n.*?\r?\n---", result, re.DOTALL)
    if head and UPDATED_LINE.search(head.group(0)):
        replaced = UPDATED_LINE.sub(f"updated: {_yaml(when.isoformat(timespec='seconds'))}", head.group(0), count=1)
        result = replaced + result[head.end():]
    return (codecs.BOM_UTF8 if bom else b"") + result.encode("utf-8")


@dataclass
class NotePlan:
    action: str  # create, rebuild, append, unchanged, kept, deferred
    content: bytes | None
    seen: bytes | None  # what the note held when planned; None when it did not exist
    identity: NoteIdentity | None = None
    reason: str = ""


def plan_note(path: Path, day: str, record: dict[str, Any] | None, render: Callable[[NoteIdentity], str],
              items: list[DiaryItem], now: datetime, *, incomplete: str = "") -> NotePlan:
    """What to do with the diary note of one date (design 5.5).

    `incomplete` names the conversations or locations this run could not read; a note is then not
    rebuilt, since the rebuilt note could lose what the earlier one listed.
    """
    stamp = now.isoformat(timespec="seconds")
    existing = _read(path)
    if record and record.get("date", day) != day:
        return NotePlan("kept", None, existing, reason=f"this command wrote the note for {record['date']}; "
                                                       "not replaced")
    if existing is None:
        if record:
            return NotePlan("kept", None, None, reason="deleted or moved by the person; not written again")
        identity = NoteIdentity(str(uuid4()), stamp, stamp)
        return NotePlan("create", render(identity).encode("utf-8"), None, identity)
    if record and record.get("mode") == "built" and record.get("sha256") == _sha256(existing):
        kept = NoteIdentity(record["noteId"], record["created"], record["updated"])
        content = render(kept).encode("utf-8")
        if content == existing:
            return NotePlan("unchanged", None, existing, kept)
        if incomplete:
            return NotePlan("deferred", None, existing, reason=f"not rebuilt while {incomplete}")
        # Still exactly what this command wrote, so nobody is writing it now: no wait.
        identity = NoteIdentity(kept.note_id, kept.created, stamp)
        return NotePlan("rebuild", render(identity).encode("utf-8"), existing, identity)
    try:
        appended = append_text(existing, items, now)
    except UnicodeDecodeError:
        return NotePlan("kept", None, existing, reason="the note is not UTF-8; left as it is")
    if appended is None:
        return NotePlan("kept", None, existing, reason="changed by the person; no conversation to add")
    modified = datetime.fromtimestamp(path.stat().st_mtime, now.tzinfo)
    if now - modified < timedelta(minutes=QUIET_MINUTES):
        return NotePlan("deferred", None, existing, reason=f"changed in the last {QUIET_MINUTES} minutes")
    text = appended.decode("utf-8-sig")
    identity = NoteIdentity(_frontmatter_value(text, "noteId") or "", _frontmatter_value(text, "created") or "",
                            stamp)
    return NotePlan("append", appended, existing, identity,
                    reason="changed by the person or not written by this command; conversations appended")


def _read(path: Path) -> bytes | None:
    return path.read_bytes() if path.is_file() else None


class NoteState:
    """What this command last wrote to each diary note: decides between rebuilding and appending."""

    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            value = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        except (OSError, ValueError):
            value = {}
        notes = value.get("notes") if isinstance(value, dict) else None
        self.notes: dict[str, dict[str, Any]] = notes if isinstance(notes, dict) else {}

    def save(self) -> None:
        atomic_write_json(self.path, {"schemaVersion": STATE_SCHEMA_VERSION, "notes": self.notes})


def _target(config: AppConfig, day: date) -> tuple[Path, str]:
    vault_root = config.vault.root
    assert vault_root is not None
    relative = config.vault.ai_dailies.relative_path(day)
    path = vault_root.joinpath(*relative.split("/"))
    if not path.resolve().is_relative_to(vault_root.resolve()):
        raise PipelineError(f"vault.ai_dailies leaves vault.root: {path}")
    return path, relative


def _daily_stem(config: AppConfig, day: date) -> str | None:
    vault_root = config.vault.root
    assert vault_root is not None
    relative = config.vault.dailies.relative_path(day)
    return _stem(relative) if vault_root.joinpath(*relative.split("/")).is_file() else None


def _dates(end: date | None, days: int, now: datetime, dry_run: bool) -> list[date]:
    today = now.date()
    last = end or today - timedelta(days=1)
    if last > today:
        raise PipelineError(f"--date {last.isoformat()} is in the future")
    if last >= today and not dry_run:
        raise PipelineError(f"{last.isoformat()} is not over yet; write it tomorrow, or look with --dry-run")
    return [last - timedelta(days=offset) for offset in range(days - 1, -1, -1)]


def build_ai_dailies(config: AppConfig, *, dry_run: bool, end: date | None = None, days: int = 1,
                  now: datetime | None = None) -> dict[str, Any]:
    if config.vault.root is None:
        raise PipelineError("vault.root is not configured; ai-dailies build writes to vault.ai_dailies")
    if not 1 <= days <= 366:
        raise PipelineError("--days must be between 1 and 366")
    zone = timezone(timedelta(minutes=config.utc_offset_minutes))
    current = (now or datetime.now(zone)).astimezone(zone)
    dates = _dates(end, days, current, dry_run)
    paths = PathText(config.path_aliases)
    check_work_roots(config, paths)
    # --dry-run writes the state root and the digest cache, never processes.data_root.
    data = config.processes.data_root
    others = [("processes.state_root", config.processes.state_root)]
    if config.digests.cache:
        others.append(("the digest cache (cache_root/digests)", digest_cache_root(config.cache_root)))
    for name, other in others:
        for left, right in ((str(other), str(data)), (str(other.resolve()), str(data.resolve()))):
            if paths.is_within(left, right) or paths.is_within(right, left):
                raise PipelineError(f"{name} must not overlap processes.data_root: {other}, {data}")
    targets = [config.vault.ai_dailies.relative_path(day).casefold() for day in dates]
    if len(set(targets)) != len(targets):
        raise PipelineError("vault.ai_dailies names the same note for two of the dates; use the full date")
    state_root = config.processes.state_root / "ai-dailies"
    with exclusive_lock(state_root / ".lock", f"another ai-dailies build is running: {state_root}", create_parent=True):
        return _build(config, dates, current, paths, dry_run=dry_run)


def _build(config: AppConfig, dates: list[date], now: datetime, paths: PathText, *, dry_run: bool) -> dict[str, Any]:
    actions, action_issues, _without_status, index = load_link_actions(config, paths)
    context = digest_context(config, paths)
    loaded = load_conversations(config, context, files_since(config, dates[0]))
    wanted = {day.isoformat() for day in dates}
    selected = [c for c in loaded.conversations.values() if wanted.intersection(activity_dates(c, context))]
    window_start = datetime.combine(dates[0], datetime.min.time(), now.tzinfo)
    links = link_conversations(actions, index, selected, window_start, paths, context.vault_root)
    run_id = now.strftime("%Y%m%dT%H%M%S%z") + "-" + uuid4().hex[:6]
    state_root = config.processes.state_root / "ai-dailies"
    run_root = state_root / run_id
    note_state = NoteState(state_root / "notes.json")
    incomplete = incomplete_inputs(loaded)
    results: list[dict[str, Any]] = []
    for day in dates:
        items, idle = diary_items(selected, day, links, context)
        if not items:
            empty: dict[str, Any] = {"date": day.isoformat(), "note": "no-conversations", "conversations": 0,
                                     "withoutAiWork": len(idle)}
            path, relative = _target(config, day)
            if relative in note_state.notes and path.is_file():
                # An earlier version wrote a note for conversations that no longer count; notes are
                # never deleted, so the person decides.
                empty.update(note="stale", notePath=str(path), vaultPath=relative,
                             reason="no conversation with AI work counts for this date any more; "
                                    "the note written earlier is left as it is")
            results.append(empty)
            continue
        results.append(_build_day(config, day, items, now, note_state, run_root, dry_run=dry_run,
                                  incomplete=incomplete, idle=idle))
    counts: dict[str, int] = {}
    for item in results:
        counts[item["note"]] = counts.get(item["note"], 0) + 1
    deferred = sum(1 for item in results if item["note"] == "deferred")
    report = {
        "schemaVersion": REPORT_SCHEMA_VERSION, "runId": run_id, "dryRun": dry_run,
        "dates": [day.isoformat() for day in dates], "generativeAiUsed": False, "vaultWritten": not dry_run and any(
            item["note"] in {"created", "rebuilt", "appended"} for item in results),
        "notes": counts, "days": results, "sources": loaded.sources, "skippedSources": loaded.skipped,
        "digests": dict(loaded.digest_status),
        "readFrom": {location: loaded.read_from.get(location, 0) for location in ("app", "archive")},
        "unreadableFiles": loaded.unreadable, "incompleteInputs": incomplete,
        "orphanSubagents": loaded.orphan_subagents, "actionCount": len(actions), "actionIssues": action_issues,
    }
    atomic_write_json(run_root / "report.json", report)
    atomic_write_json(state_root / "last-run.json", {"runId": run_id, "reportPath": str(run_root / "report.json")})
    return {**report, "ok": True, "complete": deferred == 0, "reportPath": str(run_root / "report.json")}


def incomplete_inputs(loaded: ConversationLoad) -> str:
    """What this run could not read; empty when every configured location and file was read."""
    reasons = []
    if loaded.unreadable:
        reasons.append(f"{len(loaded.unreadable)} conversation files could not be read")
    skipped = [item["source"] for item in loaded.skipped if item.get("reason") != "disabled"]
    if skipped:
        reasons.append("chat sources could not be read: " + ", ".join(skipped))
    partial = [source["name"] for source in loaded.sources if source.get("notes")]
    if partial:
        reasons.append("a location of these chat sources could not be read: " + ", ".join(partial))
    return "; ".join(reasons)


def _build_day(config: AppConfig, day: date, items: list[DiaryItem], now: datetime, note_state: NoteState,
               run_root: Path, *, dry_run: bool, incomplete: str = "",
               idle: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    summary = totals([{"day": item.day, "start": item.start} for item in items])
    path, relative = _target(config, day)
    daily_stem = _daily_stem(config, day)

    def render(identity: NoteIdentity) -> str:
        return render_note(config, day, items, summary, daily_stem, identity)

    def plan() -> NotePlan:
        return plan_note(path, day.isoformat(), note_state.notes.get(relative), render, items, now,
                         incomplete=incomplete)

    planned = plan()
    result: dict[str, Any] = {"date": day.isoformat(), "conversations": len(items), "notePath": str(path),
                              "vaultPath": relative, "withoutAiWork": len(idle or [])}
    metrics_value = {
        "schemaVersion": METRICS_SCHEMA_VERSION, "date": day.isoformat(), "application": __version__,
        "generatedAt": now.isoformat(timespec="seconds"), "note": relative, "incompleteInputs": incomplete,
        "totals": summary, "conversations": [item.record() for item in items],
        # Conversations with only slash commands or lines the application wrote that day.
        "withoutAiWork": idle or [],
    }
    if dry_run:
        if planned.content is not None:
            preview = run_root.joinpath("notes", *relative.split("/"))
            atomic_write_bytes(preview, planned.content)
            result["previewPath"] = str(preview)
        metrics = run_root / "metrics" / f"{day.isoformat()}.json"
        atomic_write_json(metrics, metrics_value)
    else:
        planned = _write(path, planned, plan)
        if planned.action in {"create", "rebuild", "append"} and planned.content is not None and planned.identity:
            note_state.notes[relative] = {
                "date": day.isoformat(), "mode": "built" if planned.action in {"create", "rebuild"} else "appended",
                "sha256": _sha256(planned.content), "noteId": planned.identity.note_id,
                "created": planned.identity.created, "updated": planned.identity.updated,
                "sessions": [item.session_id for item in items], "writtenAt": now.isoformat(timespec="seconds"),
            }
            note_state.save()
        metrics = config.processes.data_root / "ai-dailies" / "metrics" / f"{day:%Y}" / f"{day.isoformat()}.json"
        if incomplete and metrics.is_file():
            result["metricsKept"] = f"not replaced while {incomplete}"
        else:
            atomic_write_json(metrics, metrics_value)
    result["metricsPath"] = str(metrics)
    written = {"create": "created", "rebuild": "rebuilt", "append": "appended"}
    if planned.action in written:
        result["note"] = f"would-{planned.action}" if dry_run else written[planned.action]
    else:
        result["note"] = planned.action
    if planned.reason:
        result["reason"] = planned.reason
    return result


def _replace_if_unchanged(path: Path, content: bytes, seen: bytes | None) -> str:
    """Put `content` at `path` only while the note still holds `seen` (None: it does not exist).

    `written`, `changed` (the note is not what was planned on) or `busy` (another program holds it
    open on Windows). The note is compared right before the replacement, and a busy note is not
    retried: a retry after a delay could replace what the person saved meanwhile.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".tmp-{uuid4().hex[:12]}"
    try:
        temporary.write_bytes(content)
        if seen is None:
            try:
                os.link(temporary, path)  # Creates the note; never replaces one made meanwhile.
            except FileExistsError:
                return "changed"
            except OSError:  # A file system without hard links.
                try:
                    with path.open("xb") as handle:
                        handle.write(content)
                except FileExistsError:
                    return "changed"
            return "written"
        if _read(path) != seen:
            return "changed"
        try:
            os.replace(temporary, path)
        except PermissionError:
            return "busy"
        return "written"
    finally:
        temporary.unlink(missing_ok=True)


def _write(path: Path, plan: NotePlan, replan: Callable[[], NotePlan]) -> NotePlan:
    """Write a planned note after reading it again (design 4.8 5); plan once more when it changed."""
    for attempt in range(2):
        if plan.content is None:
            return plan
        outcome = _replace_if_unchanged(path, plan.content, plan.seen)
        if outcome == "written":
            return plan
        if outcome == "busy":
            return NotePlan("deferred", None, _read(path), reason="the note is open in another program")
        if attempt == 0:
            plan = replan()
    return NotePlan("deferred", None, _read(path), reason="the note changed while this run wrote it")
