"""`ai-dailies build` (stage 2a): the AI's diary from synthetic Claude Code folders and a synthetic Vault."""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml
from archive_fixtures import tree
from pydantic import ValidationError

from tkn_genai_chat_note.ai_dailies import build_ai_dailies
from tkn_genai_chat_note.cli import main
from tkn_genai_chat_note.config import load_app_config
from tkn_genai_chat_note.process_settings import AiDailySettings
from tkn_genai_chat_note.runtime import PipelineError

JST = timezone(timedelta(hours=9))
NOW = datetime(2026, 11, 5, 4, 0, tzinfo=JST)
DAY = date(2026, 11, 4)
S1 = "11111111-1111-4111-8111-111111111111"
S2 = "22222222-2222-4222-8222-222222222222"
S3 = "33333333-3333-4333-8333-333333333333"
FORK = "99999999-9999-4999-8999-999999999999"
ACTION = "20261101 Build the importer"
NOTE = "ai-dailies/2026/20261104 AI.md"


def record(kind: str, content: Any, session: str, cwd: str, when: str, **fields: Any) -> dict[str, Any]:
    return {"type": kind, "sessionId": session, "cwd": cwd, "timestamp": when,
            "uuid": fields.pop("uuid", f"{session[:4]}-{kind}-{when}"),
            "message": {"role": kind, "content": content}, "turnOrigin": "human", **fields}


def use(tool_id: str, name: str, arguments: dict[str, Any], session: str, cwd: str, when: str) -> dict[str, Any]:
    return record("assistant", [{"type": "tool_use", "id": tool_id, "name": name, "input": arguments}],
                  session, cwd, when)


def result(tool_id: str, content: str, session: str, cwd: str, when: str, *, error: bool = False) -> dict[str, Any]:
    return record("user", [{"type": "tool_result", "tool_use_id": tool_id, "content": content, "is_error": error}],
                  session, cwd, when)


def write_transcript(root: Path, session: str, records: list[dict[str, Any]]) -> None:
    path = root / "claude" / "projects" / "C--work" / f"{session}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records), encoding="utf-8")


def main_conversation(vault_root: Path) -> list[dict[str, Any]]:
    """S1 on 2026-11-04 (JST): reads the Action, stumbles a few times, changes a file and commits."""
    cwd = "C:\\path\\to\\tkn_sample"
    action = str(vault_root / "notes" / "actions" / "2026" / f"{ACTION}.md")
    return [
        record("user", "Please build the importer", S1, cwd, "2026-11-04T00:00:00Z"),
        use("t1", "Read", {"file_path": action}, S1, cwd, "2026-11-04T00:01:00Z"),
        result("t1", "the note", S1, cwd, "2026-11-04T00:01:01Z"),
        use("t2", "Read", {"file_path": "C:\\path\\to\\tkn_sample\\missing.md"}, S1, cwd, "2026-11-04T00:02:00Z"),
        result("t2", "File does not exist.", S1, cwd, "2026-11-04T00:02:01Z", error=True),
        use("t3", "Grep", {"pattern": "importer", "path": cwd}, S1, cwd, "2026-11-04T00:03:00Z"),
        result("t3", "a.py", S1, cwd, "2026-11-04T00:03:01Z"),
        use("t4", "Grep", {"pattern": "importer", "path": cwd}, S1, cwd, "2026-11-04T00:04:00Z"),
        result("t4", "a.py", S1, cwd, "2026-11-04T00:04:01Z"),
        use("t5", "Bash", {"command": "rm -rf build"}, S1, cwd, "2026-11-04T00:05:00Z"),
        result("t5", "The user doesn't want to proceed with this tool use.", S1, cwd, "2026-11-04T00:05:01Z",
               error=True),
        record("assistant", [{"type": "text", "text": "Started."}], S1, cwd, "2026-11-04T00:06:00Z"),
        record("user", "違う、そうではなく a.py を直して", S1, cwd, "2026-11-04T00:07:00Z"),
        use("t6", "Edit", {"file_path": "C:\\path\\to\\tkn_sample\\a.py", "old_string": "a", "new_string": "b"},
            S1, cwd, "2026-11-04T00:08:00Z"),
        result("t6", "ok", S1, cwd, "2026-11-04T00:08:01Z"),
        use("t7", "Bash", {"command": "git commit -m fix"}, S1, cwd, "2026-11-04T00:09:00Z"),
        result("t7", "1 file changed", S1, cwd, "2026-11-04T00:09:01Z"),
        record("assistant", [{"type": "text", "text": "Fixed."}], S1, cwd, "2026-11-04T00:10:00Z"),
        {"type": "custom-title", "sessionId": S1, "customTitle": "Build the importer sk-abcdefghijklmnopqrstuvwx"},
    ]


