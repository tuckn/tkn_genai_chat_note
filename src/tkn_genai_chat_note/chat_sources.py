"""Find the conversation logs to read: an application's own folder, a tkn-genai-chat-archiver
archive, or both (design 3.1).

Each `chat_sources` entry names one application of one source and how to read it:

- `app` reads the application's folder (for example `~/.codex` or `~/.claude`);
- `archive` reads only the archiver's archive through `ArchiveView`: finished manifests, the
  latest version of each relative path, stored and decoded SHA-256 checked;
- `auto` reads the application's folder first and takes the conversations it no longer has
  (deleted, or renamed by compression or archiving) from the archive. A conversation found in
  both is read from the application's folder, which may hold newer turns.

Both locations are only read: nothing is created or changed below them. The log formats
belong to tkn_genai_chat_search (`library`) and the archive format to tkn_genai_chat_archiver.
"""

from __future__ import annotations

import gzip
import hashlib
import logging
import os
import zlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import zstandard
from tkn_genai_chat_archiver.view import ArchiveError, ArchiveVersion, ArchiveView
from tkn_genai_chat_search.library import (
    Title,
    claude_code_transcript_kind,
    codex_rollout_status,
    read_codex_titles,
)

from .config import AppConfig
from .process_settings import ChatSourceSettings
from .runtime import PipelineError

LOGGER = logging.getLogger(__name__)
CODEX_FOLDERS = ("sessions", "archived_sessions")
CODEX_TITLE_INDEX = "session_index.jsonl"
CLAUDE_CODE_FOLDER = "projects"
# Folders of a Claude Code project folder that hold tool output or memory, not transcripts.
# Only the walk skips them; `claude_code_transcript_kind` decides what is a transcript.
CLAUDE_CODE_SKIPPED = frozenset({"memory", "tool-results", "mcp-tasks"})
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
GZIP_MAGIC = b"\x1f\x8b"
LONG_PATH = 240  # Windows paths this long are opened with the `\\?\` prefix.


class SourceReadError(PipelineError):
    """A file of an application's folder cannot be read or decoded."""


@dataclass(frozen=True)
class LogFile:
    """One conversation file and where it is read from."""

    source: str  # The chat_sources entry name.
    app: str
    source_id: str
    relative_path: str  # POSIX path below the application's folder; the archive keeps the same paths.
    kind: str  # main, subagent (Claude Code file layout) or rollout (Codex; the content decides)
    location: str  # app or archive
    modified_at: datetime | None
    os_path: str | None = None  # app: the file to open
    version: ArchiveVersion | None = None  # archive: the version to read

    @property
    def known_content_sha256(self) -> str | None:
        """The decoded content's SHA-256 when it is known without reading (an archive manifest records it)."""
        return self.version.content_sha256 if self.version is not None else None

    def location_record(self) -> dict[str, Any]:
        if self.version is None:
            return {"source": self.source, "location": "app"}
        return {"source": self.source, "location": "archive", "storedSha256": self.version.sha256,
                "capturedAt": self.version.captured_at, "previousRelation": self.version.previous_relation}


@dataclass
class OpenSource:
    """A chat source with the locations that could be opened."""

    name: str
    settings: ChatSourceSettings
    app_root: Path | None = None
    view: ArchiveView | None = None
    codex_titles: dict[str, list[Title]] = field(default_factory=dict)
    notes: list[dict[str, str]] = field(default_factory=list)

    @property
    def app(self) -> str:
        return self.settings.app

    @property
    def source_id(self) -> str:
        return self.settings.source_id

    def read(self, log: LogFile) -> tuple[bytes, str]:
        """The decoded content of a file and its SHA-256. Archived bytes are checked against the manifest."""
        if log.version is not None:
            if self.view is None:
                raise ArchiveError(f"archive of {self.name} is not open")
            data = self.view.read_content(log.version)
            return data, log.version.content_sha256 or hashlib.sha256(data).hexdigest()
        if log.os_path is None:
            raise SourceReadError(f"no file to read: {log.relative_path}")
        data = read_app_file(log.os_path)
        return data, hashlib.sha256(data).hexdigest()

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name, "app": self.app, "sourceId": self.source_id, "mode": self.settings.mode,
            "read": [location for location, opened in (("app", self.app_root is not None),
                                                       ("archive", self.view is not None)) if opened],
            "ignoredManifests": len(self.view.ignored_manifests) if self.view is not None else None,
            "notes": self.notes,
        }


@dataclass
class SourceScan:
    sources: list[OpenSource] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)


