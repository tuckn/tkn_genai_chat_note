from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from test_pipeline_workflow import config_for

from tkn_genai_chat_note import inference
from tkn_genai_chat_note.cli import main
from tkn_genai_chat_note.config import load_app_config
from tkn_genai_chat_note.generation_usage import usage_totals
from tkn_genai_chat_note.report_settings import PriceScenario, UsageReportSettings
from tkn_genai_chat_note.runtime import PipelineError, atomic_write_json
from tkn_genai_chat_note.usage_records import UsageJournal, new_record
from tkn_genai_chat_note.usage_report import build_usage_report, collect_usage, reference_cost


def stream(**usage):
    return "\n".join(
        json.dumps(value)
        for value in [
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"text": "private-answer"}},
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 1000,
                    "output_tokens": 200,
                    "cached_input_tokens": 600,
                    "reasoning_output_tokens": 150,
                    **usage,
                },
            },
        ]
    )


def record(**overrides):
    return {
        **new_record("codex", "example-model", "high"),
        "schemaVersion": 1,
        "runId": "run-one",
        "threadId": "thread-one",
        "sourceId": "windows",
        "command": "clone",
        "generationProfile": "codex",
        "noteStatus": "generated",
        "startedAt": "2026-09-19T23:30:00+00:00",
        "finishedAt": "2026-09-19T23:31:00+00:00",
        "inputTokens": 1000,
        "outputTokens": 200,
        "cachedInputTokens": 600,
        "reasoningTokens": 150,
        "status": "received",
        **overrides,
    }


def saved(tmp_path, **overrides):
    cfg = config_for(tmp_path)
    cfg.report_path = tmp_path / "reports"
    row = record(**overrides)
    path = cfg.state_root / "usage" / row["runId"] / (row["usageId"] + ".json")
    atomic_write_json(path, row)
    return cfg, row, path


def test_journal_persists_started_attempt_before_generation(tmp_path):
    row = new_record("codex", "model", "high")
    journal = UsageJournal(tmp_path, {"runId": "run", "threadId": "thread", "sourceId": "windows"})
    journal({"type": "usage-start", **row})
    persisted = json.loads((tmp_path / (row["usageId"] + ".json")).read_text())
    assert persisted["status"] == "started" and persisted["inputTokens"] is None
    row.update(inputTokens=1000, outputTokens=200, usageComplete=True)
    journal({"type": "usage-complete", **row, "status": "received"})
    journal.finish("failed")  # The response was received but the final note could not be published.
    assert len(list(tmp_path.glob("*.json"))) == 1
    persisted = json.loads((tmp_path / (row["usageId"] + ".json")).read_text())
    assert persisted["noteStatus"] == "failed" and persisted["inputTokens"] == 1000


def test_report_deduplicates_journal_run_report_and_compatibility_view(tmp_path):
    cfg, row, _ = saved(tmp_path)
    report = {
        "runId": row["runId"],
        "mode": "clone",
        "generationProvider": "codex",
        "startedAt": row["startedAt"],
        "threads": [
            {
                "threadId": row["threadId"],
                "generated": True,
                "generationMetrics": {"usageRecords": [row], "apiRequests": [row], "modelCalls": 1},
            }
        ],
    }
    atomic_write_json(cfg.reports_root / "one.json", report)
    atomic_write_json(cfg.state_root / "last-run.json", report)
    data = collect_usage(cfg)
    assert len(data["records"]) == 1 and len(data["notes"]) == 1
    assert usage_totals(data["records"])["inputTokens"] == 1000
    assert len(data["sources"]) == 2
    # A long run may start the previous day: use the attempt date for this note.
    report["startedAt"] = "2026-09-18T00:00:00+00:00"
    atomic_write_json(cfg.reports_root / "one.json", report)
    assert collect_usage(cfg)["notes"][0]["date"] == "2026-09-19"


def test_legacy_api_and_unknown_cli_attempts_are_preserved(tmp_path):
    cfg = config_for(tmp_path)
    atomic_write_json(
        cfg.reports_root / "one.json",
        {
            "runId": "old",
            "mode": "pull",
            "startedAt": "2026-09-18T10:00:00+09:00",
            "threads": [
                {"threadId": "api", "generationMetrics": {"apiRequests": [{"inputTokens": 50, "outputTokens": 20}]}},
                {"threadId": "cli", "generationMetrics": {"modelCalls": 2}},
            ],
        },
    )
    data = collect_usage(cfg)
    totals = usage_totals(data["records"])
    assert len(data["records"]) == 3 and totals["inputTokens"] is None
    assert totals["knownInputTokens"] == 50 and totals["inputTokensMissingRequests"] == 2