def configure(root: Path) -> tuple[Path, Path]:
    vault_root = root / "vault"
    actions = vault_root / "notes" / "actions" / "2026"
    actions.mkdir(parents=True)
    (actions / f"{ACTION}.md").write_text(
        "---\n" + yaml.safe_dump({"title": ACTION[9:], "status": "InProgress"}) + "---\n\n## 4. Notes\n\n"
        f"- 2026-11-04 実施（Claude Code）：Fixed it. 根拠：Claude Code {S1}\n", encoding="utf-8")
    dailies = vault_root / "dailies" / "2026"
    dailies.mkdir(parents=True)
    (dailies / "20261104.md").write_text("# My day\n", encoding="utf-8")
    write_transcript(root, S1, main_conversation(vault_root))
    path = root / "config.yaml"
    path.write_text(yaml.safe_dump({
        "schema_version": "12.0.0",
        "chat_sources": {"claude": {"app": "claude-code", "source_id": "pc", "root": str(root / "claude")}},
        "cache_root": str(root / "work" / "cache"),
        "utc_offset_minutes": 540,
        "processes": {"data_root": str(root / "work" / "data"), "state_root": str(root / "work" / "state")},
        "vault": {"root": str(vault_root), "actions": {"dir": "notes/actions"}},
    }), encoding="utf-8")
    return path, vault_root


def read_note(vault_root: Path) -> str:
    return (vault_root / NOTE).read_text(encoding="utf-8")


def test_build_writes_the_diary_and_its_counts(tmp_path: Path) -> None:
    path, vault_root = configure(tmp_path)
    cwd = "C:\\path\\to\\tkn_sample"
    copied = main_conversation(vault_root)[:3]  # A fork copies the earlier records with their session ID.
    write_transcript(tmp_path, FORK, [*copied, record("user", "Now write the docs", FORK, cwd, "2026-11-04T03:00:00Z")])
    before = tree(tmp_path / "claude")
    config = load_app_config(explicit_path=path, cwd=tmp_path)

    built = build_ai_dailies(config, dry_run=False, end=DAY, now=NOW)

    assert tree(tmp_path / "claude") == before
    assert built["complete"] is True and built["generativeAiUsed"] is False and built["vaultWritten"] is True
    assert [(day["date"], day["note"], day["conversations"]) for day in built["days"]] == [("2026-11-04", "created", 2)]
    text = read_note(vault_root)
    head, body = text.split("\n---\n", 1)
    metadata = yaml.safe_load(head.removeprefix("---\n"))
    assert metadata["type"] == "aiDaily" and metadata["title"] == "20261104 AI"
    assert metadata["actions"] == [f"[[{ACTION}]]"] and metadata["generator"] == "tkn-genai-chat-note"
    assert metadata["reviewStatus"] == "unreviewed" and list(metadata)[-3:] == ["created", "updated", "noteId"]
    assert "本人のDaily：[[20261104]]" in body
    assert (f"- 09:00〜09:10 Claude Code「Build the importer [REDACTED]」：作業フォルダ tkn_sample、本人の発言 2件"
            f"（[[{ACTION}]]、Claude Code {S1}）") in body
    assert (f"- 12:00〜12:00 Claude Code「Now write the docs」：作業フォルダ tkn_sample、本人の発言 1件"
            f"（Claude Code {FORK}）") in body
    assert ("- 資料探し：読む・探す・一覧 4回。読み込みの失敗 1件（ファイルがない 1件）、同じ検索の繰り返し 1件、"
            "3回以上読んだファイル 0件、広すぎる走査 0件") in body
    assert "- 本人の拒否 1件。本人の訂正らしい発言 1件" in body
    assert "会話 1件のうち、作業の前にActionを読んだ 1件、ActionのNotesにこの会話を根拠にした行がある 1件" in body
    assert f"（Claude Code {S1}）" in body.split("## うまくいかなかったこと", 1)[1]
    sections = [line for line in body.splitlines() if line.startswith("## ")]
    assert sections == ["## 今日やったこと", "## うまくいったこと", "## うまくいかなかったこと",
                        "## 本人から学んだこと", "## 持ち越し"]
    metrics = json.loads(Path(built["days"][0]["metricsPath"]).read_text(encoding="utf-8"))
    by_session = {item["sessionRef"]["sessionId"]: item for item in metrics["conversations"]}
    assert by_session[FORK]["day"]["toolEvents"] == 0  # The copied Read belongs to S1 only.
    assert by_session[S1]["start"]["actionRules"]["workKinds"] == {"file-written": 1, "git-commit": 1}
    assert by_session[S1]["start"]["entrances"][0]["kind"] == "action"
    assert metrics["totals"]["readFailures"] == {"not-found": 1}


