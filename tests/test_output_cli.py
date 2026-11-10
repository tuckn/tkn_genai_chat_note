"""The final-result contract is independent of dispatch, detail and logging."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tkn_genai_chat_note import cli
from tkn_genai_chat_note.config import load_app_config
from tkn_genai_chat_note.output import public_result, with_target_paths

COPY_PATH = r"C:\path to\日本語" + "\\long-folder" * 30 + "\\file.json"


@pytest.fixture
def fake_commands(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    import tkn_genai_chat_note.pipeline as pipeline
    import tkn_genai_chat_note.provenance as provenance
    import tkn_genai_chat_note.reviews as reviews
    import tkn_genai_chat_note.usage_report as usage

    value = {"ok": True, "complete": True, "status": "success", "threadCounts": {}, "failed": [],
             "configPath": COPY_PATH, "backupPath": None,
             "generatedCount": 1, "htmlPath": COPY_PATH, "targetPath": COPY_PATH,
             "message": "a\r\n\t\x1b\u0085\u2028b", "dryRun": False, "threads": [], "results": []}
    config = load_app_config()
    monkeypatch.setattr(cli, "load_app_config", lambda **_: config)
    for module, name in (
        (pipeline, "run_pipeline"), (pipeline, "pipeline_status"), (reviews, "run_reviews"),
        (pipeline, "run_scheduled"), (reviews, "validate_review"), (provenance, "validate_provenance"),
        (usage, "build_usage_report"), (cli, "initialize_user_config"),
    ):
        monkeypatch.setattr(module, name, lambda *a, **kw: copy.deepcopy(value))
    return value


@pytest.mark.parametrize("args", [
    ["clone"], ["pull"], ["raw", "ingest"], ["scheduled-run"], ["status"], ["build-report"],
    ["review-notes", "due"], ["review-notes", "build", "--period", "weekly"],
    ["review-notes", "validate", "review.json"], ["provenance", "validate"], ["config", "init"],
])
@pytest.mark.parametrize("json_mode", [False, True])
def test_every_command_emits_one_copyable_final_result(
    fake_commands: dict[str, Any], capsys: pytest.CaptureFixture[str], args: list[str], json_mode: bool,
) -> None:
    assert cli.main([*args, *(["--json"] if json_mode else [])]) == 0
    output = capsys.readouterr()
    if json_mode:
        result = json.loads(output.out)
        assert result["targetPath"] == COPY_PATH
        assert result["exitCode"] == 0 and result["status"] == "success"
    else:
        assert "targetPath=" + COPY_PATH in output.out.splitlines()
        assert "message=a\\r\\n\\t\\u001b\\u0085\\u2028b" in output.out.splitlines()
        assert "exitCode=0" in output.out.splitlines()
        assert all("=" in line for line in output.out.splitlines())
    assert "[INFO]" not in output.out and "[SUCCESS]" not in output.out and "\x1b" not in output.out


@pytest.mark.parametrize("position", range(4))
@pytest.mark.parametrize("flag", ["--json", "--full-output"])
def test_shared_output_flags_survive_every_subcommand_level(position: int, flag: str) -> None:
    args = ["raw", "ingest", "--quiet"]
    args.insert(position, flag)
    parsed = cli.build_parser().parse_args(args)
    assert getattr(parsed, "json" if flag == "--json" else "full_output")
    assert cli.build_parser().parse_args([flag, "raw", flag, "ingest", flag]).command == "raw"


@pytest.mark.parametrize("args", [[], ["review-notes", "build"], ["raw", "ingest", "--unknown"],
                                  ["--quiet", "status", "--verbose"],
                                  ["review-notes", "build", "--period", "weekly", "--date", "invalid"]])
@pytest.mark.parametrize("json_mode", [False, True])
def test_argument_failures_have_selected_output_and_original_exit_code(
    capsys: pytest.CaptureFixture[str], args: list[str], json_mode: bool,
) -> None:
    assert cli.main([*args, *(["--json"] if json_mode else [])]) == 2
    output = capsys.readouterr()
    if json_mode:
        assert json.loads(output.out)["errorType"] == "argument"
        assert json.loads(output.out)["status"] == "failed"
    else:
        assert "errorType=argument" in output.out.splitlines()
    assert "Traceback" not in output.err and "[ERROR]" in output.err


@pytest.mark.parametrize("json_mode", [False, True])
@pytest.mark.parametrize("exception,code", [
    (OSError(13, "access denied", COPY_PATH), 1), (ValueError("invalid configuration"), 1),
    (RuntimeError("runtime failure"), 1), (KeyboardInterrupt(), 130), (SystemExit(4), 1),
])
def test_runtime_errors_and_interruptions_emit_one_result(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    json_mode: bool, exception: BaseException, code: int,
) -> None:
    def fail(**_: Any) -> Any:
        raise exception

    monkeypatch.setattr(cli, "load_app_config", fail)
    assert cli.main(["status", *(["--json"] if json_mode else [])]) == code
    output = capsys.readouterr()
    if json_mode:
        result = json.loads(output.out)
        assert result["exitCode"] == code
        if isinstance(exception, OSError):
            assert result["targets"]["filename"] == COPY_PATH
    else:
        assert f"exitCode={code}" in output.out.splitlines()
        if isinstance(exception, OSError):
            assert "targets.filename=" + COPY_PATH in output.out.splitlines()
    assert "Traceback" not in output.err


@pytest.mark.parametrize("json_mode", [False, True])
def test_config_and_validation_errors_keep_target_paths_separate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], json_mode: bool,
) -> None:
    path = tmp_path / "invalid config.yaml"
    path.write_text('schema_version: "9.0.0"\nunknown: true\n', encoding="utf-8")
    for args, field, target in (
        (["config", "list", "--config", str(path)], "configPath", path),
        (["review-notes", "validate", str(tmp_path / "missing.json")], "artifactPath", tmp_path / "missing.json"),
    ):
        assert cli.main([*args, *(["--json"] if json_mode else [])]) == 1
        output = capsys.readouterr()
        if json_mode:
            assert json.loads(output.out)["targets"][field] == str(target)
        else:
            assert f"targets.{field}={target}" in output.out.splitlines()


@pytest.mark.parametrize("full", [False, True])
@pytest.mark.parametrize("json_mode", [False, True])
def test_partial_failure_and_details_are_independent_of_format(
    fake_commands: dict[str, Any], capsys: pytest.CaptureFixture[str], full: bool, json_mode: bool,
) -> None:
    fake_commands.update(ok=False, complete=False, failed=[{"sourcePath": COPY_PATH, "error": "cannot read"}],
                         threads=[{"status": "current", "threadId": "routine"},
                                  {"status": "blocked", "threadId": "attention", "reason": "edited",
                                   "sourcePath": COPY_PATH}])
    before = copy.deepcopy(fake_commands)
    args = ["clone", *(["--full-output"] if full else []), *(["--json"] if json_mode else [])]
    assert cli.main(args) == 1
    output = capsys.readouterr().out
    assert fake_commands == before
    if json_mode:
        result = json.loads(output)
        assert result["failed"][0]["sourcePath"] == COPY_PATH
        assert len(result["threads"]) == (2 if full else 1)
        assert result["threads"][-1]["reason"] == "edited"
        if not full:
            assert result["threadsCount"] == 2 and "--full-output" in result["detailsHint"]
    else:
        assert "failed[0].sourcePath=" + COPY_PATH in output.splitlines()
        assert ("threadId=routine" in output) == full
        assert "reason=edited" in output


@pytest.mark.parametrize("tty", [False, True])
@pytest.mark.parametrize("json_mode", [False, True])
@pytest.mark.parametrize("flags", [[], ["--quiet"], ["--verbose"]])
def test_tty_and_verbosity_only_change_logs(
    fake_commands: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    tty: bool, json_mode: bool, flags: list[str],
) -> None:
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: tty)
    args = ["clone", *(["--json"] if json_mode else [])]
    assert cli.main(args) == 0
    baseline = capsys.readouterr().out
    assert cli.main([*args, *flags]) == 0
    result = capsys.readouterr()
    assert result.out == baseline
    if "--quiet" in flags:
        assert result.err == ""


@pytest.mark.parametrize("json_mode", [False, True])
@pytest.mark.parametrize("full", [False, True])
@pytest.mark.parametrize("command", [["clone"], ["raw", "ingest"]])
def test_real_capture_dry_run_preserves_every_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], json_mode: bool, full: bool, command: list[str],
) -> None:
    from chat_fixtures import write_chat
    from test_pipeline_workflow import config_for

    from tkn_genai_chat_note.config import write_config

    config = config_for(tmp_path)
    path = tmp_path / "config.yaml"
    write_config(config, path)
    write_chat(config.sessions_root / "one.jsonl", thread_id="one", cwd=tmp_path)

    def snapshot() -> dict[Path, bytes | None]:
        return {p: p.read_bytes() if p.is_file() else None for p in tmp_path.rglob("*")}

    before = snapshot()
    assert cli.main([*command, "--config", str(path), "--dry-run", *(["--json"] if json_mode else []),
                     *(["--full-output"] if full else [])]) == 0
    output = capsys.readouterr().out
    assert json.loads(output)["dryRun"] if json_mode else "dryRun=true" in output.splitlines()
    assert snapshot() == before


def test_projection_preserves_history_issues_nested_sources_and_paths() -> None:
    value = {"sourceResults": [{"sourceId": "one", "capture": {"failed": [{"sourceRef": "sessions/a.jsonl",
                                                                                 "reason": "missing"}]},
                                "reviews": {"historyIssues": [{"threadId": "undated", "count": 3}],
                                            "results": [{"period": {"kind": "weekly", "start": "2026-05-18"},
                                                         "status": "failed", "reason": "invalid output"},
                                                        {"status": "skipped-empty"}]}}]}
    before = copy.deepcopy(value)
    roots = {"one": {"sourceRoot": "C:/example/source", "dataRoot": "C:/example/data"}}
    projected = public_result(with_target_paths(value, roots), full_output=False)
    source = projected["sourceResults"][0]
    assert source["capture"]["failed"][0]["sourcePaths"] == [str(Path("C:/example/source/sessions/a.jsonl"))]
    assert source["reviews"]["historyIssues"][0]["threadId"] == "undated"
    failure = source["reviews"]["results"][0]
    assert failure["jsonPath"] == str(Path("C:/example/data/reviews/weekly/2026/2026-W21.json"))
    assert failure["notePath"].endswith("2026-W21.md")
    assert source["reviews"]["resultsCounts"] == {"failed": 1, "skipped-empty": 1}
    assert value == before


@pytest.mark.parametrize("json_mode", [False, True])
def test_subprocess_pipe_and_redirect_use_explicit_format_only(tmp_path: Path, json_mode: bool) -> None:
    env = dict(os.environ, USERPROFILE=str(tmp_path / "user"), HOME=str(tmp_path / "user"),
               XDG_CACHE_HOME=str(tmp_path / "cache"), PYTHONIOENCODING="utf-8")
    args = [sys.executable, "-m", "tkn_genai_chat_note", "config", "list", "--quiet",
            *(["--json"] if json_mode else [])]
    piped = subprocess.run(args, cwd=tmp_path, env=env, capture_output=True, text=True, encoding="utf-8")
    assert piped.returncode == 0, piped.stderr
    redirected_path = tmp_path / "result.txt"
    with redirected_path.open("w", encoding="utf-8") as stream:
        redirected = subprocess.run(args, cwd=tmp_path, env=env, stdout=stream, stderr=subprocess.PIPE)
    assert redirected.returncode == 0
    assert redirected_path.read_text(encoding="utf-8") == piped.stdout
    if json_mode:
        assert json.loads(piped.stdout)["command"] == "config list"
    else:
        assert "command=config list" in piped.stdout.splitlines()
    assert not (tmp_path / "user").exists() and not (tmp_path / "cache").exists()


@pytest.mark.parametrize("args,expected", [(["--json", "--version"], "0.54.0"),
                                         (["raw", "ingest", "--json", "--help"], "--full-output")])
def test_help_and_version_keep_guidance_even_with_json(
    capsys: pytest.CaptureFixture[str], args: list[str], expected: str,
) -> None:
    with pytest.raises(SystemExit) as done:
        cli.main(args)
    assert done.value.code == 0
    assert expected in capsys.readouterr().out


def test_json_after_option_terminator_is_an_artifact_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    assert cli.main(["review-notes", "validate", "--", "--json"]) == 1
    result = capsys.readouterr().out
    assert "targets.artifactPath=" + str(tmp_path / "--json") in result.splitlines()
    assert "status=failed" in result.splitlines()
