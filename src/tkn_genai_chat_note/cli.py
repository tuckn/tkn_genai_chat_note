"""Command-line interface for Tkn GenAI Chat Note."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any, NoReturn, TypeVar, overload

from . import __version__
from .config import (
    config_document,
    global_config_path,
    initialize_user_config,
    load_app_config,
    resolve_app_config,
)
from .console_logging import ColorFormatter, ConsoleFilter, log_success, supports_color
from .output import emit, public_result, with_target_paths
from .runtime import PipelineError

LOGGER = logging.getLogger("tkn_genai_chat_note")
_NamespaceT = TypeVar("_NamespaceT")


def _utf8_console() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def _add_runtime_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true",
                        help="Print one JSON result instead of the default key=value list")
    parser.add_argument("--full-output", action="store_true",
                        help="Include every item in the final result (text or --json)")
    parser.add_argument("--config", type=Path, help="Explicit YAML config path")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--profile",
                           help="GenAI profile name under genai_profiles (default: generation.genai_profile)")
    selection.add_argument(
        "--provider",
        choices=("codex", "claude-code", "github-copilot", "antigravity", "ollama", "azure-openai"),
        help="Compatibility selector: requires a unique genai_profiles entry for this provider; prefer --profile",
    )
    parser.add_argument("--source", help="Select one enabled source_id from sources")
    parser.add_argument("--model")
    parser.add_argument(
        "--reasoning-effort",
        choices=("low", "medium", "high", "xhigh", "max", "ultra"),
    )
    parser.add_argument("--runtime-minutes", type=int)
    parser.add_argument("--model-timeout-seconds", type=int)
    parser.add_argument("--codex-executable")
    parser.add_argument("--claude-executable")
    parser.add_argument("--copilot-executable")
    parser.add_argument("--ollama-base-url")
    output = parser.add_mutually_exclusive_group()
    output.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress progress logs; errors are still shown",
    )
    output.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show detailed diagnostic logs",
    )


class _ArgumentFailure(SystemExit):
    """Keep argparse's exit code while allowing a formatted final error."""

    def __init__(self, message: str) -> None:
        super().__init__(2)
        self.message = message


class _CommandParser(argparse.ArgumentParser):
    """Expose shared options at every command level without resetting earlier values."""

    def __init__(self, *, runtime_defaults: bool = False, **kwargs: Any) -> None:
        super().__init__(argument_default=None if runtime_defaults else argparse.SUPPRESS, **kwargs)
        _add_runtime_options(self)
        self.argument_default = None

    def error(self, message: str) -> NoReturn:
        raise _ArgumentFailure(message)

    @overload
    def parse_args(self, args: Sequence[str] | None = None,
                   namespace: None = None) -> argparse.Namespace: ...

    @overload
    def parse_args(self, args: Sequence[str] | None, namespace: _NamespaceT) -> _NamespaceT: ...

    @overload
    def parse_args(self, *, namespace: _NamespaceT) -> _NamespaceT: ...

    def parse_args(self, args: Sequence[str] | None = None, namespace: Any = None) -> Any:
        result = super().parse_args(args, namespace)
        # argparse checks each parser's groups separately; also reject conflicts across levels.
        for left, right in (("profile", "provider"), ("quiet", "verbose")):
            if getattr(result, left, None) and getattr(result, right, None):
                self.error(f"argument --{right}: not allowed with argument --{left}")
        return result


