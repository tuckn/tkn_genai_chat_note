"""Read Action notes straight from the Vault's Markdown (design 3.6); never writes.

Frontmatter keys, statuses, the Notes heading and the record-line format come from
`vault.actions` settings, so another Vault layout only needs another configuration.
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from .digests import UUID
from .path_text import PathText
from .process_settings import VaultSettings
from .runtime import PipelineError

HEADING = re.compile(r"^(#{1,6})\s+")


@dataclass
class ActionNote:
    path: Path
    relative_path: str  # relative to vault.root, `/` separated, as written in the Vault
    stem: str
    id: str | None
    title: str
    status: str
    updated: datetime | None
    aliases: list[str]
    repositories: list[str]
    in_progress: bool
    closed: bool
    created: date | None = None  # local dates; None when the Frontmatter does not say
    started: date | None = None
    completed: date | None = None
    notes_session_ids: set[str] = field(default_factory=set)
    record_count: int = 0
    issues: list[str] = field(default_factory=list)

    def key(self) -> str:
        return normalize_name(self.relative_path)


def normalize_name(value: str) -> str:
    return unicodedata.normalize("NFC", value).replace("\\", "/").casefold()


def _frontmatter(text: str) -> tuple[dict[str, Any], str, str | None]:
    if not text.startswith("---"):
        return {}, text, None
    match = re.match(r"\A---\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", text, re.DOTALL)
    if not match:
        return {}, text, "frontmatter has no closing delimiter"
    try:
        value = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return {}, text[match.end():], "frontmatter is not valid YAML"
    return (value if isinstance(value, dict) else {}), text[match.end():], None


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(item) for item in value if isinstance(item, str | int | float) and str(item).strip()]
    return []


def _datetime(value: Any, utc_offset_minutes: int) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime(value.year, value.month, value.day)
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone(timedelta(minutes=utc_offset_minutes)))
    return parsed


def _local_date(value: Any, utc_offset_minutes: int) -> date | None:
    parsed = _datetime(value, utc_offset_minutes)
    return parsed.astimezone(timezone(timedelta(minutes=utc_offset_minutes))).date() if parsed else None


def notes_lines(body: str, heading: re.Pattern[str]) -> list[str]:
    """Lines of the Notes section: from its heading to the next heading of the same or higher level."""
    lines = body.splitlines()
    result: list[str] = []
    level: int | None = None
    for line in lines:
        match = HEADING.match(line)
        if level is None:
            if match and heading.search(line):
                level = len(match.group(1))
            continue
        if match and len(match.group(1)) <= level:
            break
        result.append(line)
    return result


def cited_sessions(lines: list[str], record: re.Pattern[str], same: list[str]) -> tuple[set[str], int]:
    """Session IDs cited as evidence by record lines; `同上` (configurable) repeats the previous evidence."""
    found: set[str] = set()
    previous: set[str] = set()
    count = 0
    for line in lines:
        match = record.match(line)
        if not match:
            continue
        count += 1
        evidence = (match.group("evidence") or "").strip()
        ids = {value.casefold() for value in UUID.findall(evidence)}
        if not ids and any(evidence.startswith(marker) for marker in same):
            ids = previous
        found.update(ids)
        previous = ids
    return found, count


def load_actions(vault: VaultSettings, paths: PathText, utc_offset_minutes: int) -> tuple[list[ActionNote], list[str]]:
    if vault.root is None:
        raise PipelineError("vault.root is not configured; set the Vault folder and vault.actions.dir")
    actions_root = vault.actions_root
    assert actions_root is not None
    if not actions_root.is_dir():
        raise PipelineError(f"Action folder not found: {actions_root} (vault.root + vault.actions.dir)")
    settings = vault.actions
    keys = settings.frontmatter
    heading = re.compile(settings.sections.notes)
    record = re.compile(settings.record_line)
    in_progress = {value.casefold() for value in settings.statuses.in_progress}
    closed = {value.casefold() for value in settings.statuses.closed}
    notes: list[ActionNote] = []
    issues: list[str] = []
    for folder, directories, files in os.walk(actions_root):
        directories[:] = sorted(name for name in directories if not name.startswith("."))
        for name in sorted(files):
            if not name.casefold().endswith(".md"):
                continue
            path = Path(folder) / name
            try:
                text = path.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeDecodeError) as exc:
                issues.append(f"{path}: {type(exc).__name__}")
                continue
            metadata, body, problem = _frontmatter(text)

            def value(key: str | None, metadata: dict[str, Any] = metadata) -> Any:
                return metadata.get(key) if key else None

            status = str(value(keys.status) or "").strip()
            stem = path.stem
            relative = path.relative_to(vault.root).as_posix()
            session_ids, count = cited_sessions(notes_lines(body, heading), record, settings.same_evidence)
            note = ActionNote(
                path=path,
                relative_path=relative,
                stem=stem,
                id=str(value(keys.id)) if value(keys.id) else None,
                title=str(value(keys.title) or stem).strip(),
                status=status,
                updated=_datetime(value(keys.updated), utc_offset_minutes),
                aliases=_strings(value(keys.aliases)),
                repositories=_strings(value(keys.repositories)),
                in_progress=status.casefold() in in_progress,
                closed=status.casefold() in closed,
                created=_local_date(value(keys.created), utc_offset_minutes),
                started=_local_date(value(keys.started), utc_offset_minutes),
                completed=_local_date(value(keys.completed), utc_offset_minutes),
                notes_session_ids=session_ids,
                record_count=count,
            )
            if problem:
                note.issues.append(problem)
                issues.append(f"{relative}: {problem}")
            notes.append(note)
    return notes, issues