def test_unchanged_notes_are_rebuilt_and_changed_ones_get_additions(tmp_path: Path) -> None:
    path, vault_root = configure(tmp_path)
    config = load_app_config(explicit_path=path, cwd=tmp_path)
    first = build_ai_dailies(config, dry_run=False, end=DAY, now=NOW)
    assert first["days"][0]["note"] == "created"
    created = yaml.safe_load(read_note(vault_root).split("\n---\n", 1)[0].removeprefix("---\n"))
    again = build_ai_dailies(config, dry_run=False, end=DAY, now=NOW + timedelta(hours=1))
    assert again["days"][0]["note"] == "unchanged"

    cwd = "C:\\path\\to\\other"
    write_transcript(tmp_path, S2, [record("user", "Check the logs", S2, cwd, "2026-11-04T05:00:00Z")])
    rebuilt = build_ai_dailies(config, dry_run=False, end=DAY, now=NOW + timedelta(hours=2))
    assert rebuilt["days"][0]["note"] == "rebuilt"
    again = yaml.safe_load(read_note(vault_root).split("\n---\n", 1)[0].removeprefix("---\n"))
    assert (again["noteId"], again["created"]) == (created["noteId"], created["created"])
    assert again["updated"] != created["updated"] and S2 in read_note(vault_root)

    note = vault_root / NOTE
    note.write_bytes(note.read_bytes().replace(b"\n", b"\r\n") + "私のメモ\r\n".encode())
    old = (NOW - timedelta(hours=1)).timestamp()
    os.utime(note, (old, old))
    write_transcript(tmp_path, S3, [record("user", "Plan the release", S3, cwd, "2026-11-04T06:00:00Z")])
    appended = build_ai_dailies(config, dry_run=False, end=DAY, now=NOW + timedelta(hours=3))
    assert appended["days"][0]["note"] == "appended"
    text = note.read_bytes().decode("utf-8")
    assert "私のメモ\r\n\r\n## 追加（2026-11-05 07:00）\r\n\r\n- 15:00〜15:00 Claude Code「Plan the release」" in text
    assert text.count(S2) == 1 and "\n" not in text.replace("\r\n", "")
    assert 'updated: "2026-11-05T07:00:00+09:00"' in text and created["noteId"] in text

    # The person is writing (changed a minute ago): the next run leaves the note alone.
    write_transcript(tmp_path, "44444444-4444-4444-8444-444444444444",
                     [record("user", "One more", "44444444-4444-4444-8444-444444444444", cwd, "2026-11-04T07:00:00Z")])
    recent = (NOW + timedelta(hours=4) - timedelta(minutes=1)).timestamp()
    os.utime(note, (recent, recent))
    waiting = build_ai_dailies(config, dry_run=False, end=DAY, now=NOW + timedelta(hours=4))
    assert waiting["days"][0]["note"] == "deferred" and waiting["complete"] is False
    assert note.read_bytes().decode("utf-8") == text