def os_path(path: Path | str) -> str:
    """The path to give the operating system; long Windows paths get the `\\\\?\\` prefix."""
    text = str(path)
    if os.name != "nt" or len(text) < LONG_PATH or text.startswith("\\\\?\\"):
        return text
    absolute = os.path.abspath(text)
    return "\\\\?\\UNC\\" + absolute[2:] if absolute.startswith("\\\\") else "\\\\?\\" + absolute


def _decode_zstd(data: bytes) -> bytes:
    """Every concatenated frame, as the archiver decodes them; a frame that does not end is an error."""
    parts: list[bytes] = []
    remaining = data
    decompressor = zstandard.ZstdDecompressor()
    while remaining:
        frame = decompressor.decompressobj()
        parts.append(frame.decompress(remaining))
        if not frame.eof:
            raise EOFError("incomplete zstd frame (truncated data)")
        remaining = frame.unused_data
    return b"".join(parts)


def read_app_file(path: str) -> bytes:
    """Read a file of an application's folder (never written) and expand zstd or gzip content."""
    try:
        with open(path, "rb") as handle:
            data = handle.read()
        if data.startswith(ZSTD_MAGIC):
            return _decode_zstd(data)
        if data.startswith(GZIP_MAGIC):
            return gzip.decompress(data)
    except (OSError, EOFError, zlib.error, zstandard.ZstdError) as exc:
        raise SourceReadError(f"cannot read {path}: {exc}") from exc
    return data


def _is_dir(path: Path) -> bool:
    try:
        return os.path.isdir(os_path(path))
    except OSError:
        return False


def open_sources(config: AppConfig) -> SourceScan:
    """Open every enabled chat source. A location that cannot be read is reported, not fatal."""
    if not config.chat_sources:
        raise PipelineError("no chat_sources are configured; name each application's folder or archive "
                            "under chat_sources")
    scan = SourceScan()
    for name, settings in config.chat_sources.items():
        base = {"source": name, "app": settings.app, "sourceId": settings.source_id, "mode": settings.mode}
        if not settings.enabled:
            scan.skipped.append({**base, "reason": "disabled"})
            continue
        source = OpenSource(name, settings)
        if settings.reads_archive and settings.archive is not None:
            try:
                # The marker must belong to the same application and source, or evidence IDs would mix.
                source.view = ArchiveView(settings.archive, profile=settings.app, source_id=settings.source_id)
            except ArchiveError as exc:
                LOGGER.warning("Chat source %s: the archive is not readable: %s", name, exc)
                source.notes.append({"location": "archive", "reason": "unreadable", "detail": str(exc)})
        if settings.reads_app:
            root = settings.app_root.expanduser()
            if _is_dir(root):
                source.app_root = root
            else:
                LOGGER.warning("Chat source %s: the application folder is not found: %s", name, root)
                source.notes.append({"location": "app", "reason": "folder-missing", "detail": str(root)})
        if source.app_root is None and source.view is None:
            scan.skipped.append({**base, "reason": "unreadable", "notes": source.notes})
            continue
        if source.view is not None and source.view.ignored_manifests:
            LOGGER.info("Chat source %s: %s unfinished archive manifests ignored", name,
                        len(source.view.ignored_manifests))
        if settings.app == "codex":
            source.codex_titles = _codex_titles(source)
        scan.sources.append(source)
    return scan


def _codex_titles(source: OpenSource) -> dict[str, list[Title]]:
    """Thread names from `session_index.jsonl`; the application's copy wins over the archived one."""
    titles: dict[str, list[Title]] = {}
    if source.view is not None:
        index = source.view.latest(CODEX_TITLE_INDEX)
        if index is not None:
            try:
                titles.update(read_codex_titles(source.view.read_content(index))[0])
            except ArchiveError as exc:
                LOGGER.warning("Chat source %s: cannot read archived session titles: %s", source.name, exc)
    if source.app_root is not None:
        path = os_path(source.app_root / CODEX_TITLE_INDEX)
        if os.path.isfile(path):
            try:
                titles.update(read_codex_titles(read_app_file(path))[0])
            except SourceReadError as exc:
                LOGGER.warning("Chat source %s: cannot read session titles: %s", source.name, exc)
    return titles


def _walk(root: Path, top: str, prune: Callable[[list[str], list[str]], list[str]],
          unlisted: Callable[[OSError], None] | None = None) -> Iterator[tuple[str, str]]:
    """(relative POSIX path, path to open) of each file below `root/top`; links are not followed.
    A folder that cannot be listed is logged and passed to `unlisted`."""
    base = os_path(root / top)
    if not os.path.isdir(base):
        return

    def on_error(exc: OSError) -> None:
        LOGGER.warning("Cannot list folder %s: %s", exc.filename, exc.strerror or type(exc).__name__)
        if unlisted is not None:
            unlisted(exc)

    for current, directories, filenames in os.walk(base, onerror=on_error, followlinks=False):
        relative = os.path.relpath(current, base)
        parts = [] if relative == "." else relative.replace("\\", "/").split("/")
        directories[:] = prune(parts, sorted(directories))
        for name in sorted(filenames):
            yield "/".join([top, *parts, name]), os.path.join(current, name)