def build_parser() -> argparse.ArgumentParser:
    parser = _CommandParser(
        runtime_defaults=True,
        prog="tkn-genai-chat-note",
        description="Read archived Codex and Claude Code chats and prepare proposals for existing notes. "
                    "Calendar reviews remain as manual commands.",
        epilog="Results: key=value on stdout, or --json. Progress/diagnostics: stderr. "
               "Exit codes: 0 success, 1 failure, 2 arguments/incomplete, 130 interrupted.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    config = commands.add_parser("config", help="Create or inspect configuration")
    sub = config.add_subparsers(dest="config_command", required=True)
    init = sub.add_parser("init", help="Create config.yaml; protect existing edits")
    init.add_argument("--force", action="store_true", help="Back up and replace an edited config")
    sub.add_parser(
        "list",
        help="List effective values and their sources",
        description="Read-only configuration listing as key=value lines; does not write files.",
    )
    for name, help_text in (
        ("clone", "Initialize and process all available history; resume safely when repeated"),
        ("pull", "Capture new/changed logs and refresh canonical events without inference"),
    ):
        command = commands.add_parser(
            name, help=help_text, description=help_text + ". Writes by default; no inference."
        )
        command.add_argument("--dry-run", action="store_true", help="Inspect without writes or inference")
    actions = commands.add_parser("actions", help="Match conversations with Action notes (stage 0: dry run only)")
    action_sub = actions.add_subparsers(dest="actions_command", required=True)
    link = action_sub.add_parser(
        "link",
        help="Match recent conversations to Action notes with clues A-D; write a report, never the Vault",
        description="Reads the conversations of chat_sources (application folders and/or tkn-genai-chat-archiver "
                    "archives, read only) and the Vault's Action notes. With --dry-run it writes only its report "
                    "under processes.state_root and the digest cache under <cache_root>/digests (none when "
                    "digests.cache is false). It never writes the Vault and never calls generative AI.",
    )
    link.add_argument("--dry-run", action="store_true", help="Required in this version: report only")
    link.add_argument("--days", type=int, help="Days back from --as-of (default: processes.actions.lookback_days)")
    link.add_argument("--as-of", type=date.fromisoformat, help="Last local date of the window (default: today)")
    ai_logs = commands.add_parser("ai-logs", help="Write the AI's diary of a day (stage 2a: no generative AI)")
    ai_logs_sub = ai_logs.add_subparsers(dest="ai_logs_command", required=True)
    ai_build = ai_logs_sub.add_parser(
        "build",
        help="Write the AI's diary of finished days to vault.ai_logs from that day's conversations",
        description="Reads the conversations of chat_sources (read only) and the Vault's Action notes, and writes "
                    "one diary note per local date below vault.ai_logs: the day's conversations with their linked "
                    "Actions and the counts of lookups, failures, repeats and Action rules. Counts go to "
                    "processes.data_root/ai-logs/metrics. A note the person changed is never rebuilt; new "
                    "conversations are appended instead. No generative AI is called. With --dry-run nothing is "
                    "written outside processes.state_root (previews and a report) and the digest cache.",
    )
    ai_build.add_argument("--dry-run", action="store_true",
                          help="Write previews below processes.state_root; leave the Vault and the metrics alone")
    ai_build.add_argument("--date", type=date.fromisoformat,
                          help="Last local date to write (default: yesterday; today only with --dry-run)")
    ai_build.add_argument("--days", type=int, default=1, help="Number of dates ending at --date (default: 1)")
    usage_report =commands.add_parser("build-report", help="Build an offline token usage and reference-cost report")
    usage_report.add_argument("--dry-run", action="store_true", help="Aggregate without writing or opening")
    usage_report.add_argument("--no-open", action="store_true", help="Write the report without opening a browser")
    scheduled = commands.add_parser(
        "scheduled-run", help="Daily unattended run: capture and normalize without inference (no reviews)")
    scheduled.add_argument("--dry-run", action="store_true", help="Plan without writes or inference")
    reviews = commands.add_parser("review-notes", help="Build source-cited weekly, monthly and yearly reviews")
    review_sub = reviews.add_subparsers(dest="review_command", required=True)
    review_build = review_sub.add_parser("build", help="Build a closed calendar period (default: latest closed)")
    _add_build_options(review_build)
    review_build.add_argument("--period", choices=("weekly", "monthly", "yearly"), required=True)
    review_build.add_argument("--date", type=date.fromisoformat, help="A date inside the requested closed period")
    review_due = review_sub.add_parser(
        "due", help="Catch up closed weekly, monthly and yearly periods across all saved history")
    _add_build_options(review_due)
    review_due.add_argument("--as-of", type=date.fromisoformat, help="Calendar date to evaluate (default: today)")
    review_validate = review_sub.add_parser("validate", help="Validate review JSON, periods and source IDs offline")
    review_validate.add_argument("artifact", type=Path)
    commands.add_parser("status", help="Show last-run coverage and pending work without a live source scan")
    provenance = commands.add_parser("provenance", help="Inspect downstream evidence integrity")
    provenance.add_subparsers(dest="provenance_command", required=True).add_parser(
        "validate",
        help="Read-only verification of artifact identities, snapshots, and activity relations",
    )
    raw = commands.add_parser("raw", help="Capture source bytes independently of inference")
    ingest = raw.add_subparsers(dest="raw_command", required=True).add_parser(
        "ingest", help="Write source copies and manifests (writes by default)"
    )
    ingest.add_argument("--dry-run", action="store_true", help="Inspect without writing")
    return parser


def _add_build_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dry-run", action="store_true", help="Validate and plan without inference or file writes")
    parser.add_argument(
        "--force", action="store_true", help="Re-evaluate unchanged inputs; does not override reviewed or edited files"
    )
    parser.add_argument("--allow-edited", action="store_true", help="Explicitly replace edited, unreviewed outputs")


def _overrides(args: argparse.Namespace) -> dict[str, Any]:
    names = (
        "profile",
        "provider",
        "model",
        "reasoning_effort",
        "runtime_minutes",
        "model_timeout_seconds",
        "codex_executable",
        "claude_executable",
        "copilot_executable",
        "ollama_base_url",
    )
    result = {name: getattr(args, name) for name in names if getattr(args, name, None) is not None}
    return result


def _configure_logging(args: argparse.Namespace) -> None:
    level = logging.DEBUG if args.verbose else logging.ERROR if args.quiet else logging.INFO
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(level)
    handler.addFilter(ConsoleFilter())
    handler.setFormatter(
        ColorFormatter(
            "[%(levelname)s] %(message)s",
            use_color=supports_color(sys.stderr),
        )
    )
    logging.basicConfig(level=level, handlers=[handler], force=True)


def _usage_summary(value: dict[str, Any]) -> str:
    parts = []
    for key, label in (("inputTokens", "input"), ("outputTokens", "output")):
        number = value.get(key)
        parts.append(f"{label} {number:,} tokens" if isinstance(number, int) else f"{label} tokens unknown")
    cost = value.get("estimatedCostJpy")
    if isinstance(cost, int | float):
        parts.append(f"estimated JPY {cost:.2f}")
    elif value.get("requestCount", 1):
        parts.append("cost unknown")
    return ", ".join(parts)


def _progress(event: dict[str, Any]) -> None:
    if event.get("type") == "source-start":
        LOGGER.info("Source: %s", event.get("sourceId", ""))


def _run(args: argparse.Namespace, pending: list[dict[str, Any]], targets: dict[str, Any]) -> int:
    def save(value: dict[str, Any]) -> None:
        pending[:] = [value]

    if args.command == "config":
        if args.config_command == "init":
            if _overrides(args):
                raise PipelineError(
                    "runtime configuration options cannot be used with config init; "
                    "create the file first, then edit it or use the options at runtime"
                )
            LOGGER.info("Initializing user configuration")
            report = initialize_user_config(args.config, force=args.force)
            if report["status"] == "unchanged":
                LOGGER.info("Configuration unchanged: %s", report["configPath"])
            else:
                log_success(
                    LOGGER,
                    "Configuration %s: %s",
                    report["status"],
                    report["configPath"],
                )
            if report["backupPath"]:
                LOGGER.info("Configuration backup: %s", report["backupPath"])
            save({"command": "config init", **report})
            return 0
        LOGGER.info("Showing resolved configuration")
        resolution = resolve_app_config(
            explicit_path=args.config,
            overrides=_overrides(args),
        )
        resolved = resolution.config
        report = {
            "command": "config list",
            "config": config_document(resolved),
            "generationResolved": {
                "profile": resolved.generation.genai_profile,
                "provider": resolved.provider,
                "model": resolved.model,
                "reasoningEffort": resolved.reasoning_effort,
                "inferenceOptions": resolved.active_provider_config.inference_options(
                    resolved.generation.genai_profile),
            },
            "storage": {
                "layoutVersion": 5,
                "sourceRoots": {
                    source_id: {
                        **{
                            kind: str(path)
                            for kind, path in resolved.source_storage_paths(source_id).items()
                        },
                        "catalog": str(resolved.source_storage_paths(source_id)["data"] / "catalog"),
                        "provenance": str(resolved.source_storage_paths(source_id)["data"] / "provenance"),
                    }
                    for source_id in resolved.sources
                },
            },
            "configSchema": {
                "effectiveVersion": resolution.effective_schema_version,
            },
            "sources": resolution.sources,
            "layers": list(resolution.layers),
        }
        save(report)
        return 0

    from .pipeline import pipeline_status, run_pipeline, run_scheduled

    config = load_app_config(explicit_path=args.config, overrides=_overrides(args))
    targets["sources"] = {
        name: {"sourceRoot": str(config.for_source(name).codex_home),
               **{kind + "Root": str(path) for kind, path in config.source_storage_paths(name).items()}}
        for name in config.sources if args.source is None or name == args.source
    }
    if args.source is not None:
        selected = config.enabled_source_configs(args.source)
        config = selected[0]
    if args.command == "review-notes":
        from .reviews import run_reviews, validate_review

        if args.review_command == "validate":
            save(validate_review(args.artifact.expanduser().absolute()))
            return 0
        result = run_reviews(config, kind=getattr(args, "period", None), day=getattr(args, "date", None),
                             as_of=getattr(args, "as_of", None), dry_run=args.dry_run,
                             force=args.force, allow_edited=args.allow_edited)
        save(result)
        review_result = result
        LOGGER.info("Review notes: generated %s; rendered %s; planned %s; skipped empty %s; "
                    "model calls %s; cache hits %s",
                    review_result.get("generatedCount", 0), review_result.get("renderedCount", 0),
                    review_result.get("plannedCount", 0), review_result.get("skippedEmptyCount", 0),
                    review_result.get("modelCalls", 0), review_result.get("cacheHits", 0))
        if review_result.get("reportPath"):
            LOGGER.info("Run report: %s", review_result["reportPath"])
        for path in review_result.get("reportPaths", []):
            LOGGER.info("Run report: %s", path)
        if not result["complete"] and review_result.get("pendingPeriods", 0):
            LOGGER.warning("Review generation incomplete; %s pending periods. "
                           "Rerun the same command to resume. Exit code: %s",
                           review_result.get("pendingPeriods", "unknown"), 1 if not result["ok"] else 2)
        elif not result["complete"] and "pendingPeriods" not in review_result:
            LOGGER.warning("Review generation incomplete; input processing failed before review planning; "
                           "inspect the run report. Exit code: %s", 1 if not result["ok"] else 2)
        elif not result["complete"]:
            LOGGER.warning("No dated review periods are pending, but unresolved input problems prevent "
                           "completion; inspect the run report. Exit code: %s", 1 if not result["ok"] else 2)
        elif result["ok"] and not args.dry_run:
            LOGGER.info("Review generation complete; no pending periods")
        return 1 if not result["ok"] else 0 if result["complete"] else 2
    if args.command == "actions":
        from .action_links import link_actions

        if args.days is not None and args.days < 1:
            raise PipelineError("--days must be 1 or more")
        LOGGER.info("Matching conversations with Action notes (no Vault writes, no generative AI)")
        result = link_actions(config, dry_run=args.dry_run, as_of=args.as_of, days=args.days)
        save(result)
        decisions = result["decisions"]
        log_success(LOGGER, "actions link: %s conversations; linked %s, ambiguous %s, follow-up %s, unlinked %s",
                    result["conversationCount"], decisions["linked"], decisions["ambiguous"], decisions["followup"],
                    decisions["unlinked"])
        LOGGER.info("Report: %s", result["reportPath"])
        return 0
    if args.command == "ai-logs":
        from .ai_logs import build_ai_logs

        LOGGER.info("Writing the AI's diary%s (no generative AI)", " preview" if args.dry_run else "")
        result = build_ai_logs(config, dry_run=args.dry_run, end=args.date, days=args.days)
        save(result)
        counts = ", ".join(f"{name} {count}" for name, count in sorted(result["notes"].items()))
        for day in result["days"]:
            LOGGER.info("%s: %s (%s conversations)%s", day["date"], day["note"], day["conversations"],
                        f"; {day['reason']}" if day.get("reason") else "")
        if not result["complete"]:
            LOGGER.warning("ai-logs build: %s; deferred notes are written by the next run. Exit code: 2", counts)
            return 2
        log_success(LOGGER, "ai-logs build: %s", counts)
        LOGGER.info("Report: %s", result["reportPath"])
        return 0
    if args.command == "build-report":
        from .usage_report import build_usage_report

        result = build_usage_report(config, dry_run=args.dry_run, no_open=args.no_open)
        save(result)
        log_success(LOGGER, "Usage report: %s", result["htmlPath"])
        return 0
    if args.command == "provenance":
        from .provenance import validate_provenance

        results = [
            {
                **validate_provenance(source.data_root),
                "sourceProvider": source.source_provider,
                "sourceId": source.source_id,
            }
            for source in config.enabled_source_configs()
        ]
        save(
            results[0]
            if len(results) == 1
            else {"ok": all(result["ok"] for result in results), "sourceResults": results}
        )
        return 0
    if args.command == "status":
        save(pipeline_status(config))
        return 0
    LOGGER.info("%s %s", "Planning" if args.dry_run else "Starting", args.command)
    if args.command == "scheduled-run":
        report = run_scheduled(config, dry_run=args.dry_run, config_path=args.config, progress=_progress)
    else:
        report = run_pipeline(config, mode=args.command, dry_run=args.dry_run,
                              config_path=args.config, progress=_progress)
    for report_path in report.get("reportPaths", []):
        LOGGER.info("Run report: %s", report_path)
    if report.get("reportPath"):
        LOGGER.info("Run report: %s", report["reportPath"])
    for warning in report.get("warnings", []):
        LOGGER.warning("%s", warning)
    save(report)
    counts = report["threadCounts"]
    summary = (
        f"{args.command}: normalized {report.get('normalizedThreadCount', 0)} conversations; "
        f"current {counts.get('current', 0)}, deferred {counts.get('deferred', 0)}, "
        f"excluded {counts.get('excluded', 0)}, failed {len(report['failed']) or counts.get('failed', 0)}"
    )
    if report.get("usageTotals", {}).get("requestCount"):
        summary += "; " + _usage_summary(report["usageTotals"])
    failed = bool(report["failed"] or report["threadCounts"].get("failed"))
    if failed:
        LOGGER.error("%s; inspect the saved report for failures", summary)
        return 1
    if (report.get("generationStop") or not report["ok"]
        or (not args.dry_run and args.command in {"clone", "pull", "scheduled-run"} and not report["complete"])):
        LOGGER.warning("%s; incomplete, next pull resumes pending work", summary)
        return 2
    log_success(LOGGER, "%s; %s", summary, "plan validated (no report saved)" if args.dry_run else "completed")
    return 0


def _command_name(args: argparse.Namespace) -> str:
    keys = ("command", "config_command", "actions_command", "ai_logs_command", "review_command",
            "provenance_command", "raw_command")
    return " ".join(str(getattr(args, key)) for key in keys if getattr(args, key, None))


def main(argv: Sequence[str] | None = None) -> int:
    _utf8_console()
    argv = list(sys.argv[1:] if argv is None else argv)
    tokens = argv[:argv.index("--")] if "--" in argv else argv
    json_output = "--json" in tokens
    _configure_logging(argparse.Namespace(quiet="--quiet" in tokens or "-q" in tokens,
                                         verbose="--verbose" in tokens or "-v" in tokens))
    args: argparse.Namespace | None = None
    targets: dict[str, Any] = {}
    pending: list[dict[str, Any]] = []
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        json_output = bool(args.json)
        _configure_logging(args)
        targets["configPaths"] = [str(global_config_path()),
                                  str(Path.cwd() / ".tkn/config.yaml")]
        for key in ("config", "artifact"):
            path = getattr(args, key, None)
            if path is not None:
                targets[key + "Path"] = str(path.expanduser().absolute())
        code = _run(args, pending, targets)
        result = with_target_paths(pending[0], targets.get("sources", {}))
    except SystemExit as exc:
        if isinstance(exc, _ArgumentFailure):
            code, message = 2, exc.message
            parser.print_usage(sys.stderr)
        elif args is None and exc.code == 0:
            raise  # argparse's help/version remain ordinary successful guidance.
        else:
            code, message = 1, str(exc)
        LOGGER.error("%s", message)
        result = {"ok": False, "status": "failed", "error": message,
                  "errorType": "argument" if code == 2 else "execution",
                  "targets": targets}
    except KeyboardInterrupt:
        code = 130
        LOGGER.error("Interrupted; already completed writes are retained; rerun to resume")
        result = {"ok": False, "status": "interrupted", "error": "interrupted", "targets": targets}
    except Exception as exc:
        code = 1
        LOGGER.error("%s", exc)
        for attribute in ("filename", "filename2"):
            path = getattr(exc, attribute, None)
            if path is not None:
                targets[attribute] = str(path)
        result = {"ok": False, "error": str(exc), "errorType": type(exc).__name__, "targets": targets}
    result = {**result, "command": _command_name(args) if args is not None else None, "exitCode": code}
    result.setdefault("status", "success" if code == 0 else "incomplete" if code == 2 else "failed")
    emit(public_result(result, full_output=bool(getattr(args, "full_output", False))), json_output=json_output)
    return code