def test_report_dry_run_never_writes_opens_or_infers(tmp_path, monkeypatch):
    cfg, _, _ = saved(tmp_path)
    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    monkeypatch.setattr("webbrowser.open", lambda *a: pytest.fail("browser opened"))
    monkeypatch.setattr(inference, "invoke_structured", lambda *a, **k: pytest.fail("inference"))
    result = build_usage_report(cfg, dry_run=True)
    assert result["recordCount"] == 1 and not result["opened"]
    assert before == {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


def test_report_outputs_are_offline_safe_and_timezone_aware(tmp_path, monkeypatch):
    cfg, _, _ = saved(tmp_path, threadId="</script><script>alert(1)</script>", requestedModel="=bad()")
    cfg.usage_report = UsageReportSettings(utc_offset_minutes=540)
    opened = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url) or True)
    result = build_usage_report(cfg, no_open=True)
    html = Path(result["htmlPath"]).read_text(encoding="utf-8")
    data = json.loads(Path(result["jsonPath"]).read_text(encoding="utf-8"))
    assert data["records"][0]["date"] == "2026-09-20"
    assert data["records"][0]["threadId"] == "</script><script>alert(1)</script>"
    assert "</script><script>alert(1)</script>" not in html
    assert "__USAGE_" not in html and "__STATIC_SUMMARY__" not in html
    assert "connect-src 'none'" in html and "<script src=" not in html
    assert "'=bad()" in Path(result["csvPath"]).read_text(encoding="utf-8")
    assert not opened
    assert build_usage_report(cfg)["opened"]
    assert opened == [Path(result["htmlPath"]).as_uri()]


def test_empty_report_is_explicit(tmp_path):
    cfg = config_for(tmp_path)
    cfg.report_path = tmp_path / "report"
    result = build_usage_report(cfg, no_open=True)
    assert result["recordCount"] == 0
    assert "該当する使用量記録がありません" in Path(result["htmlPath"]).read_text(encoding="utf-8")


@pytest.mark.parametrize("destination", ["state", "raw", "data", "codex", "cache", "."])
def test_report_cannot_overwrite_source_or_owned_data(tmp_path, destination):
    cfg = config_for(tmp_path)
    cfg.report_path = tmp_path / destination
    with pytest.raises(PipelineError, match="separate"):
        build_usage_report(cfg)


def test_foreign_report_directory_and_corrupt_evidence_are_rejected(tmp_path):
    cfg, _, path = saved(tmp_path)
    cfg.report_path.mkdir()
    (cfg.report_path / "index.html").write_text("user content")
    with pytest.raises(PipelineError, match="application-owned"):
        build_usage_report(cfg)
    assert (cfg.report_path / "index.html").read_text() == "user content"
    cfg.report_path = tmp_path / "fresh"
    path.write_text("{broken")
    with pytest.raises(PipelineError, match="usage evidence"):
        build_usage_report(cfg)
    assert not cfg.report_path.exists()


def price(**overrides):
    return PriceScenario(pricing_date="2026-09-19", input_per_million=2, output_per_million=10, **overrides)


def test_cost_scenarios_handle_cache_and_never_add_reasoning_twice():
    row = record(cacheWriteTokens=100)
    assert reference_cost(row, price()) == pytest.approx(0.004)
    cached = price(cache_policy="observed", cached_input_per_million=0.2)
    assert reference_cost(row, cached) == pytest.approx(0.00292)
    assert reference_cost(
        row, price(cache_policy="observed", cached_input_per_million=0.2, cache_write_per_million=3)
    ) == pytest.approx(0.00302)
    assert reference_cost({**row, "cachedInputTokens": None}, cached) is None
    assert reference_cost({**row, "outputTokens": None}, price()) is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"input_per_million": -1},
        {"output_per_million": float("nan")},
        {"cache_policy": "observed"},
        {"pricing_date": "unknown"},
        {"currency": "invalid"},
    ],
)
def test_invalid_prices_rejected(overrides):
    value = {"pricing_date": "2026-09-19", "input_per_million": 2, "output_per_million": 10, **overrides}
    with pytest.raises(ValueError):
        PriceScenario(**value)