def test_dry_run_writes_only_previews_and_unfinished_days_need_it(tmp_path: Path) -> None:
    path, vault_root = configure(tmp_path)
    before = tree(vault_root)
    config = load_app_config(explicit_path=path, cwd=tmp_path)

    planned = build_ai_dailies(config, dry_run=True, end=DAY, days=2, now=NOW)

    assert tree(vault_root) == before and not (tmp_path / "work" / "data" / "ai-dailies").exists()
    assert [(day["date"], day["note"]) for day in planned["days"]] == [
        ("2026-11-03", "no-conversations"), ("2026-11-04", "would-create")]
    preview = Path(planned["days"][1]["previewPath"])
    assert preview.name == "20261104 AI.md" and "## 今日やったこと" in preview.read_text(encoding="utf-8")
    assert Path(planned["days"][1]["metricsPath"]).is_relative_to(tmp_path / "work" / "state")
    with pytest.raises(PipelineError, match="not over yet"):
        build_ai_dailies(config, dry_run=False, end=NOW.date(), now=NOW)
    assert build_ai_dailies(config, dry_run=True, end=NOW.date(), now=NOW)["days"][0]["note"] == "no-conversations"
    with pytest.raises(PipelineError, match="future"):
        build_ai_dailies(config, dry_run=True, end=NOW.date() + timedelta(days=1), now=NOW)


def test_vault_root_is_required_and_work_roots_must_stay_outside(tmp_path: Path) -> None:
    path, vault_root = configure(tmp_path)
    config = load_app_config(explicit_path=path, cwd=tmp_path)
    config.processes.data_root = vault_root / "data"
    with pytest.raises(PipelineError, match="must not overlap"):
        build_ai_dailies(config, dry_run=False, end=DAY, now=NOW)
    config.vault.root = None
    with pytest.raises(PipelineError, match="vault.root"):
        build_ai_dailies(config, dry_run=True, end=DAY, now=NOW)