def _claude_code_prune(parts: list[str], directories: list[str]) -> list[str]:
    # projects/<project>/<session>.jsonl and projects/<project>/<session>/subagents/agent-*.jsonl
    if len(parts) == 1:
        return [name for name in directories if name not in CLAUDE_CODE_SKIPPED]
    if len(parts) == 2:
        return [name for name in directories if name == "subagents"]
    return directories if not parts else []


def _modified(path: str) -> datetime | None:
    try:
        return datetime.fromtimestamp(os.stat(path).st_mtime, UTC)
    except OSError:
        return None


def _app_files(source: OpenSource) -> list[LogFile]:
    root = source.app_root
    if root is None:
        return []
    def unlisted(exc: OSError) -> None:
        # The conversations below that folder are missing from this run; readers must know.
        note = {"location": "app", "reason": "folder-unlisted",
                "detail": f"{exc.filename}: {exc.strerror or type(exc).__name__}"}
        if note not in source.notes:
            source.notes.append(note)

    found: list[tuple[str, str]] = []
    if source.app == "codex":
        for top in CODEX_FOLDERS:
            found.extend(_walk(root, top, lambda _parts, directories: directories, unlisted))
    else:
        found.extend(_walk(root, CLAUDE_CODE_FOLDER, _claude_code_prune, unlisted))
    files = []
    for relative_path, path in found:
        kind = _kind(source.app, relative_path)
        if kind is not None:
            files.append(LogFile(source.name, source.app, source.source_id, relative_path, kind, "app",
                                 _modified(path), os_path=path))
    return files


def _kind(app: str, relative_path: str) -> str | None:
    if app == "codex":
        return "rollout" if codex_rollout_status(relative_path) else None
    return claude_code_transcript_kind(relative_path)


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.astimezone()


def _archive_files(source: OpenSource) -> list[LogFile]:
    view = source.view
    if view is None:
        return []
    files: list[LogFile] = []
    prefixes = tuple(f"{top}/" for top in CODEX_FOLDERS) if source.app == "codex" else (f"{CLAUDE_CODE_FOLDER}/",)
    for prefix in prefixes:
        for relative_path in view.relative_paths(prefix):
            kind = _kind(source.app, relative_path)
            version = view.latest(relative_path) if kind else None
            if version is None or kind is None:
                continue
            stat = version.record.get("sourceStat")
            modified = _parse(stat.get("modifiedAt") if isinstance(stat, dict) else None) or _parse(
                version.captured_at)
            files.append(LogFile(source.name, source.app, source.source_id, relative_path, kind, "archive",
                                 modified, version=version))
    # The earliest captured copy first, so a same-content `.jsonl` / `.jsonl.zst` pair keeps it.
    return sorted(files, key=lambda f: (f.version.captured_at if f.version else "",
                                        f.version.run_id if f.version else "", f.relative_path))


def conversation_key(app: str, relative_path: str) -> str:
    """One conversation file across locations. Codex may compress (`.zst`) or move (`archived_sessions/`)
    a rollout but keeps its file name. A Claude Code transcript is named by its session (and agent)
    below the project folder, which changes when the working folder is renamed or moved."""
    parts = PurePosixPath(relative_path.replace("\\", "/")).parts
    if app == "codex":
        name = parts[-1].casefold() if parts else ""
        return name[: -len(".zst")] if name.endswith(".zst") else name
    return "/".join(parts[2:]).casefold() if len(parts) > 2 else relative_path.casefold()


def conversation_files(source: OpenSource, *, modified_since: datetime | None = None) -> list[LogFile]:
    """The files to read, optionally only those changed since a time. In `auto` mode a conversation
    the application's folder has is not read from the archive."""
    app_files = _app_files(source)
    archive_files = _archive_files(source)
    if app_files:
        present = {conversation_key(source.app, item.relative_path) for item in app_files}
        archive_files = [item for item in archive_files
                         if conversation_key(source.app, item.relative_path) not in present]
    files = sorted(app_files, key=lambda f: f.relative_path) + archive_files
    if modified_since is None:
        return files
    return [item for item in files if item.modified_at is None or item.modified_at >= modified_since]
