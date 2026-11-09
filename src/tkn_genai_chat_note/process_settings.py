"""Settings of the note processes: where conversations are read, the Vault's conventions and work folders.

Everything that differs between users (folders, file names, Frontmatter keys, statuses,
headings and the record-line format) is configured here. The defaults are generic; a user's
actual values belong in their own config file, never in this repository.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from string import Formatter
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# The record-line formats of an Action's Notes (design 4.3). The current form sits below a
# `### YYYY-MM-DD` heading and names the kind in upper case: `- DECIDED (user): <conclusion>。根拠: <evidence>`
# (kinds DECIDED, DONE, CHECKED and the earlier PROPOSED). The earlier form starts with the date and
# writes the kind and actor in Japanese: `- YYYY-MM-DD 決定（本人）：<conclusion>。根拠：<evidence>`.
# Both are read; a line without a date counts only with one of those upper-case kinds.
DEFAULT_RECORD_LINE = (
    r"^\s*[-*]\s+(?:(?P<date>\d{4}-\d{2}-\d{2})\s+|(?=(?:DECIDED|DONE|CHECKED|PROPOSED)\s*[（(]))"
    r"(?P<kind>[^（(：:\s]+)\s*[（(](?P<actor>[^）)]*)[）)]\s*[：:]\s*"
    r"(?P<conclusion>.*?)(?:[。.]?\s*根拠[：:]\s*(?P<evidence>.*))?$"
)
CHAT_APPS = ("codex", "claude-code")
# The application's own folder below the home folder, used when `root` is omitted.
DEFAULT_APP_FOLDERS = {"codex": ".codex", "claude-code": ".claude"}


def default_app_root() -> Path:
    from .config import default_app_root as app_root

    return app_root()


class ChatSourceSettings(BaseModel):
    """Where one application's conversations of one source are read (design 3.1).

    `app` reads the application's folder, `archive` only a tkn-genai-chat-archiver archive, and
    `auto` the folder first and the archive for conversations the folder no longer has. Both
    locations are read only.
    """

    model_config = ConfigDict(extra="forbid")

    app: Literal["codex", "claude-code"]
    source_id: str
    mode: Literal["app", "archive", "auto"] = "app"
    root: Path | None = None  # The application's folder; default ~/.codex or ~/.claude.
    archive: Path | None = None  # The archive root; required by `archive` and `auto`.
    enabled: bool = True

    @field_validator("source_id")
    @classmethod
    def safe_source_id(cls, value: str) -> str:
        from .config import validate_source_id

        return validate_source_id(value)

    @field_validator("root", "archive", mode="before")
    @classmethod
    def nonempty_path(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            raise ValueError("chat source paths must not be empty")
        return value

    @model_validator(mode="after")
    def archive_for_mode(self) -> Self:
        if self.mode in ("archive", "auto") and self.archive is None:
            raise ValueError(f"mode {self.mode} needs archive (a tkn-genai-chat-archiver archive root)")
        return self

    @property
    def reads_app(self) -> bool:
        return self.mode in ("app", "auto")

    @property
    def reads_archive(self) -> bool:
        return self.mode in ("archive", "auto")

    @property
    def app_root(self) -> Path:
        return self.root if self.root is not None else Path.home() / DEFAULT_APP_FOLDERS[self.app]


class DigestSettings(BaseModel):
    """Session digests are a cache below `<cache_root>/digests/`; `cache: false` rebuilds them every run."""

    model_config = ConfigDict(extra="forbid")

    cache: bool = True


class ActionsLinkSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lookback_days: int = Field(default=14, ge=1, le=366)


class ProcessSettings(BaseModel):
    """Work folders of the note processes; independent of the per-source review stores."""

    model_config = ConfigDict(extra="forbid")

    data_root: Path = Field(default_factory=lambda: default_app_root() / "processes" / "data")
    state_root: Path = Field(default_factory=lambda: default_app_root() / "processes" / "state")
    actions: ActionsLinkSettings = Field(default_factory=ActionsLinkSettings)


class ActionFrontmatter(BaseModel):
    """Frontmatter keys of an Action note; null means the Vault does not use that item."""

    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    title: str | None = "title"
    status: str | None = "status"
    updated: str | None = "updated"
    aliases: str | None = "aliases"
    repositories: str | None = "repositories"


class ActionStatuses(BaseModel):
    model_config = ConfigDict(extra="forbid")

    in_progress: list[str] = Field(default_factory=lambda: ["InProgress"])
    not_started: list[str] = Field(default_factory=lambda: ["NotStarted"])
    closed: list[str] = Field(default_factory=lambda: ["Completed", "Done", "Closed"])


class ActionSections(BaseModel):
    """Regular expressions that find section headings (matched against each heading line)."""

    model_config = ConfigDict(extra="forbid")

    notes: str = r"^#{2,6}\s+(?:\d+\.\s*)?Notes\b"

    @field_validator("notes")
    @classmethod
    def compiles(cls, value: str) -> str:
        re.compile(value)
        return value


class ActionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dir: str = "actions"
    frontmatter: ActionFrontmatter = Field(default_factory=ActionFrontmatter)
    statuses: ActionStatuses = Field(default_factory=ActionStatuses)
    sections: ActionSections = Field(default_factory=ActionSections)
    # Must define the named group `evidence`; other groups (date, kind, actor, conclusion) are reported.
    record_line: str = DEFAULT_RECORD_LINE
    # Evidence text that means "the same evidence as the previous line".
    same_evidence: list[str] = Field(default_factory=lambda: ["同上"])
    # Shortest title, alias or file name that counts as a mention in a request (clue C).
    min_name_chars: int = Field(default=8, ge=2, le=200)
    # Notes in the folder without a status value (an inbox or index) are not linked.
    require_status: bool = True

    @field_validator("dir")
    @classmethod
    def relative_folder(cls, value: str) -> str:
        text = value.replace("\\", "/").strip("/")
        if not text or ".." in text.split("/") or re.match(r"^[A-Za-z]:", text):
            raise ValueError("vault.actions.dir must be a folder relative to vault.root")
        return text

    @field_validator("record_line")
    @classmethod
    def has_evidence_group(cls, value: str) -> str:
        if "evidence" not in re.compile(value).groupindex:
            raise ValueError("vault.actions.record_line must define the named group 'evidence'")
        return value


SAMPLE_DATE = date(2026, 1, 2)


def _date_template(value: str, *, folder: bool) -> str:
    """A folder or file name with `{date:<strftime>}` fields only, e.g. `dailies/{date:%Y}`."""
    text = value.replace("\\", "/").strip("/") if folder else value
    for _literal, field_name, _spec, conversion in Formatter().parse(text):
        if field_name is not None and (field_name != "date" or conversion):
            raise ValueError(f"only {{date:<format>}} fields are allowed: {value}")
    rendered = text.format(date=SAMPLE_DATE)
    parts = rendered.split("/")
    if not rendered or ".." in parts or re.match(r"^[A-Za-z]:", rendered) or any(not part for part in parts):
        raise ValueError(f"must be a path relative to vault.root: {value}")
    if not folder and ("/" in rendered or not rendered.casefold().endswith(".md")):
        raise ValueError(f"must be a Markdown file name without folders: {value}")
    if any(char in rendered for char in '<>:"|?*'):
        raise ValueError(f"contains a character Windows does not allow in names: {value}")
    return text


class DatedNoteLocation(BaseModel):
    """Where the note of one date lives: `dir` and `filename` take `{date:<strftime>}` fields."""

    model_config = ConfigDict(extra="forbid")

    dir: str
    filename: str

    @field_validator("dir")
    @classmethod
    def relative_folder(cls, value: str) -> str:
        return _date_template(value, folder=True)

    @field_validator("filename")
    @classmethod
    def markdown_name(cls, value: str) -> str:
        return _date_template(value, folder=False)

    @model_validator(mode="after")
    def one_note_per_date(self) -> Self:
        # Every date of two years, a leap year included, must name its own note; this rejects
        # formats without the day, the month or the year, and fields such as %w or %a alone.
        first = date(2027, 1, 1)
        names = {self.relative_path(date.fromordinal(first.toordinal() + offset)).casefold()
                 for offset in range(731)}
        if len(names) != 731:
            raise ValueError("dir and filename must name a different note for every date "
                             "(use the year, the month and the day, e.g. {date:%Y%m%d})")
        return self

    def relative_path(self, day: date) -> str:
        return f"{self.dir.format(date=day)}/{self.filename.format(date=day)}"


class DailySettings(DatedNoteLocation):
    """The person's Daily (one note per date); only linked from the AI's diary, never written."""

    dir: str = "dailies/{date:%Y}"
    filename: str = "{date:%Y%m%d}.md"


class AiDailySettings(DatedNoteLocation):
    """The AI's diary (design 5.4): one note per local date, written by `ai-dailies build`."""

    dir: str = "ai-dailies/{date:%Y}"
    filename: str = "{date:%Y%m%d} AI.md"
    type: str = Field(default="aiDaily", min_length=1)
    schema_version: str = Field(default="1.0.0", pattern=r"^\d+\.\d+\.\d+$")


class VaultSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    root: Path | None = None
    actions: ActionSettings = Field(default_factory=ActionSettings)
    dailies: DailySettings = Field(default_factory=DailySettings)
    ai_dailies: AiDailySettings = Field(default_factory=AiDailySettings)

    @property
    def actions_root(self) -> Path | None:
        return self.root.joinpath(*self.actions.dir.split("/")) if self.root is not None else None
