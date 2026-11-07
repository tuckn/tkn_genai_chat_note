"""Counts for the AI's diary that need no generative AI (design 5.3).

Day counts use the tool executions and requests of one local date, subagents included. Start
counts (the first files opened, the steps before arriving, the Action rules) describe the whole
conversation and belong to the date it started, so a conversation that runs over several days
is not counted twice.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from .action_links import Conversation
from .digests import PASTED_BLOCK, DigestContext

LOOKUP = frozenset({"read", "search", "list"})
REREAD_TIMES = 3
ENTRANCE_COUNT = 5
BROAD_OUTPUT_CHARS = 30_000
TARGET_CHARS = 200
# Words that often start a correction of the previous answer; candidates only (stage 2b checks them).
CORRECTION = re.compile(
    r"違う|違います|ちがう|そうではなく|そうじゃなく|じゃなくて|ではなくて|間違|まちが|やめて|戻して|元に戻|"
    r"勝手に|頼んでいない|頼んでない|言ったのに|指示と違|\bwrong\b|\bnot what i\b|\bi said\b|\bthat's not\b|"
    r"\brevert\b|\bundo\b",
    re.IGNORECASE,
)
# Listing or searching without a depth limit: (command, the option that limits it or None).
UNBOUNDED = (
    (re.compile(r"-recurse\b", re.IGNORECASE), re.compile(r"-depth\b", re.IGNORECASE)),
    (re.compile(r"(?:^|[\s;|&(])find\s", re.IGNORECASE), re.compile(r"-maxdepth\b", re.IGNORECASE)),
    (re.compile(r"(?:^|[\s;|&(])ls\s+-[A-Za-z]*R"), None),
    (re.compile(r"(?:^|[\s;|&(])dir\s[^|;&]*/s\b", re.IGNORECASE), None),
    (re.compile(r"(?:^|[\s;|&(])tree\b", re.IGNORECASE), re.compile(r"\s-L\s*\d")),
    (re.compile(r"\brg\s[^|;&]*--files\b", re.IGNORECASE), re.compile(r"--max-depth\b", re.IGNORECASE)),
)
ENTRANCE_KINDS = (
    ("instructions", re.compile(r"(?:^|[/\s\"'])(?:agents|claude|gemini)\.md\b")),
    ("skill", re.compile(r"skill\.md\b")),
    ("readme", re.compile(r"(?:^|[/\s\"'])readme\b")),
    ("spec", re.compile(r"[^/\s\"']*spec[^/\s\"']*\.md\b")),
)
# Files written there are the application's own records or scratch work, not the task's work.
NOT_WORK = ("/appdata/local/temp/", "/tmp/", "/.claude/", "/.codex/", "/scratchpad/")


def _all_events(conversation: Conversation) -> list[dict[str, Any]]:
    events = list(conversation.session["toolEvents"])
    for subagent in conversation.subagents:
        events.extend(subagent["session"]["toolEvents"])
    return events


def _day(context: DigestContext, value: str | None) -> str | None:
    local = context.local(value) if value else None
    return local.date().isoformat() if local else None


def _first_target(event: dict[str, Any]) -> str:
    targets = event.get("targets") or []
    return str(targets[0]) if targets else ""


def _key(text: str) -> str:
    return " ".join(text.replace('"', " ").replace("'", " ").split()).casefold()


def _broad(event: dict[str, Any]) -> str | None:
    """A listing or search that returned a very large result or has no depth limit (reading a large
    file is not a scan)."""
    if event["category"] == "read":
        return None
    if (event.get("outputChars") or 0) >= BROAD_OUTPUT_CHARS:
        return "large-output"
    text = " ".join(event.get("targets") or [])
    for command, limit in UNBOUNDED:
        if command.search(text) and not (limit and limit.search(text)):
            return "no-depth-limit"
    return None


def activity_dates(conversation: Conversation, context: DigestContext) -> list[str]:
    """Local dates with a message or a tool execution, subagents included (a night of tool runs counts)."""
    dates = set(conversation.session["localDates"])
    for subagent in conversation.subagents:
        dates.update(subagent["session"]["localDates"])
    for event in _all_events(conversation):
        when = _day(context, event.get("timestamp"))
        if when:
            dates.add(when)
    return sorted(dates)


def _digest_work_dates(session: dict[str, Any], context: DigestContext) -> set[str] | None:
    """One transcript's work dates, or None when some work has no time (then nothing is left out)."""
    if "assistantDates" not in session or session.get("undatedAssistantMessages") or any(
            "slashCommand" not in request for request in session["userRequests"]):
        return None  # A digest made before 0.51.1, or work without a time.
    dates: set[str] = set(session["assistantDates"])
    for request in session["userRequests"]:
        if request.get("slashCommand"):
            continue  # A command such as `/model opus`; an AI answer would add the date itself.
        if not request.get("localDate"):
            return None
        dates.add(request["localDate"])
    for event in session["toolEvents"]:
        when = _day(context, event.get("timestamp"))
        if not when:
            return None
        dates.add(when)
    return dates