def test_new_settings_resolve_relative_to_config_and_accept_old_schema(tmp_path, monkeypatch):
    monkeypatch.setattr("tkn_genai_chat_note.config.global_config_path", lambda: tmp_path / "absent")
    path = tmp_path / "config.yaml"
    path.write_text('schema_version: "12.0.0"\nreport_path: reports\nusage_report:\n  utc_offset_minutes: 540\n')
    before = path.read_bytes()
    cfg = load_app_config(explicit_path=path, cwd=tmp_path)
    assert cfg.report_path == tmp_path / "reports"
    assert cfg.usage_report.utc_offset_minutes == 540
    assert path.read_bytes() == before


def test_build_report_cli_routes_without_running_pipeline(tmp_path, monkeypatch, capsys):
    cfg, _, _ = saved(tmp_path)
    monkeypatch.setattr("tkn_genai_chat_note.cli.load_app_config", lambda **kw: cfg)
    monkeypatch.setattr("tkn_genai_chat_note.pipeline.run_pipeline", lambda *a, **k: pytest.fail("pipeline run"))
    assert main(["--json", "build-report", "--dry-run"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["dryRun"] and result["recordCount"] == 1


def test_review_records_usage_and_repeated_build_does_not_double_count(tmp_path, monkeypatch):
    from datetime import date

    from chat_fixtures import write_chat
    from test_review_sources import response
    from tkn_genai_bridge import Runtime, Usage
    from tkn_genai_bridge.providers.base import ProviderResponse

    from tkn_genai_chat_note.pipeline import run_pipeline
    from tkn_genai_chat_note.reviews import run_reviews

    cfg = config_for(tmp_path)
    write_chat(cfg.sessions_root / "one.jsonl", thread_id="one", cwd=tmp_path,
               started_at="2026-05-19T00:00:00Z")

    class Backend:
        def generate(self, profile, request):
            return ProviderResponse(data=response(request.prompt, {}),
                                    usage=Usage(input_tokens=1000, output_tokens=200))

    monkeypatch.setattr(inference, "Runtime", lambda profile, **kw: Runtime(profile, backend=Backend(), **kw))
    assert run_pipeline(cfg, mode="clone")["complete"]
    assert collect_usage(cfg)["records"] == []
    options = {"kind": "weekly", "day": date(2026, 5, 19), "as_of": date(2026, 5, 25)}
    report = run_reviews(cfg, **options)
    assert report["ok"] and report["generatedCount"] == 1, report
    records = collect_usage(cfg)["records"]
    assert len(records) == 3  # extraction, composition and source audit
    assert sum(r["inputTokens"] for r in records) == 3000
    assert all(r["noteStatus"] == "generated" for r in records)
    assert records[0]["inputTokens"] == 1000
    assert records[0]["noteStatus"] == "generated"
    assert records[0]["sourceId"] == cfg.source_id
    assert run_pipeline(cfg, mode="pull")["complete"]
    assert run_reviews(cfg, **options)["modelCalls"] == 0
    assert len(collect_usage(cfg)["records"]) == 3


def save_report(cfg, row, entry, **extra):
    report = {
        "runId": row["runId"], "mode": "pull", "generationProvider": "codex",
        "generationProfile": "codex", "startedAt": row["startedAt"], "threads": [entry], **extra,
    }
    atomic_write_json(cfg.reports_root / "report.json", report)
    return report


def test_titles_are_read_from_current_frontmatter_without_exporting_body(tmp_path):
    cfg, row, _ = saved(tmp_path)
    note = cfg.data_root / "session-notes" / "summary.md"
    note.parent.mkdir(parents=True)
    note.write_text('---\ntitle: >-\n  Reviewed title with\n  日本語\n---\nPRIVATE NOTE BODY\n', encoding="utf-8")
    entry = {"threadId": row["threadId"], "title": "Historical title", "noteRef": "data:/session-notes/summary.md",
             "generated": True, "generationMetrics": {"usageRecords": [row]}}
    report = save_report(cfg, row, entry)
    atomic_write_json(cfg.state_root / "last-run.json", report)
    data = collect_usage(cfg)
    assert len(data["records"]) == 1
    assert data["records"][0]["noteFile"] == "summary.md"
    assert data["records"][0]["taskTitle"] == "Historical title"
    assert data["records"][0]["displayTitle"] == "Reviewed title with 日本語"
    assert data["notes"][0]["titleSource"] == "frontmatter-current"
    assert "PRIVATE NOTE BODY" not in json.dumps(data)
    assert len([s for s in data["sources"] if s.get("scope") == "frontmatter-utf8"]) == 1
    note.unlink()
    data = collect_usage(cfg)
    assert data["records"][0]["displayTitle"] == "Historical title"
    assert data["records"][0]["noteFile"] == "summary.md"
    assert any(d["category"] == "note-metadata" for d in data["diagnostics"])


@pytest.mark.parametrize("ref", [
    "data:/../private.md", "data:/C:/private.md", "data:/session-notes/../../../private.md",
])
def test_note_references_cannot_read_outside_data(tmp_path, ref):
    cfg, row, _ = saved(tmp_path)
    (tmp_path / "private.md").write_text("---\ntitle: SECRET\n---\n", encoding="utf-8")
    save_report(cfg, row, {"threadId": row["threadId"], "title": "Safe fallback", "noteRef": ref})
    data = collect_usage(cfg)
    assert data["records"][0]["displayTitle"] == "Safe fallback"
    assert "SECRET" not in json.dumps(data)
    assert any(d["category"] == "note-metadata" for d in data["diagnostics"])


def test_diagnostics_include_recovered_validation_and_failures_without_usage(tmp_path):
    cfg, row, _ = saved(tmp_path, status="rejected", httpStatus=429)
    entry = {"threadId": row["threadId"], "generated": True, "title": "Recovered note", "generationMetrics": {
        "usageRecords": [row], "validationFailures": [
            {"stage": "chunk", "attempt": 1, "reason": "invalid anchor"},
            {"stage": "chunk", "attempt": 1, "reason": "invalid anchor"}], "transportRetries": 1}}
    report = save_report(cfg, row, entry, warnings=["Missing application metadata"],
                         failed=[{"stage": "raw", "error": "cannot read capture", "sourceRef": "source:/missing"}],
                         rawIngest={"failed": [{"stage": "raw", "error": "cannot read capture",
                                               "sourceRef": "source:/missing"}]})
    report["threads"].extend([
        {"threadId": "budget", "status": "deferred", "generationStop": {"reason": "api-cost-budget",
                                                                          "message": "No request submitted"}},
        {"threadId": "deadline", "status": "deferred", "reason": "runtime-deadline"},
        {"threadId": "broken", "status": "failed", "error": "Cannot load note"},
    ])
    atomic_write_json(cfg.reports_root / "report.json", report)
    atomic_write_json(cfg.state_root / "last-run.json", report)
    data = collect_usage(cfg)
    rows = data["diagnostics"]
    assert len(data["records"]) == 1
    assert len([d for d in rows if d["message"] == "cannot read capture"]) == 1
    validation = [d for d in rows if d["category"] == "validation"]
    assert len(validation) == 2 and validation[0]["diagnosticId"] != validation[1]["diagnosticId"]
    assert all(d["severity"] == "WARNING" and d["noteStatus"] == "generated" for d in validation)
    assert all(d["models"] == ["example-model"] for d in validation)
    assert any(d["message"] == "Cannot load note" and d["models"] == ["unknown"] for d in rows)
    assert any(d["category"] == "budget-stop" and "No request submitted" in d["message"] for d in rows)
    assert any(d["severity"] == "INFO" and d["message"] == "runtime-deadline" for d in rows)
    assert any("HTTP 429" in d["message"] for d in rows)
    assert len([d for d in rows if d["category"] == "attempt"]) == 1


def test_diagnostic_export_is_safe_and_dry_run_stays_read_only(tmp_path, monkeypatch):
    cfg, row, _ = saved(tmp_path)
    warning = '</script><script>alert("x")</script>'
    save_report(cfg, row, {"threadId": row["threadId"], "title": "=HYPERLINK()", "status": "failed",
                          "error": "=PRIVATE_ERROR()"}, warnings=[warning])
    monkeypatch.setattr("webbrowser.open", lambda *a: pytest.fail("browser opened"))
    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert build_usage_report(cfg, dry_run=True)["diagnosticCount"] == 2
    assert before == {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result = build_usage_report(cfg, no_open=True)
    html = Path(result["htmlPath"]).read_text(encoding="utf-8")
    assert warning not in html
    csv = Path(result["diagnosticsCsvPath"]).read_text(encoding="utf-8")
    assert "'=PRIVATE_ERROR()" in csv and "'=HYPERLINK()" in csv
    assert result["diagnosticCount"] == 2


def test_usage_report_interactions():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for report interaction tests")
    result = subprocess.run([node, "--test", str(Path(__file__).with_name("usage_report_ui.cjs"))],
                            capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
