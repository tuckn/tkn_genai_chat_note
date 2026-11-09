"""`actions link --dry-run` on synthetic archives and a synthetic Vault: clues A-D, nothing written."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml
from archive_fixtures import ArchiveRun, tree, write_marker

from tkn_genai_chat_note.action_links import link_actions
from tkn_genai_chat_note.cli import main
from tkn_genai_chat_note.config import load_app_config
from tkn_genai_chat_note.runtime import PipelineError

S1 = "11111111-1111-4111-8111-111111111111"
S2 = "22222222-2222-4222-8222-222222222222"
S3 = "33333333-3333-4333-8333-333333333333"
S4 = "44444444-4444-4444-8444-444444444444"
S0 = "00000000-0000-4000-8000-000000000000"
THREAD = "0199aaaa-bbbb-7ccc-8ddd-eeeeeeeeeeee"
A1 = "20261101 Build the importer"
A2 = "20261020 Old finished work"
A3 = "20261102 Write docs for tkn_sample"
A4 = "20261103 Plan the trip with friends"


def action(folder: Path, stem: str, status: str, *, notes: str = "", aliases: list[str] | None = None,
           updated: str = "2026-11-01T10:00:00+09:00") -> Path:
    path = folder / f"{stem}.md"
    metadata: dict[str, Any] = {"type": "action", "title": stem[9:], "status": status, "updated": updated}
    if aliases:
        metadata["aliases"] = aliases
    path.write_text("---\n" + yaml.safe_dump(metadata, allow_unicode=True) + "---\n\n## 1. Context\n\n- x\n\n"
                    f"## 4. Notes\n\n{notes}\n### Visible output\n", encoding="utf-8")
    return path


def vault(root: Path) -> Path:
    folder = root / "vault" / "notes" / "actions" / "2026"
    folder.mkdir(parents=True)
    action(folder, A1, "InProgress")
    action(folder, A2, "Completed", notes=f"- 2026-10-20 決定（本人）：Keep it. 根拠：Claude Code {S2}\n"
                                          "- 2026-10-21 実施（Codex）：Done it. 根拠：同上\n")
    action(folder, A3, "NotStarted")
    action(folder, A4, "InProgress", aliases=["trip plan"])
    (folder.parent / "inbox.md").write_text("---\ntype: action\nstatus:\n---\n# Inbox\n", encoding="utf-8")
    return root / "vault"


def claude(kind: str, content: Any, session: str, cwd: str, when: str, **fields: Any) -> dict[str, Any]:
    return {"type": kind, "sessionId": session, "cwd": cwd, "timestamp": when, "uuid": fields.pop("uuid", kind + when),
            "message": {"role": kind, "content": content}, **fields}


def lines(*records: dict[str, Any]) -> bytes:
    return "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records).encode()


def claude_archive(root: Path, vault_root: Path) -> Path:
    archive = root / "archives" / "claude"
    write_marker(archive, "pc-claude", profile="claude-code", software="Claude Code")
    run = ArchiveRun(archive, "pc-claude", "20261105T020000+0900", "2026-11-05T02:00:00+09:00")
    note = vault_root / "notes" / "actions" / "2026" / f"{A1}.md"
    s1 = lines(
        claude("user", "Task text from another session", S1, str(vault_root), "2026-11-04T00:00:00Z",
               turnOrigin="sdk"),
        claude("user", "Please update the importer note", S1, str(vault_root), "2026-11-04T00:01:00Z",
               turnOrigin="human"),
        claude("assistant", [{"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": str(note)}},
                             {"type": "tool_use", "id": "t2", "name": "Edit",
                              "input": {"file_path": str(note), "old_string": "a", "new_string": "b"}}],
               S1, str(vault_root), "2026-11-04T00:02:00Z"),
        claude("user", [{"type": "tool_result", "tool_use_id": "t1", "content": "text"},
                        {"type": "tool_result", "tool_use_id": "t2", "content": "ok"}],
               S1, str(vault_root), "2026-11-04T00:03:00Z"),
        claude("assistant", [{"type": "text", "text": "Updated the note."}], S1, str(vault_root),
               "2026-11-04T00:04:00Z"),
        {"type": "custom-title", "sessionId": S1, "customTitle": "Update the importer"},
    )
    trip = vault_root / "notes" / "actions" / "2026" / f"{A4}.md"
    subagent = lines(
        claude("user", "Find the trip note", S1, str(vault_root), "2026-11-04T00:02:20Z", isSidechain=True),
        claude("assistant", [{"type": "tool_use", "id": "s1", "name": "Read", "input": {"file_path": str(trip)}}],
               S1, str(vault_root), "2026-11-04T00:02:30Z", isSidechain=True),
    )
    s2 = lines(claude("user", "Fix the build", S2, "C:\\path\\to\\other-repo", "2026-11-03T00:00:00Z",
                      turnOrigin="human"))
    s3 = lines(claude("user", "Add a README section", S3, "C:\\path\\to\\tkn_sample", "2026-11-02T00:00:00Z",
                      turnOrigin="human"))
    s4 = lines(
        claude("user", "<command-name>/model</command-name>\n<command-message>model</command-message>\n"
               "<command-args></command-args>", S4, "C:\\path\\to\\elsewhere", "2026-11-01T00:00:00Z",
               turnOrigin="human"),
        claude("user", f"About [[{A4}]]: book the hotel", S4, "C:\\path\\to\\elsewhere", "2026-11-01T00:01:00Z",
               turnOrigin="human"),
    )
    s0 = lines(claude("user", f"Old talk about [[{A1}]]", S0, str(vault_root), "2026-09-01T00:00:00Z"))
    for session, data in ((S1, s1), (S2, s2), (S3, s3), (S4, s4), (S0, s0)):
        run.version(f"projects/C--work/{session}.jsonl", data)
    run.version(f"projects/C--work/{S1}/subagents/agent-a1.jsonl", subagent)
    run.version(f"projects/C--work/{S1}/tool-results/r1.txt", b"large tool output")
    run.commit()
    return archive


def codex_archive(root: Path) -> Path:
    archive = root / "archives" / "codex"
    write_marker(archive, "pc-codex")
    run = ArchiveRun(archive, "pc-codex", "20261105T020000+0900", "2026-11-05T02:00:00+09:00")
    alias_note = f"X:\\elsewhere\\vault\\notes\\actions\\2026\\{A1}.md"
    rollout = lines(
        {"type": "session_meta", "timestamp": "2026-11-03T00:00:00Z",
         "payload": {"id": THREAD, "timestamp": "2026-11-03T00:00:00Z", "cwd": "X:\\elsewhere\\vault"}},
        {"type": "turn_context", "payload": {"turn_id": "t1", "cwd": "X:\\elsewhere\\vault"}},
        {"type": "response_item", "timestamp": "2026-11-03T00:00:01Z",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text",
                                  "text": "<external_codex_apps_open_page>{}</external_codex_apps_open_page>"}]}},
        {"type": "response_item", "timestamp": "2026-11-03T00:00:02Z",
         "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Check it"}]}},
        {"type": "event_msg", "timestamp": "2026-11-03T00:00:03Z",
         "payload": {"type": "item_completed", "turn_id": "t1",
                     "item": {"type": "CommandExecution", "id": "exec-1",
                              "command": ["pwsh", "-Command", f"Get-Content -LiteralPath '{alias_note}'"],
                              "parsed_cmd": [{"type": "read", "path": alias_note}],
                              "status": "completed", "exit_code": 0, "aggregated_output": "x"}}},
    )
    run.version(f"sessions/2026/11/03/rollout-2026-11-03T00-00-00-{THREAD}.jsonl.zst", rollout, encoding="zstd")
    run.version("session_index.jsonl", lines({"id": THREAD, "thread_name": "Check the importer"}))
    run.commit()
    return archive


def configure(root: Path, **settings: Any) -> tuple[Path, Path, list[Path]]:
    vault_root = vault(root)
    archives = [claude_archive(root, vault_root), codex_archive(root)]
    path = root / "config.yaml"
    path.write_text(yaml.safe_dump({
        "schema_version": "12.0.0",
        "chat_sources": {
            "claude": {"app": "claude-code", "source_id": "pc-claude", "mode": "archive", "archive": str(archives[0])},
            "codex": {"app": "codex", "source_id": "pc-codex", "mode": "archive", "archive": str(archives[1])},
        },
        "cache_root": str(root / "work" / "cache"),
        "utc_offset_minutes": 540,
        "path_aliases": [[str(vault_root), "X:/elsewhere/vault"]],
        "processes": {"data_root": str(root / "work" / "data"), "state_root": str(root / "work" / "state")},
        "vault": {"root": str(vault_root), "actions": {"dir": "notes/actions"}},
        **settings,
    }), encoding="utf-8")
    return path, vault_root, archives


def test_link_uses_clues_a_to_d_and_writes_only_its_report(tmp_path: Path) -> None:
    path, vault_root, archives = configure(tmp_path)
    before = {folder: tree(folder) for folder in (vault_root, *archives)}
    config = load_app_config(explicit_path=path, cwd=tmp_path)

    result = link_actions(config, dry_run=True, as_of=date(2026, 11, 5))

    assert {folder: tree(folder) for folder in (vault_root, *archives)} == before
    assert result["vaultWritten"] is False and result["generativeAiUsed"] is False
    assert result["conversationCount"] == 5  # S0 is older than 14 days.
    assert result["notesWithoutStatus"] == 1  # The inbox is not a link target.
    rows = {row["sessionId"]: row for row in json.loads(Path(result["reportJsonPath"]).read_text("utf-8"))[
        "conversations"]}
    s1 = rows[S1]
    links = {Path(link["action"]).stem: link for link in s1["links"]}
    assert links[A1]["clues"] == ["A"] and {d["via"] for d in links[A1]["details"]} == {"read", "edit"}
    assert links[A4]["details"][0]["subagent"] is True
    assert s1["humanRequestCount"] == 1 and s1["excludedUserMessages"] == {"turnOrigin:sdk": 1}
    assert s1["firstRequest"] == "Please update the importer note" and s1["subagentCount"] == 1
    assert [link["clues"] for link in rows[S2]["links"]] == [["B"]]
    assert rows[S2]["links"][0]["target"] is False  # Completed, still reported.
    assert rows[S3]["links"][0]["clues"] == ["D"] and rows[S3]["links"][0]["strength"] == "medium"
    assert [Path(link["action"]).stem for link in rows[S4]["links"]] == [A4]
    assert rows[S4]["links"][0]["clues"] == ["C"] and rows[S4]["excludedUserMessages"] == {"slash-command": 1}
    codex = rows[THREAD]
    assert codex["title"] == "Check the importer" and codex["firstRequest"] == "Check it"
    assert [Path(link["action"]).stem for link in codex["links"]] == [A1]
    assert codex["excludedUserMessages"] == {"codex-app-block": 1}
    assert result["decisions"] == {"linked": 5, "ambiguous": 0, "followup": 0, "unlinked": 0}
    report = Path(result["reportPath"]).read_text(encoding="utf-8")
    assert f"[[{A1}]]（A(変・読)" in report and "Vaultには書いていない" in report
    assert len(Path(result["linksPath"]).read_text(encoding="utf-8").splitlines()) == 5

    again = link_actions(config, dry_run=True, as_of=date(2026, 11, 5))
    assert again["digests"] == {"cached": 7} and again["decisions"] == result["decisions"]


def test_several_repository_matches_are_left_for_the_ai_step(tmp_path: Path) -> None:
    path, vault_root, _archives = configure(tmp_path)
    folder = vault_root / "notes" / "actions" / "2026"
    action(folder, "20261104 Release tkn_sample", "InProgress")
    config = load_app_config(explicit_path=path, cwd=tmp_path)
    result = link_actions(config, dry_run=True, as_of=date(2026, 11, 5))
    row = next(r for r in json.loads(Path(result["reportJsonPath"]).read_text("utf-8"))["conversations"]
               if r["sessionId"] == S3)
    assert row["decision"] == "ambiguous" and len(row["dCandidates"]) == 2


S5 = "55555555-5555-4555-8555-555555555555"
S6 = "66666666-6666-4666-8666-666666666666"
S7 = "77777777-7777-4777-8777-777777777777"
S8 = "88888888-8888-4888-8888-888888888888"


def run_command(session: str, cwd: str, request: str, command: str | None) -> bytes:
    records = [claude("user", request, session, cwd, "2026-11-04T01:00:00Z", turnOrigin="human")]
    if command is not None:
        records += [
            claude("assistant", [{"type": "tool_use", "id": "b1", "name": "Bash", "input": {"command": command}}],
                   session, cwd, "2026-11-04T01:01:00Z"),
            claude("user", [{"type": "tool_result", "tool_use_id": "b1", "content": "ok"}],
                   session, cwd, "2026-11-04T01:02:00Z"),
        ]
    return lines(*records)


def test_command_mentions_are_weak_and_finished_repositories_are_follow_ups(tmp_path: Path) -> None:
    path, vault_root, archives = configure(tmp_path)
    folder = vault_root / "notes" / "actions" / "2026"
    action(folder, "20261001 Ship tkn_tool", "Completed")
    action(folder, "20261002 Fix windows", "Completed")
    dormant = action(folder, "20260901 Dormant tkn_other", "NotStarted", updated="2026-09-01T10:00:00+09:00")
    trip, docs = folder / f"{A4}.md", folder / f"{A3}.md"
    run = ArchiveRun(archives[0], "pc-claude", "20261105T030000+0900", "2026-11-05T03:00:00+09:00")
    conversations = {
        S5: run_command(S5, str(vault_root), "Commit the notes", f'git add "{trip}" "{dormant}"'),
        S6: run_command(S6, "C:\\path\\to\\tkn_tool", "Add a flag", None),
        S7: run_command(S7, "C:\\path\\to\\windows", "Why is it slow", None),
        S8: run_command(S8, "C:\\path\\to\\tkn_sample", "Fix the docs build", f'git add "{docs}"'),
    }
    for session, data in conversations.items():
        run.version(f"projects/C--work/{session}.jsonl", data)
    run.commit()
    config = load_app_config(explicit_path=path, cwd=tmp_path)

    result = link_actions(config, dry_run=True, as_of=date(2026, 11, 5))

    rows = {row["sessionId"]: row for row in json.loads(Path(result["reportJsonPath"]).read_text("utf-8"))[
        "conversations"]}
    s5 = rows[S5]
    assert s5["decision"] == "unlinked" and s5["links"] == []
    assert {Path(link["action"]).stem for link in s5["weakLinks"]} == {A4, "20260901 Dormant tkn_other"}
    assert {link["strength"] for link in s5["weakLinks"]} == {"weak"}
    assert result["targetActionCount"] == 3  # A command naming the dormant Action does not make it a target.
    s6 = rows[S6]
    assert s6["decision"] == "followup" and [a["title"] for a in s6["finishedActions"]] == ["Ship tkn_tool"]
    assert rows[S7]["decision"] == "unlinked"  # "windows" is a plain word, not compared with titles.
    s8 = rows[S8]["links"]
    assert len(s8) == 1 and s8[0]["clues"] == ["A", "D"] and s8[0]["strength"] == "medium"
    assert rows[S8]["weakLinks"] == []
    assert result["decisions"] == {"linked": 6, "ambiguous": 0, "followup": 1, "unlinked": 2}
    assert result["conversationsWithWeakLinks"] == 1
    report = Path(result["reportPath"]).read_text(encoding="utf-8")
    assert "| 続き | 終了：[[20261001 Ship tkn_tool]]（Completed） |" in report
    assert f"弱い：[[{A4}]]（A(実行)）" in report


def test_dry_run_is_required_and_work_roots_must_not_overlap(tmp_path: Path) -> None:
    path, vault_root, _archives = configure(tmp_path)
    config = load_app_config(explicit_path=path, cwd=tmp_path)
    with pytest.raises(PipelineError, match="--dry-run"):
        link_actions(config, dry_run=False)
    config.processes.state_root = vault_root / "state"
    with pytest.raises(PipelineError, match="must not overlap"):
        link_actions(config, dry_run=True, as_of=date(2026, 11, 5))
    assert not (vault_root / "state").exists()


def test_cli_reports_counts(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path, _vault_root, _archives = configure(tmp_path)
    assert main(["--config", str(path), "actions", "link", "--dry-run", "--as-of", "2026-11-05", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["command"] == "actions link" and result["decisions"]["linked"] == 5
    assert main(["--config", str(path), "actions", "link", "--as-of", "2026-11-05"]) == 1


def test_work_roots_are_checked_after_resolving_parent_steps(tmp_path: Path) -> None:
    path, vault_root, _archives = configure(tmp_path)
    config = load_app_config(explicit_path=path, cwd=tmp_path)
    config.processes.data_root = tmp_path / "work" / ".." / "vault" / "cache"
    with pytest.raises(PipelineError, match="must not overlap"):
        link_actions(config, dry_run=True, as_of=date(2026, 11, 5))
    assert not (vault_root / "cache").exists()


def test_archive_of_another_source_is_skipped(tmp_path: Path) -> None:
    path, _vault_root, archives = configure(tmp_path)
    marker = archives[1] / ".tkn-genai-chat-archive.json"
    value = json.loads(marker.read_text(encoding="utf-8"))
    marker.write_text(json.dumps({**value, "sourceId": "../escape"}), encoding="utf-8")
    config = load_app_config(explicit_path=path, cwd=tmp_path)
    result = link_actions(config, dry_run=True, as_of=date(2026, 11, 5))
    assert [(item["source"], item["reason"]) for item in result["skippedSources"]] == [("codex", "unreadable")]
    assert result["conversationCount"] == 4
    assert not (tmp_path / "work" / "cache" / "digests" / "codex").exists()


def test_short_stems_and_paths_outside_the_vault_are_not_clues(tmp_path: Path) -> None:
    path, vault_root, archives = configure(tmp_path)
    action(vault_root / "notes" / "actions" / "2026", "AI", "InProgress")
    run = ArchiveRun(archives[0], "pc-claude", "20261105T030000+0900", "2026-11-05T03:00:00+09:00")
    other = "C:\\another-vault\\notes\\actions\\2026\\" + A3 + ".md"
    session = "55555555-5555-4555-8555-555555555555"
    run.version(f"projects/C--work/{session}.jsonl", lines(
        claude("user", "I said to run the test", session, str(vault_root), "2026-11-04T01:00:00Z",
               turnOrigin="human"),
        claude("assistant", [{"type": "tool_use", "id": "t9", "name": "Read", "input": {"file_path": other}}],
               session, str(vault_root), "2026-11-04T01:01:00Z"),
    ))
    run.commit()
    config = load_app_config(explicit_path=path, cwd=tmp_path)
    result = link_actions(config, dry_run=True, as_of=date(2026, 11, 5))
    row = next(r for r in json.loads(Path(result["reportJsonPath"]).read_text("utf-8"))["conversations"]
               if r["sessionId"] == session)
    assert row["links"] == [] and row["decision"] == "unlinked"

