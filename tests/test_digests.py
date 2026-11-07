"""Digest rules: paths, the person's requests, tool categories, evidence IDs and continuation."""

from __future__ import annotations

from typing import Any

import pytest
from tkn_genai_chat_archiver.view import ArchiveVersion
from tkn_genai_chat_search.library import ToolEvent

from tkn_genai_chat_note.digests import (
    continuation,
    evidence_id,
    failure_kind,
    line_count,
    pasted_kind,
    request_text,
    tool_category,
)
from tkn_genai_chat_note.path_text import PathText


@pytest.mark.parametrize("value", [
    r"C:\work\vault\notes\a.md", "c:/work/vault/notes/a.md", "file:///C:/work/vault/notes/a.md",
    r"\\?\C:\work\vault\notes\a.md", "/c/work/vault/notes/a.md", "/mnt/c/work/vault/notes/a.md",
    r"D:\mirror\vault\notes\a.md", "file:///D:/mirror/vault/notes/a.md",
])
def test_path_forms_and_aliases_normalize_to_one_text(value: str) -> None:
    paths = PathText([["C:/work/vault", "D:/mirror/vault"]])
    assert paths.text(value) == "c:/work/vault/notes/a.md"
    assert paths.is_within(value, "C:\\work\\vault")


def test_aliases_replace_only_whole_prefixes_and_spaces_decode() -> None:
    paths = PathText([["C:/work", "D:/w"]])
    assert paths.text("D:/wx/file.md") == "d:/wx/file.md"
    assert paths.text("file:///D:/w/a%20b.md") == "c:/work/a b.md"
    assert paths.text("C:/a/b/c") == "c:/a/b/c"  # A drive path is not read as a Git Bash path.


@pytest.mark.parametrize(("app", "text", "expected"), [
    ("codex", "<external_codex_apps_open_page>{}</external_codex_apps_open_page>", (None, "codex-app-block")),
    ("codex", "<user_action>review</user_action>\nThen fix it", ("Then fix it", "")),
    ("claude-code", "<command-name>/model</command-name>\n<command-args></command-args>", (None, "slash-command")),
    ("claude-code", "<command-name>/codex-review</command-name><command-args>xhigh</command-args>",
     ("/codex-review xhigh", "")),
    ("claude-code", "<local-command-stdout>done</local-command-stdout>", (None, "generated")),
    ("claude-code", "[Request interrupted by user]", (None, "generated")),
    ("claude-code", "Please continue", ("Please continue", "")),
])
def test_request_text(app: str, text: str, expected: tuple[str | None, str]) -> None:
    assert request_text(text, app) == expected


def test_pasted_kinds() -> None:
    assert pasted_kind("typed only") == "none"
    assert pasted_kind('<pasted_content id="a">x</pasted_content>') == "whole"
    assert pasted_kind('Look:\n<pasted_content id="a">x</pasted_content>') == "partial"


def event(
    name: str, arguments: Any, *, origin: str = "call", error: bool | None = None, preview: str = ""
) -> ToolEvent:
    return ToolEvent(call_id="c", name=name, arguments=arguments, timestamp="", line_number=1, origin=origin,
                     is_error=error, error_preview=preview)


@pytest.mark.parametrize(("item", "category"), [
    (event("Read", {"file_path": "a"}), "read"),
    (event("Grep", {"pattern": "x"}), "search"),
    (event("Edit", {"file_path": "a"}), "edit"),
    (event("Bash", {"command": "rg -n x src"}), "search"),
    (event("PowerShell", {"command": "Get-Content a.md"}), "read"),
    (event("Bash", {"command": "uv run pytest"}), "run"),
    (event("CommandExecution", {"command": ["pwsh", "-Command", "Get-ChildItem x"], "parsed_cmd": []},
           origin="item"), "list"),
    (event("CommandExecution", {"parsed_cmd": [{"type": "read"}]}, origin="item"), "read"),
    (event("FileChange", {"changes": {"a": {}}}, origin="item"), "edit"),
    (event("mcp__browser__navigate", {}), "mcp"),
    (event("WebSearch", {"query": "x"}), "web"),
    (event("exec", "await tools.x()"), "script"),
])
def test_tool_categories(item: ToolEvent, category: str) -> None:
    assert tool_category(item) == category