@pytest.mark.parametrize(("field", "value"), [
    ("dir", "{date.year}"), ("dir", "../outside"), ("dir", "C:/abs"), ("dir", "{name}"),
    ("filename", "sub/{date:%Y}.md"), ("filename", "{date:%Y}.txt"), ("filename", "a|b.md"),
    ("filename", "AI.md"), ("filename", "{date:%d}.md"),  # Several dates would share one note.
    ("filename", "{date:%Y-%m-%w} AI.md"),  # The weekday repeats every week.
])
def test_note_locations_take_only_date_fields_inside_the_vault(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        AiDailySettings(**{field: value})


@pytest.mark.parametrize("key", ["logs", "ai_logs"])
def test_the_earlier_note_keys_are_refused(tmp_path: Path, key: str) -> None:
    path, _vault_root = configure(tmp_path)
    settings = yaml.safe_load(path.read_text(encoding="utf-8"))
    settings["vault"][key] = {"dir": "notes/{date:%Y}", "filename": "{date:%Y%m%d}.md"}
    path.write_text(yaml.safe_dump(settings), encoding="utf-8")
    with pytest.raises(PipelineError, match=key):
        load_app_config(explicit_path=path, cwd=tmp_path)


def test_cli_builds_a_past_day(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path, vault_root = configure(tmp_path)
    assert main(["--config", str(path), "ai-dailies", "build", "--date", "2026-11-04", "--json"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["command"] == "ai-dailies build" and output["notes"] == {"created": 1}
    assert (vault_root / NOTE).is_file()


def test_a_note_is_not_rebuilt_while_a_source_cannot_be_read(tmp_path: Path) -> None:
    path, vault_root = configure(tmp_path)
    config = load_app_config(explicit_path=path, cwd=tmp_path)
    assert build_ai_dailies(config, dry_run=False, end=DAY, now=NOW)["days"][0]["note"] == "created"
    before = read_note(vault_root)
    metrics = tmp_path / "work" / "data" / "ai-dailies" / "metrics" / "2026" / "2026-11-04.json"
    counts = metrics.read_bytes()
    write_transcript(tmp_path, S2, [record("user", "Check the logs", S2, "C:/path/to/other", "2026-11-04T05:00:00Z")])
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["chat_sources"]["missing"] = {"app": "codex", "source_id": "pc", "root": str(tmp_path / "no-codex")}
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    config = load_app_config(explicit_path=path, cwd=tmp_path)

    waiting = build_ai_dailies(config, dry_run=False, end=DAY, now=NOW + timedelta(hours=1))

    day = waiting["days"][0]
    assert day["note"] == "deferred" and "could not be read: missing" in day["reason"]
    assert waiting["complete"] is False and "missing" in waiting["incompleteInputs"]
    assert read_note(vault_root) == before and metrics.read_bytes() == counts and "metricsKept" in day


def test_busy_notes_are_not_retried_and_deleted_notes_are_not_written_again(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path, vault_root = configure(tmp_path)
    config = load_app_config(explicit_path=path, cwd=tmp_path)
    build_ai_dailies(config, dry_run=False, end=DAY, now=NOW)
    before = read_note(vault_root)
    write_transcript(tmp_path, S2, [record("user", "Check the logs", S2, "C:/path/to/other", "2026-11-04T05:00:00Z")])

    replace = os.replace
    attempts: list[str] = []

    def busy(source: Any, target: Any) -> None:
        if str(target).endswith(" AI.md") and not attempts:  # The note is open, at first only.
            attempts.append(str(target))
            raise PermissionError(32, "The process cannot access the file because it is being used")
        replace(source, target)

    monkeypatch.setattr(os, "replace", busy)
    waiting = build_ai_dailies(config, dry_run=False, end=DAY, now=NOW + timedelta(hours=1))
    assert waiting["days"][0]["note"] == "deferred" and read_note(vault_root) == before
    assert len(attempts) == 1 and "open in another program" in waiting["days"][0]["reason"]
    assert not [name for name in os.listdir(vault_root / "ai-dailies" / "2026") if name.startswith(".tmp-")]
    monkeypatch.undo()

    (vault_root / NOTE).unlink()
    gone = build_ai_dailies(config, dry_run=False, end=DAY, now=NOW + timedelta(hours=2))
    assert gone["days"][0]["note"] == "kept" and "deleted or moved" in gone["days"][0]["reason"]
    assert not (vault_root / NOTE).exists()


def test_a_date_with_only_tool_runs_has_a_diary_and_previews_keep_their_folders(tmp_path: Path) -> None:
    path, vault_root = configure(tmp_path)
    cwd = "C:/path/to/night"
    write_transcript(tmp_path, S3, [
        record("user", "Run the long job", S3, cwd, "2026-11-02T14:00:00Z"),  # 11-02 23:00 JST
        use("n1", "Bash", {"command": "make all"}, S3, cwd, "2026-11-02T16:00:00Z"),  # 11-03 01:00 JST
        result("n1", "done", S3, cwd, "2026-11-02T16:30:00Z"),
        record("assistant", [{"type": "text", "text": "Finished."}], S3, cwd, "2026-11-03T16:00:00Z"),  # 11-04
    ])
    config = load_app_config(explicit_path=path, cwd=tmp_path)

    planned = build_ai_dailies(config, dry_run=True, end=date(2026, 11, 3), now=NOW)

    day = planned["days"][0]
    assert day["note"] == "would-create" and day["conversations"] == 1
    preview = Path(day["previewPath"])
    assert preview.as_posix().endswith("/notes/ai-dailies/2026/20261103 AI.md")
    assert "- 01:00〜01:00 Claude Code「Run the long job」：作業フォルダ night、本人の発言なし" in preview.read_text(
        encoding="utf-8")


def test_state_and_data_roots_must_not_overlap(tmp_path: Path) -> None:
    path, _vault_root = configure(tmp_path)
    config = load_app_config(explicit_path=path, cwd=tmp_path)
    state = config.processes.state_root
    config.processes.state_root = config.processes.data_root / "state"
    with pytest.raises(PipelineError, match="must not overlap"):
        build_ai_dailies(config, dry_run=True, end=DAY, now=NOW)
    assert not (config.processes.state_root).exists()
    config.processes.state_root = state
    config.cache_root = config.processes.data_root / "cache"
    with pytest.raises(PipelineError, match="digest cache"):
        build_ai_dailies(config, dry_run=True, end=DAY, now=NOW)
    assert not config.processes.data_root.exists()


def test_an_unlisted_folder_keeps_the_note_from_being_rebuilt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path, vault_root = configure(tmp_path)
    config = load_app_config(explicit_path=path, cwd=tmp_path)
    build_ai_dailies(config, dry_run=False, end=DAY, now=NOW)
    before = read_note(vault_root)
    write_transcript(tmp_path, S2, [record("user", "Check the logs", S2, "C:/path/to/other", "2026-11-04T05:00:00Z")])
    walk = os.walk

    def locked(top: Any, onerror: Any = None, followlinks: bool = False) -> Any:
        if onerror is not None:
            onerror(PermissionError(13, "Access is denied", os.path.join(str(top), "locked")))
        yield from walk(top, onerror=onerror, followlinks=followlinks)

    monkeypatch.setattr(os, "walk", locked)
    waiting = build_ai_dailies(config, dry_run=False, end=DAY, now=NOW + timedelta(hours=1))
    assert waiting["days"][0]["note"] == "deferred" and "claude" in waiting["incompleteInputs"]
    assert read_note(vault_root) == before
    assert waiting["sources"][0]["notes"][0]["reason"] == "folder-unlisted"


def test_a_subagent_of_an_older_parent_file_is_counted(tmp_path: Path) -> None:
    path, _vault_root = configure(tmp_path)
    cwd = "C:/path/to/old"
    parent = tmp_path / "claude" / "projects" / "C--work" / f"{S3}.jsonl"
    write_transcript(tmp_path, S3, [record("user", "Start a long review", S3, cwd, "2026-10-20T00:00:00Z")])
    old = (NOW - timedelta(days=15)).timestamp()
    os.utime(parent, (old, old))
    agent = parent.with_suffix("") / "subagents" / "agent-a1.jsonl"
    agent.parent.mkdir(parents=True)
    agent.write_text("".join(json.dumps(item) + "\n" for item in (
        record("user", "Review it", S3, cwd, "2026-11-04T01:00:00Z", isSidechain=True),
        {**use("g1", "Grep", {"pattern": "x", "path": cwd}, S3, cwd, "2026-11-04T01:01:00Z"), "isSidechain": True},
        {**result("g1", "a.py", S3, cwd, "2026-11-04T01:01:30Z"), "isSidechain": True},
    )), encoding="utf-8")
    config = load_app_config(explicit_path=path, cwd=tmp_path)

    planned = build_ai_dailies(config, dry_run=True, end=DAY, now=NOW)

    assert planned["orphanSubagents"] == 0 and planned["days"][0]["conversations"] == 2
    metrics = json.loads(Path(planned["days"][0]["metricsPath"]).read_text(encoding="utf-8"))
    by_session = {item["sessionRef"]["sessionId"]: item for item in metrics["conversations"]}
    assert by_session[S3]["day"]["lookups"] == 1 and by_session[S3]["start"] is None


def slash_command(session: str, cwd: str, when: str, name: str = "/model") -> list[dict[str, Any]]:
    """What Claude Code records when the person only runs a slash command: no request, no answer."""
    return [
        record("user", "<local-command-caveat>The command below was run directly.</local-command-caveat>",
               session, cwd, when, turnOrigin=None, uuid=f"{session[:4]}-caveat-{when}"),
        record("user", f"<command-name>{name}</command-name>\n<command-message>{name[1:]}</command-message>\n"
               "<command-args></command-args>", session, cwd, when, turnOrigin=None, uuid=f"{session[:4]}-cmd-{when}"),
        record("user", "<local-command-stdout>Set model</local-command-stdout>", session, cwd, when,
               turnOrigin=None, uuid=f"{session[:4]}-out-{when}"),
    ]


def test_conversations_with_only_slash_commands_are_left_out(tmp_path: Path) -> None:
    path, vault_root = configure(tmp_path)
    cwd = "C:/Windows/Temp/_tkn"
    write_transcript(tmp_path, S2, slash_command(S2, cwd, "2026-11-04T03:00:00Z"))
    # S3 only ran /model on 11-03 and worked on 11-04: it belongs to 11-04, where it also starts.
    write_transcript(tmp_path, S3, [*slash_command(S3, cwd, "2026-11-03T03:00:00Z"),
                                    record("user", "Now fix the tests", S3, cwd, "2026-11-04T04:00:00Z"),
                                    record("assistant", [{"type": "text", "text": "Fixed."}], S3, cwd,
                                           "2026-11-04T04:05:00Z")])
    config = load_app_config(explicit_path=path, cwd=tmp_path)

    planned = build_ai_dailies(config, dry_run=True, end=DAY, days=2, now=NOW)

    before, day = planned["days"]
    assert before == {"date": "2026-11-03", "note": "no-conversations", "conversations": 0, "withoutAiWork": 1}
    assert day["conversations"] == 2 and day["withoutAiWork"] == 1
    text = Path(day["previewPath"]).read_text(encoding="utf-8")
    assert S2 not in text and "（タイトルなし）" not in text and f"Claude Code {S3}" in text
    metrics = json.loads(Path(day["metricsPath"]).read_text(encoding="utf-8"))
    assert [ref["sessionId"] for ref in metrics["withoutAiWork"]] == [S2]
    by_session = {item["sessionRef"]["sessionId"]: item for item in metrics["conversations"]}
    assert by_session[S3]["start"] is not None and metrics["totals"]["conversations"] == 2


def test_commands_with_arguments_are_not_work_and_undated_work_is_kept(tmp_path: Path) -> None:
    path, _vault_root = configure(tmp_path)
    cwd = "C:/Windows/Temp/_tkn"
    with_arguments = slash_command(S2, cwd, "2026-11-04T03:00:00Z")
    with_arguments[1] = record("user", "<command-name>/model</command-name>\n<command-message>model</command-message>\n"
                               "<command-args>opus</command-args>", S2, cwd, "2026-11-04T03:00:00Z", turnOrigin=None)
    write_transcript(tmp_path, S2, with_arguments)
    # A request without a time next to a dated line the application wrote: nothing is left out.
    write_transcript(tmp_path, S3, [*slash_command(S3, cwd, "2026-11-04T03:00:00Z"),
                                    record("user", "Check the disk", S3, cwd, "")])
    # A request that starts with a path is a request, even when the answer comes the next day.
    s4 = "44444444-4444-4444-8444-444444444444"
    write_transcript(tmp_path, s4, [record("user", "/tmp を調べてください", s4, cwd, "2026-11-04T06:00:00Z")])
    config = load_app_config(explicit_path=path, cwd=tmp_path)

    day = build_ai_dailies(config, dry_run=True, end=DAY, now=NOW)["days"][0]

    text = Path(day["previewPath"]).read_text(encoding="utf-8")
    assert S2 not in text and f"Claude Code {S3}" in text and day["withoutAiWork"] == 1
    assert f"本人の発言 1件（Claude Code {s4}）" in text


def test_a_note_whose_conversations_no_longer_count_is_left_and_reported(tmp_path: Path) -> None:
    path, vault_root = configure(tmp_path)
    (tmp_path / "claude" / "projects" / "C--work" / f"{S1}.jsonl").unlink()
    cwd = "C:/Windows/Temp/_tkn"
    write_transcript(tmp_path, S2, [record("user", "Check the logs", S2, cwd, "2026-11-04T05:00:00Z"),
                                    record("assistant", [{"type": "text", "text": "Done."}], S2, cwd,
                                           "2026-11-04T05:01:00Z")])
    config = load_app_config(explicit_path=path, cwd=tmp_path)
    assert build_ai_dailies(config, dry_run=False, end=DAY, now=NOW)["days"][0]["note"] == "created"
    before = read_note(vault_root)
    write_transcript(tmp_path, S2, slash_command(S2, cwd, "2026-11-04T05:00:00Z"))

    day = build_ai_dailies(config, dry_run=False, end=DAY, now=NOW + timedelta(hours=1))["days"][0]

    assert day["note"] == "stale" and day["withoutAiWork"] == 1 and "left as it is" in day["reason"]
    assert read_note(vault_root) == before


def test_the_start_belongs_to_the_first_working_date_of_the_conversation_itself(tmp_path: Path) -> None:
    path, _vault_root = configure(tmp_path)
    cwd = "C:/path/to/night"
    write_transcript(tmp_path, S3, [record("user", "Review it", S3, cwd, "2026-11-04T01:00:00Z"),
                                    record("assistant", [{"type": "text", "text": "Reviewed."}], S3, cwd,
                                           "2026-11-04T01:30:00Z")])
    agent = tmp_path / "claude" / "projects" / "C--work" / S3 / "subagents" / "agent-a1.jsonl"
    agent.parent.mkdir(parents=True)
    agent.write_text(json.dumps(record("assistant", [{"type": "text", "text": "Early note."}], S3, cwd,
                                       "2026-11-02T16:00:00Z", isSidechain=True)) + "\n", encoding="utf-8")
    config = load_app_config(explicit_path=path, cwd=tmp_path)

    planned = build_ai_dailies(config, dry_run=True, end=DAY, days=2, now=NOW)

    starts = {}
    for day in planned["days"]:
        metrics = json.loads(Path(day["metricsPath"]).read_text(encoding="utf-8"))
        starts[day["date"]] = {item["sessionRef"]["sessionId"]: item["start"] is not None
                               for item in metrics["conversations"]}
    assert starts["2026-11-03"][S3] is False and starts["2026-11-04"][S3] is True
