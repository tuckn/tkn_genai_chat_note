"""Reading locations (`chat_sources` modes app, archive and auto), evidence IDs and the digest cache."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml
import zstandard
from archive_fixtures import ArchiveRun, tree
from test_actions_link import S1, S2, THREAD, claude, configure, lines
from tkn_genai_chat_archiver.view import ArchiveView

from tkn_genai_chat_note.action_links import link_actions
from tkn_genai_chat_note.config import load_app_config
from tkn_genai_chat_note.runtime import PipelineError

AS_OF = date(2026, 11, 5)
ROLLOUT = f"sessions/2026/11/03/rollout-2026-11-03T00-00-00-{THREAD}.jsonl.zst"


def mirror(archive: Path, app_root: Path, *, decode: bool = False) -> Path:
    """Write the latest stored bytes of every archived path where the application keeps them."""
    view = ArchiveView(archive)
    for relative_path in view.relative_paths():
        version = view.latest(relative_path)
        assert version is not None
        data = view.read_content(version) if decode else view.read_stored(version)
        name = relative_path[: -len(".zst")] if decode and relative_path.endswith(".zst") else relative_path
        target = app_root.joinpath(*name.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return app_root


def rewrite(path: Path, **changes: Any) -> None:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document.update(changes)
    path.write_text(yaml.safe_dump(document), encoding="utf-8")


def sources(claude_settings: dict[str, Any], codex_settings: dict[str, Any]) -> dict[str, Any]:
    return {"claude": {"app": "claude-code", "source_id": "pc-claude", **claude_settings},
            "codex": {"app": "codex", "source_id": "pc-codex", **codex_settings}}


def run(path: Path, tmp_path: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    result = link_actions(load_app_config(explicit_path=path, cwd=tmp_path), dry_run=True, as_of=AS_OF)
    rows = json.loads(Path(result["reportJsonPath"]).read_text("utf-8"))["conversations"]
    return result, {row["sessionId"]: row for row in rows}


def evidence(row: dict[str, Any]) -> list[str]:
    return sorted(item.get("evidenceId", "") for link in row["links"] for item in link["details"])


def without_location(row: dict[str, Any]) -> dict[str, Any]:
    return {**row, "digest": {k: v for k, v in row["digest"].items() if k not in {"source", "location"}}}


def test_app_mode_reads_the_application_folders_only(tmp_path: Path) -> None:
    path, _vault_root, archives = configure(tmp_path)
    _result, archived = run(path, tmp_path)
    apps = [mirror(archives[0], tmp_path / "apps" / "claude"), mirror(archives[1], tmp_path / "apps" / "codex")]
    rewrite(path, chat_sources=sources({"mode": "app", "root": str(apps[0])}, {"root": str(apps[1])}))
    before = tree(tmp_path / "apps")
    for archive in archives:  # The archives are not read in app mode.
        (archive / ".tkn-genai-chat-archive.json").unlink()

    result, rows = run(path, tmp_path)

    assert tree(tmp_path / "apps") == before
    assert result["readFrom"] == {"app": 7, "archive": 0}
    assert [source["read"] for source in result["sources"]] == [["app"], ["app"]]
    assert {key: without_location(row) for key, row in rows.items()} == {
        key: without_location(row) for key, row in archived.items()}
    assert {row["digest"]["location"] for row in rows.values()} == {"app"}
    assert rows[THREAD]["title"] == "Check the importer"  # session_index.jsonl of the Codex folder.


def test_evidence_ids_follow_the_decoded_content(tmp_path: Path) -> None:
    path, _vault_root, archives = configure(tmp_path)
    _result, archived = run(path, tmp_path)
    app = mirror(archives[1], tmp_path / "apps" / "codex", decode=True)  # `.jsonl`, not `.jsonl.zst`.
    assert (app / ROLLOUT[: -len(".zst")]).is_file() and not (app / ROLLOUT).exists()
    rewrite(path, chat_sources=sources({"mode": "archive", "archive": str(archives[0])}, {"root": str(app)}))

    _result, rows = run(path, tmp_path)

    codex = rows[THREAD]
    assert codex["digest"]["relativePath"] == ROLLOUT[: -len(".zst")]
    assert codex["digest"]["contentSha256"] == archived[THREAD]["digest"]["contentSha256"]
    assert evidence(codex) == evidence(archived[THREAD])
    sha12 = codex["digest"]["contentSha256"][:12]
    assert all(item.startswith(f"codex/pc-codex/{THREAD}@{sha12}#L") for item in evidence(codex))


def test_auto_prefers_the_application_folder_and_fills_in_from_the_archive(tmp_path: Path) -> None:
    path, _vault_root, archives = configure(tmp_path)
    cwd = "C:\\path\\to\\other-repo"
    claude_app = tmp_path / "apps" / "claude"
    folder = claude_app / "projects" / "C--work"
    folder.mkdir(parents=True)
    # S2 has a newer turn in the application's folder; the other conversations exist only in the archive.
    (folder / f"{S2}.jsonl").write_bytes(lines(
        claude("user", "Fix the build", S2, cwd, "2026-11-03T00:00:00Z", turnOrigin="human"),
        claude("user", "And the tests", S2, cwd, "2026-11-04T00:00:00Z", turnOrigin="human"),
    ))
    # Codex moved the rollout to archived_sessions and keeps it uncompressed there.
    codex_app = tmp_path / "apps" / "codex"
    view = ArchiveView(archives[1])
    version = view.latest(ROLLOUT)
    assert version is not None
    moved = codex_app / "archived_sessions" / Path(ROLLOUT).name[: -len(".zst")]
    moved.parent.mkdir(parents=True)
    moved.write_bytes(view.read_content(version))
    rewrite(path, chat_sources=sources(
        {"mode": "auto", "root": str(claude_app), "archive": str(archives[0])},
        {"mode": "auto", "root": str(codex_app), "archive": str(archives[1])}))
    before = {folder: tree(folder) for folder in (claude_app, codex_app, *archives)}

    result, rows = run(path, tmp_path)

    assert {folder: tree(folder) for folder in (claude_app, codex_app, *archives)} == before
    assert rows[S2]["digest"]["location"] == "app" and rows[S2]["humanRequestCount"] == 2
    assert rows[S1]["digest"]["location"] == "archive"
    assert rows[THREAD]["digest"]["location"] == "app"
    assert rows[THREAD]["digest"]["relativePath"] == f"archived_sessions/{moved.name}"
    assert result["duplicateSessions"] == 0 and result["conversationCount"] == 5
    assert result["readFrom"] == {"app": 2, "archive": 5}


def test_auto_without_the_application_folder_reads_the_archive(tmp_path: Path) -> None:
    path, _vault_root, archives = configure(tmp_path)
    missing = tmp_path / "apps" / "missing"
    rewrite(path, chat_sources=sources(
        {"mode": "auto", "root": str(missing), "archive": str(archives[0])},
        {"mode": "auto", "root": str(missing), "archive": str(archives[1])}))

    result, _rows = run(path, tmp_path)

    assert result["conversationCount"] == 5 and result["readFrom"] == {"app": 0, "archive": 7}
    assert [note["reason"] for source in result["sources"] for note in source["notes"]] == ["folder-missing"] * 2
    assert not missing.exists()


def test_app_mode_without_the_folder_skips_the_source(tmp_path: Path) -> None:
    path, _vault_root, archives = configure(tmp_path)
    rewrite(path, chat_sources=sources({"mode": "archive", "archive": str(archives[0])},
                                       {"root": str(tmp_path / "apps" / "missing")}))
    result, _rows = run(path, tmp_path)
    assert [(item["source"], item["reason"]) for item in result["skippedSources"]] == [("codex", "unreadable")]
    assert result["conversationCount"] == 4


def cache_files(root: Path) -> list[Path]:
    return sorted((root / "work" / "cache" / "digests").rglob("*.json"))


def test_digests_are_a_cache_rebuilt_when_content_or_settings_change(tmp_path: Path) -> None:
    path, _vault_root, archives = configure(tmp_path)
    app = mirror(archives[0], tmp_path / "apps" / "claude")
    rewrite(path, chat_sources=sources({"mode": "app", "root": str(app)},
                                       {"mode": "archive", "archive": str(archives[1])}))
    first, _rows = run(path, tmp_path)
    assert first["digests"] == {"built": 7} and first["digestCache"] is True
    files = cache_files(tmp_path)
    assert len(files) == 7 and {p.parent.name for p in files} == {"pc-claude", "pc-codex"}
    assert not (tmp_path / "work" / "data" / "digests").exists()
    digest = json.loads(files[0].read_text(encoding="utf-8"))
    assert digest["schemaVersion"] == "2.2.0" and set(digest["file"]) == {
        "app", "sourceId", "relativePath", "contentSha256", "lineCount"}

    second, _rows = run(path, tmp_path)
    assert second["digests"] == {"cached": 7}

    transcript = app / "projects" / "C--work" / f"{S2}.jsonl"
    transcript.write_bytes(transcript.read_bytes() + lines(
        claude("user", "One more thing", S2, "C:\\path\\to\\other-repo", "2026-11-03T01:00:00Z",
               turnOrigin="human")))
    third, rows = run(path, tmp_path)
    assert third["digests"] == {"built": 1, "cached": 6} and rows[S2]["humanRequestCount"] == 2

    rewrite(path, utc_offset_minutes=0)
    fourth, _rows = run(path, tmp_path)
    assert fourth["digests"] == {"built": 7}


def test_cache_false_keeps_no_digest_files(tmp_path: Path) -> None:
    path, _vault_root, _archives = configure(tmp_path, digests={"cache": False})
    first, _rows = run(path, tmp_path)
    second, _rows = run(path, tmp_path)
    assert first["digests"] == second["digests"] == {"built": 7}
    assert second["digestCache"] is False
    assert not (tmp_path / "work" / "cache").exists()


def test_same_content_in_two_files_is_digested_once(tmp_path: Path) -> None:
    path, _vault_root, archives = configure(tmp_path)
    view = ArchiveView(archives[1])
    version = view.latest(ROLLOUT)
    assert version is not None
    content = view.read_content(version)
    later = ArchiveRun(archives[1], "pc-codex", "20261105T030000+0900", "2026-11-05T03:00:00+09:00")
    later.version(ROLLOUT[: -len(".zst")], content)
    later.commit()

    result, rows = run(path, tmp_path)

    assert result["digests"].get("same-content") == 1 and result["duplicateSessions"] == 0
    assert rows[THREAD]["digest"]["relativePath"] == ROLLOUT  # The earliest captured copy.


def test_work_folders_must_not_overlap_an_application_folder(tmp_path: Path) -> None:
    path, _vault_root, archives = configure(tmp_path)
    app = mirror(archives[0], tmp_path / "apps" / "claude")
    rewrite(path, chat_sources=sources({"root": str(app)}, {"mode": "archive", "archive": str(archives[1])}),
            cache_root=str(app / "cache"))
    before = tree(app)
    with pytest.raises(PipelineError, match="must not overlap an application's folder"):
        run(path, tmp_path)
    assert tree(app) == before


@pytest.mark.parametrize(("entries", "message"), [
    ({"a": {"app": "codex", "source_id": "pc", "mode": "archive"}}, "needs archive"),
    ({"a": {"app": "codex", "source_id": "pc", "mode": "auto", "root": "~/.codex"}}, "needs archive"),
    ({"a": {"app": "codex", "source_id": "pc"}, "b": {"app": "codex", "source_id": "PC"}}, "both name codex"),
    ({"a": {"app": "copilot", "source_id": "pc"}}, "chat_sources.a.app"),
    ({"a": {"app": "codex", "source_id": "../pc"}}, "source_id must use only"),
    ({"a": {"app": "codex", "source_id": "pc", "mode": "everything"}}, "chat_sources.a.mode"),
    ({"a": {"app": "codex", "source_id": "pc", "root": " "}}, "must not be empty"),
])
def test_invalid_chat_sources_are_rejected(tmp_path: Path, entries: dict[str, Any], message: str) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"schema_version": "12.0.0", "chat_sources": entries}), encoding="utf-8")
    with pytest.raises(PipelineError, match=message):
        load_app_config(explicit_path=path, cwd=tmp_path)


def test_chat_source_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"schema_version": "12.0.0", "chat_sources": {
        "codex": {"app": "codex", "source_id": "pc"},
        "claude": {"app": "claude-code", "source_id": "pc", "root": "apps/claude"},
        "archived": {"app": "codex", "source_id": "other", "mode": "archive", "archive": "archives/codex"},
    }}), encoding="utf-8")
    config = load_app_config(explicit_path=path, cwd=tmp_path)
    codex, claude_code, archived = (config.chat_sources[name] for name in ("codex", "claude", "archived"))
    assert codex.mode == "app" and codex.app_root == Path.home() / ".codex" and codex.enabled
    assert claude_code.app_root == tmp_path / "apps" / "claude"  # Relative to the config file.
    assert archived.archive == tmp_path / "archives" / "codex" and not archived.reads_app
    assert config.digests.cache is True


def test_no_chat_sources_is_an_error(tmp_path: Path) -> None:
    path, _vault_root, _archives = configure(tmp_path)
    rewrite(path, chat_sources={})
    with pytest.raises(PipelineError, match="no chat_sources"):
        run(path, tmp_path)


def test_a_truncated_compressed_file_is_reported_unreadable(tmp_path: Path) -> None:
    path, _vault_root, archives = configure(tmp_path)
    app = mirror(archives[1], tmp_path / "apps" / "codex")
    rollout = app / ROLLOUT
    rollout.write_bytes(zstandard.ZstdCompressor().compress(b'{"type": "session_meta"}\n' * 50)[:-6])
    rewrite(path, chat_sources=sources({"mode": "archive", "archive": str(archives[0])}, {"root": str(app)}))
    result, _rows = run(path, tmp_path)
    assert [item["relativePath"] for item in result["unreadableFiles"]] == [ROLLOUT]
    assert result["unreadableFiles"][0]["location"] == "app"


@pytest.mark.parametrize("entry", [
    {"mode": "archive", "root": "APP", "archive": "ARCHIVE"},  # The root is not read in this mode.
    {"mode": "app", "root": "OTHER", "archive": "APP"},  # Nor is the archive.
    {"mode": "app", "root": "APP", "enabled": False},  # A disabled entry still names a protected folder.
])
def test_every_configured_location_is_protected(tmp_path: Path, entry: dict[str, Any]) -> None:
    path, _vault_root, archives = configure(tmp_path)
    app = mirror(archives[0], tmp_path / "apps" / "claude")
    places = {"APP": str(app), "ARCHIVE": str(archives[0]), "OTHER": str(tmp_path / "apps" / "other")}
    claude_settings = {key: places.get(value, value) if isinstance(value, str) else value
                       for key, value in entry.items()}
    rewrite(path, chat_sources=sources(claude_settings, {"mode": "archive", "archive": str(archives[1])}),
            processes={"data_root": str(tmp_path / "work" / "data"), "state_root": str(app / "state")})
    before = tree(app)
    with pytest.raises(PipelineError, match="must not overlap"):
        run(path, tmp_path)
    assert tree(app) == before


def test_auto_prefers_the_application_copy_after_the_project_folder_changed(tmp_path: Path) -> None:
    path, _vault_root, archives = configure(tmp_path)
    cwd = "C:\\path\\to\\renamed-repo"
    # The archive kept a longer copy under the old project folder; the application now keeps the
    # session under a new folder (for example after the working folder was renamed).
    run_ = ArchiveRun(archives[0], "pc-claude", "20261105T030000+0900", "2026-11-05T03:00:00+09:00")
    run_.version(f"projects/C--old/{S2}.jsonl", lines(
        claude("user", "Old first", S2, cwd, "2026-11-03T00:00:00Z", turnOrigin="human"),
        claude("user", "Old second", S2, cwd, "2026-11-03T00:01:00Z", turnOrigin="human"),
        claude("user", "Old third", S2, cwd, "2026-11-03T00:02:00Z", turnOrigin="human"),
    ))
    run_.commit()
    app = tmp_path / "apps" / "claude"
    folder = app / "projects" / "C--new"
    folder.mkdir(parents=True)
    (folder / f"{S2}.jsonl").write_bytes(lines(
        claude("user", "New only", S2, cwd, "2026-11-03T00:00:00Z", turnOrigin="human")))
    rewrite(path, chat_sources=sources({"mode": "auto", "root": str(app), "archive": str(archives[0])},
                                       {"mode": "archive", "archive": str(archives[1])}))

    _result, rows = run(path, tmp_path)

    assert rows[S2]["digest"]["location"] == "app" and rows[S2]["firstRequest"] == "New only"
    assert rows[S2]["digest"]["relativePath"] == f"projects/C--new/{S2}.jsonl"
    assert rows[S1]["digest"]["location"] == "archive"  # Other sessions still come from the archive.
    assert rows[S1]["subagentCount"] == 1


def test_a_codex_digest_is_rebuilt_when_the_thread_name_differs(tmp_path: Path) -> None:
    path, _vault_root, archives = configure(tmp_path)
    first, rows = run(path, tmp_path)  # Warm the cache from the archive.
    assert rows[THREAD]["title"] == "Check the importer"
    app = mirror(archives[1], tmp_path / "apps" / "codex")
    (app / "session_index.jsonl").write_bytes(lines({"id": THREAD, "thread_name": "Renamed in the app"}))
    rewrite(path, chat_sources=sources({"mode": "archive", "archive": str(archives[0])}, {"root": str(app)}))

    second, rows = run(path, tmp_path)

    assert rows[THREAD]["title"] == "Renamed in the app" and rows[THREAD]["digest"]["location"] == "app"
    assert second["digests"] == {"cached": 6, "built": 1}
    third, _rows = run(path, tmp_path)
    assert third["digests"] == {"cached": 7}