def test_failure_kinds() -> None:
    assert failure_kind(event("Read", {}, error=True, preview="File does not exist.")) == "not-found"
    assert failure_kind(event("Bash", {}, error=True, preview="The user doesn't want to proceed")) == "rejected"
    assert failure_kind(event("Bash", {}, error=True, preview="boom")) == "other"
    assert failure_kind(event("Bash", {}, error=False)) is None


def version(sha: str, relation: str | None) -> ArchiveVersion:
    return ArchiveVersion(relative_path="p", stored_path="s", sha256=sha, byte_count=1, captured_at="", run_id="",
                          record_id="", content_encoding="identity", content_sha256=sha, content_byte_count=1,
                          previous_sha256=None, previous_relation=relation, record={})


def test_evidence_ids_and_continuation() -> None:
    first, second, third = version("a" * 64, None), version("b" * 64, "appended"), version("c" * 64, "rewritten")
    assert evidence_id("codex", "pc", "s1", "b" * 64, 12) == "codex/pc/s1@bbbbbbbbbbbb#L12"
    assert continuation([first], None, 0) == {"fromLine": 1, "rewritten": False}
    assert continuation([first], "a" * 64, 7) == {"fromLine": 8, "rewritten": False}
    assert continuation([first, second], "a" * 64, 7) == {"fromLine": 8, "rewritten": False}
    assert continuation([first, second, third], "a" * 64, 7) == {"fromLine": 1, "rewritten": True}
    assert continuation([first, second], "z" * 64, 7) == {"fromLine": 1, "rewritten": True}
    assert line_count(b"a\nb\n") == 2 and line_count(b"a\nb") == 2 and line_count(b"") == 0


def _claude_digest(records: list[dict[str, Any]], session: str) -> dict[str, Any]:
    import hashlib
    import json

    from tkn_genai_chat_note.chat_sources import LogFile
    from tkn_genai_chat_note.digests import DigestContext, build_digest

    data = "".join(json.dumps(item) + "\n" for item in records).encode()
    log = LogFile("claude", "claude-code", "pc", f"projects/C--work/{session}.jsonl", "main", "app", None)
    context = DigestContext(540, PathText(), "", "")
    return build_digest(log, data, hashlib.sha256(data).hexdigest(), context)["session"]


def _turn(kind: str, content: Any, session: str, when: str, uuid: str) -> dict[str, Any]:
    return {"type": kind, "sessionId": session, "cwd": "C:/work", "timestamp": when, "uuid": uuid,
            "turnOrigin": "human", "message": {"role": kind, "content": content}}


def test_records_copied_from_another_session_are_left_to_that_session() -> None:
    parent, fork = "11111111-1111-4111-8111-111111111111", "99999999-9999-4999-8999-999999999999"
    session = _claude_digest([
        _turn("user", "Old request", parent, "2026-11-01T00:00:00Z", "u1"),
        _turn("assistant", [{"type": "tool_use", "id": "a", "name": "Read", "input": {"file_path": "C:/x.md"}}],
              parent, "2026-11-01T00:01:00Z", "u2"),
        _turn("user", "New request", fork, "2026-11-03T00:00:00Z", "u3"),
    ], fork)
    assert [request["text"] for request in session["userRequests"]] == ["New request"]
    assert session["toolEvents"] == [] and session["localDates"] == ["2026-11-03"]
    assert session["copiedRecords"] == {"records": 2, "from": [parent]}
    assert session["created"] == "2026-11-03T00:00:00Z"


def test_unregistering_a_scheduled_task_is_not_a_registration() -> None:
    session_id = "11111111-1111-4111-8111-111111111111"
    records = [_turn("user", "Clean up", session_id, "2026-11-01T00:00:00Z", "u1")]
    for index, command in enumerate(("Unregister-ScheduledTask -TaskName Old", "Register-ScheduledTask -TaskName New",
                                     "schtasks /create /tn Other")):
        records.append(_turn("assistant", [{"type": "tool_use", "id": f"t{index}", "name": "PowerShell",
                                            "input": {"command": command}}],
                             session_id, f"2026-11-01T00:0{index + 1}:00Z", f"a{index}"))
        records.append(_turn("user", [{"type": "tool_result", "tool_use_id": f"t{index}", "content": "ok"}],
                             session_id, f"2026-11-01T00:0{index + 1}:30Z", f"r{index}"))
    facts = _claude_digest(records, session_id)["facts"]
    assert [fact["evidenceId"].rsplit("#L", 1)[1] for fact in facts if fact["kind"] == "scheduled-task-registered"] \
        == ["4", "6"]