def work_dates(conversation: Conversation, context: DigestContext) -> list[str]:
    """Local dates on which the person asked for something or the AI wrote or ran something,
    subagents included. A date with only slash commands or lines the application wrote is left
    out. When some work has no time, or a digest predates 0.51.1, every activity date is kept."""
    dates: set[str] = set()
    for digest in (conversation.digest, *conversation.subagents):
        found = _digest_work_dates(digest["session"], context)
        if found is None:
            return activity_dates(conversation, context)
        dates.update(found)
    return sorted(dates)


def start_date(conversation: Conversation, context: DigestContext) -> str | None:
    """The date the conversation started working, from its own transcript: subagents are started by
    its tool calls, and which subagent files a run reads depends on the window."""
    own = _digest_work_dates(conversation.session, context)
    if own:
        return min(own)
    dates = work_dates(conversation, context)
    return dates[0] if dates else None


def day_metrics(conversation: Conversation, day: str, context: DigestContext) -> dict[str, Any]:
    """What one conversation did on one local date, from its tool executions and requests of that date."""
    session = conversation.session
    single_day = activity_dates(conversation, context) == [day]
    events: list[dict[str, Any]] = []
    undated = 0
    for event in _all_events(conversation):
        when = _day(context, event.get("timestamp"))
        if when == day or (when is None and single_day):
            events.append(event)
        elif when is None:
            undated += 1
    requests = [r for r in session["userRequests"] if r.get("localDate") == day]
    first_request = session["userRequests"][0]["line"] if session["userRequests"] else None
    lookups = [e for e in events if e["category"] in LOOKUP]
    failures = Counter(str(e["failureKind"]) for e in lookups
                       if e["succeeded"] is False and e["failureKind"] != "rejected")
    searches = Counter(_key(f"{e['tool']} {' '.join(e.get('targets') or [])}")
                       for e in lookups if e["category"] == "search")
    reads = Counter(context.paths.text(_first_target(e)) for e in lookups
                    if e["category"] == "read" and e["succeeded"] is not False and _first_target(e))
    broad = [{"evidenceId": e["evidenceId"], "reason": reason, "target": _first_target(e)[:TARGET_CHARS]}
             for e in lookups if (reason := _broad(e))]
    # The messages of that date, and tool executions that ran before or after them.
    values = [*session.get("dayRanges", {}).get(day, []), *(e["timestamp"] for e in events)]
    times = sorted(local for local in (context.local(value) for value in values if value)
                   if local is not None and local.date().isoformat() == day)
    return {
        "firstLocalTime": times[0].isoformat() if times else None,
        "lastLocalTime": times[-1].isoformat() if times else None,
        "humanRequests": len(requests),
        "toolEvents": len(events),
        "undatedToolEvents": undated,
        "lookups": len(lookups),
        "readFailures": dict(sorted(failures.items())),
        "rejections": sum(1 for e in events if e["failureKind"] == "rejected"),
        "correctionCandidates": [r["evidenceId"] for r in requests if r["line"] != first_request
                                 and CORRECTION.search(PASTED_BLOCK.sub("", r["text"]))],
        "repeatedSearches": sum(count - 1 for count in searches.values() if count > 1),
        "rereadFiles": [{"target": target[:TARGET_CHARS], "reads": count}
                        for target, count in sorted(reads.items()) if count >= REREAD_TIMES],
        "broadScans": broad,
    }


def entrance_kind(target: str, context: DigestContext) -> str:
    text = context.paths.text(target)
    marker = context.actions_dir + "/"
    if context.actions_dir and (marker in text) and ".md" in text:
        return "action"
    return next((kind for kind, pattern in ENTRANCE_KINDS if pattern.search(text)), "other")


def _is_action_path(path: str, context: DigestContext) -> bool:
    if not context.actions_dir:
        return False
    if path.startswith(context.actions_dir + "/"):
        return True  # A Vault-relative path from a Vault touch.
    return bool(context.vault_root) and path.startswith(f"{context.vault_root}/{context.actions_dir}/")


def start_metrics(conversation: Conversation, row: dict[str, Any] | None, context: DigestContext) -> dict[str, Any]:
    """How the conversation began and whether it kept the Action rules (design 5.3, last row)."""
    session = conversation.session
    main = sorted(session["toolEvents"], key=lambda e: e["line"])
    lookups = [e for e in main if e["category"] in LOOKUP]
    entrances = [{"evidenceId": e["evidenceId"], "kind": entrance_kind(_first_target(e), context),
                  "target": _first_target(e)[:TARGET_CHARS]} for e in lookups[:ENTRANCE_COUNT]]
    links = row["links"] if row else []
    linked_evidence = {detail["evidenceId"] for link in links for detail in link["details"]
                       if detail.get("clue") == "A" and detail.get("evidenceId")}
    steps: int | None = None
    count = 0
    for event in main:
        if (event["category"] == "edit" and event["succeeded"] is not False) or event["evidenceId"] in linked_evidence:
            steps = count
            break
        if event["category"] in LOOKUP:
            count += 1
    return {"entrances": entrances, "stepsToArrive": steps, "actionRules": action_rules(conversation, row, context)}


def action_rules(conversation: Conversation, row: dict[str, Any] | None, context: DigestContext) -> dict[str, Any]:
    """For a conversation that did work: did it read an Action first, and do the Action's Notes cite it?

    Work is a commit, a registered scheduled task or a written file other than an Action note and
    the application's own or scratch files. A Notes line counts only when it matches the configured
    record-line format and cites this session (clue B).
    """
    events = _all_events(conversation)
    times = {e["evidenceId"]: context.local(e["timestamp"]) for e in events if e.get("timestamp")}
    facts = list(conversation.session["facts"])
    for subagent in conversation.subagents:
        facts.extend(subagent["session"]["facts"])
    work = [fact for fact in facts if fact["kind"] in {"git-commit", "scheduled-task-registered"}
            or (fact["kind"] == "file-written" and not _is_action_path(fact["path"], context)
                and not any(marker in fact["path"] for marker in NOT_WORK))]
    if not work:
        return {"work": False}
    work_times = [time for fact in work if (time := times.get(fact["evidenceId"])) is not None]
    first_work = min(work_times) if work_times else None
    touches = conversation.touches()
    read_times = [times.get(t["evidenceId"]) for t in touches
                  if t["access"] == "read" and _is_action_path(t["path"], context)]
    links = row["links"] if row else []
    return {
        "work": True,
        "workKinds": dict(sorted(Counter(fact["kind"] for fact in work).items())),
        "readActionBeforeWork": any(time is not None and (first_work is None or time <= first_work)
                                    for time in read_times),
        "editedAction": any(t["access"] == "edit" and _is_action_path(t["path"], context) for t in touches),
        "recordedInNotes": any("B" in link["clues"] for link in links),
    }


def score(metrics: dict[str, Any]) -> int:
    """How much went wrong in a conversation's day; orders the conversations named in the diary."""
    return (sum(int(count) for count in metrics["readFailures"].values()) + int(metrics["repeatedSearches"])
            + len(metrics["rereadFiles"]) + len(metrics["broadScans"]) + int(metrics["rejections"]))


def totals(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Sums of the day counts, and of the start counts of the conversations that started that day."""
    failures: Counter[str] = Counter()
    for item in items:
        failures.update(item["day"]["readFailures"])
    started = [item["start"] for item in items if item.get("start") is not None]
    rules = [start["actionRules"] for start in started if start["actionRules"]["work"]]
    entrances: Counter[str] = Counter(entrance["kind"] for start in started for entrance in start["entrances"])
    return {
        "conversations": len(items),
        "startedConversations": len(started),
        "humanRequests": sum(item["day"]["humanRequests"] for item in items),
        "toolEvents": sum(item["day"]["toolEvents"] for item in items),
        "lookups": sum(item["day"]["lookups"] for item in items),
        "readFailures": dict(sorted(failures.items())),
        "rejections": sum(item["day"]["rejections"] for item in items),
        "correctionCandidates": sum(len(item["day"]["correctionCandidates"]) for item in items),
        "repeatedSearches": sum(item["day"]["repeatedSearches"] for item in items),
        "rereadFiles": sum(len(item["day"]["rereadFiles"]) for item in items),
        "broadScans": sum(len(item["day"]["broadScans"]) for item in items),
        "entranceKinds": dict(sorted(entrances.items())),
        "actionRules": {
            "startedWithWork": len(rules),
            "readActionBeforeWork": sum(1 for rule in rules if rule["readActionBeforeWork"]),
            "editedAction": sum(1 for rule in rules if rule["editedAction"]),
            "recordedInNotes": sum(1 for rule in rules if rule["recordedInNotes"]),
        },
    }
