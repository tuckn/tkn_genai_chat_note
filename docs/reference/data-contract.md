# Data contract: chat evidence and calendar reviews

CLI 0.51.0 · config 11.1.0 · storage 5 · catalog/provenance 1.0.0 · Review Note 1.0.0

Terminal results follow the [CLI output contract](cli-output.md): copyable
`key=value` by default, explicit `--json`, independent `--full-output` detail.
This display-only boundary does not change any file or Python API contract below.

This is the file interface for consumers of captured Codex conversations and calendar
reviews. For operation, see the [README](../../README.md); for review content, see the
[Review Note contract](review-note-format.md).

## Ownership and processing

The application captures local chat files, exports deterministic Canonical Events,
records observed Project membership and generates weekly, monthly and yearly reviews.
Weekly reviews consume Canonical Events; monthly reviews consume weekly JSON; yearly
reviews consume monthly JSON. Missing lower-period evidence falls back to Canonical
Events and bounded, hash-verified Raw excerpts. Session Note generation is removed.
Existing Session Notes are neither read as review inputs nor deleted.

Intelligence candidates and AI behavior improvements remain proposals. Adopted knowledge,
semantics, rules and Working Context belong to downstream processes. This CLI never
rewrites a Vault, Skill, memory or AGENTS.md automatically.

`raw ingest` only captures source files. `clone` initializes storage, captures and
normalizes; `pull` refreshes an initialized store. These commands do not call a model.
`scheduled-run` captures and normalizes without inference; since 0.48.0 it no longer generates
reviews. `review-notes due` builds closed periods when run explicitly. A successful normalization
does not imply that reviews have been generated. When
tkn-genai-chat-archiver manages raw_root, these commands read its archive instead of
capturing (see [Archive-managed Raw](#archive-managed-raw)) and `raw ingest` is refused.

## Note processes (stages 0 and 2a): reading locations, digests, the link report and the AI's diary

`actions link --dry-run` reads conversations where `chat_sources` says and the Vault's Action notes
(`vault`, read only). It writes only below `processes.state_root` and the digest cache
`<cache_root>/digests/`; both, and `processes.data_root`, must not overlap the Vault or any folder
named under `chat_sources` (an application's folder or an archive, whatever the entry's mode and even
when it is disabled). It calls no generative AI.

- Reading locations: each `chat_sources.<name>` entry has `app` (`codex` or `claude-code`),
  `source_id`, `mode`, `root` (the application's folder; default `~/.codex` or `~/.claude`),
  `archive` (a tkn-genai-chat-archiver archive root) and `enabled`. `mode: app` (default) reads the
  application's folder; `archive` reads only the archive; `auto` reads the application's folder and
  takes from the archive only the conversations the folder no longer has (Codex rollouts are matched
  by file name without `.zst`, so compressed or moved rollouts count as present; Claude Code
  transcripts by the path below the project folder, that is the session and agent, because the
  project folder changes with the working folder). Neither location is written. An archive is read only through the
  archiver's `ArchiveView`, only when its marker names the same `app` (profile) and `source_id`:
  finished manifests only, the latest version of each relative path, stored and decoded SHA-256
  checked. One `app` and `source_id` pair may appear in one entry only.
- Session digests: a cache, not data to keep. `<cache_root>/digests/<app>/<source_id>/<key>.json`,
  one per conversation file (`key` derives from the relative path), schemaVersion 2.1.0. `file` holds
  app, sourceId, relativePath, the decoded content's SHA-256 and the line count, and depends only on
  the content; `location` names the chat source and whether this copy was read from the application's
  folder (`app`) or the archive (`archive`, with the stored SHA-256, capture time and previous
  relation). `session` holds sessionRef, kind, parentSessionId, `copiedRecords`, `originator`
  (Codex: who started the thread, for example `codex_exec`), normalized cwd and repo, title, local
  dates, `dayRanges` (local date to the first and last message time of that date), `userRequests`,
  `excludedUserMessages`, `assistantFinals`, `toolEvents`, `vaultTouches` and `facts`; `coverage`
  counts parser warnings and ignored record types. A Claude Code main transcript leaves out the
  records that carry another session's `sessionId` (copied there when a conversation is forked or
  resumed; that session's own digest holds them) and counts them in `copiedRecords` (`records` and
  `from`, the other session IDs). A scheduled task counts as registered for
  `Register-ScheduledTask` or `schtasks /create`, not for `Unregister-ScheduledTask`. A digest is reused only for the
  same content SHA-256, settings and application version (and, for Codex, the same thread names from
  `session_index.jsonl`, recorded as `titlesSha256`), and may be deleted at any time.
  `digests.cache: false` reads and writes no cache. The same content in two files of one run is
  digested once.
- Evidence IDs: `<app>/<source_id>/<session_id>@<first 12 hex of the decoded content's SHA-256>#L<line>`.
  The same content gives the same ID whether it was read as `.jsonl` or `.jsonl.zst`, from the
  application's folder or from the archive. Archived versions never change; a conversation read only
  from the application's folder can later be deleted or extended there, after which an older ID can
  no longer be checked (`auto` keeps it checkable through the archive).
- Report: `<state_root>/actions-link/<runId>/report.md` (for people), `report.json` (summary counts,
  per-conversation rows and per-Action links; schemaVersion 2.0.0: `sources`, `skippedSources`,
  `digestCache` and `readFrom` replace `archives` and `skippedArchives`, and each row's `digest`
  names `source`, `location`, `relativePath` and `contentSha256`) and `links.jsonl` (one row per
  conversation with its clues, evidence IDs and decision `linked`, `ambiguous`, `followup` or
  `unlinked`). An A clue that only names the file in a command's arguments is listed under
  `weakLinks` and links only together with another clue for the same Action. `followup` rows list
  the finished Actions whose repository matched under `finishedActions`.
  `<state_root>/actions-link/last-run.json` names the latest report.
- The AI's diary (`ai-logs build`, stage 2a, no generative AI): one Markdown note per local date at
  `<vault.root>/<vault.ai_logs.dir>/<vault.ai_logs.filename>` (both take `{date:<strftime>}` fields
  only, stay below `vault.root` and must name a different note for every date), only for a finished
  day with at least one main conversation that has a message or a tool execution (subagents
  included) on that date; the parent transcript of such a subagent is read whatever its modification
  time. Neither `processes.state_root` nor the digest cache may overlap `processes.data_root`.
  Frontmatter: `type` (`vault.ai_logs.type`, default `aiLog`), `schemaVersion`
  (`vault.ai_logs.schema_version`, default 1.0.0), `title` (the file name without `.md`),
  `description`, `actions` (WikiLinks of the linked Actions), `generator: tkn-genai-chat-note`,
  `reviewStatus: unreviewed`, `created`, `updated`, `noteId`. Body: a link to the person's Log
  (`vault.logs`) when that note exists, then the fixed sections 今日やったこと, うまくいったこと,
  うまくいかなかったこと, 本人から学んだこと and 持ち越し; stage 2a fills the first and the third.
  A note still exactly as this command last wrote it is rebuilt with the same `noteId` and
  `created`. Any other existing note is not rebuilt: the day's conversations whose session ID it does
  not contain are appended under `## 追加（<YYYY-MM-DD HH:MM>）`, changing only the Frontmatter's
  `updated` line and keeping the BOM and line breaks; such a note changed in the last 30 minutes is
  left for the next run (exit code 2). The note is compared right before one replacement (or a
  create that never replaces) and planned once more when it changed; a note another program holds
  open is left for the next run instead of being retried. A note this command wrote that is now
  missing is not written again. While a chat source, one of its locations, one of its folders
  (`folder-unlisted` in the source's `notes`) or a conversation file cannot be read
  (`incompleteInputs`), notes are not rebuilt (left for the next run) and existing
  counts are not replaced; new notes and additions are still written. Runs hold
  `<state_root>/ai-logs/.lock`.
- Diary state and counts: `<state_root>/ai-logs/notes.json` records, per Vault-relative note path,
  the date, the mode (`built` or `appended`), SHA-256, `noteId`, `created`, `updated` and session IDs
  last written; a note recorded for another date is never replaced. `<data_root>/ai-logs/metrics/<YYYY>/<YYYY-MM-DD>.json` (schemaVersion 1.0.0) holds the
  day's `totals` and, per conversation, `sessionRef`, title, cwd, repo, linked `actions`, `day` counts
  (local first and last time, human requests, tool events, lookups, `readFailures` by kind,
  rejections, `correctionCandidates` evidence IDs, repeated searches, files read three times or more,
  broad scans) and `start` counts on the date the conversation started (the first five lookups and
  their kind, lookups before the first edit or linked Action, and `actionRules`: whether a conversation
  that did work read an Action first, edited one, and is cited by a linked Action's Notes). A run
  report goes to `<state_root>/ai-logs/<runId>/report.json`; `--dry-run` writes the planned notes
  (below `notes/` at their Vault-relative paths) and counts there only.

## Source identity and configuration

`sources` is an ID-keyed map. Each source has source_root, optional raw_root/data_root/
state_root, enabled/include_archived, and reviews settings. Source IDs identify acquisition
environments, not inference providers. Preserve their spelling across configuration updates.
The source provider is `codex`. Generation profiles are configured separately. `sources` serves
the capture, normalization and review commands, which stay until stage 4; the note processes read
conversations through `chat_sources` instead.

Source IDs allow ASCII letters/digits/._-, starting with a letter or digit. Windows device
names, trailing dots, duplicate YAML keys and case-only duplicate IDs are rejected.
Map layers merge by ID; an explicit map suppresses implicit source defaults. Disabled
sources are not scanned. `--source <id>` selects one enabled source.

Each source owns its catalog, evidence, reviews and checkpoints. Reviews are generated
independently; sources are not merged into one calendar note. Review settings belong to
`sources.<id>.reviews`. Review outputs are `<data_root>/reviews`; review state is
`<state_root>/reviews`. Review-specific output roots are not configurable.

Every configuration file states `schema_version: "11.1.0"` (an older minor of schema 11 and a newer
patch are accepted). Another major or a newer minor stops with an error; older files are not
converted. Schema 11 replaces `archives` with `chat_sources` and adds `digests`; 11.1 adds
`vault.logs` and `vault.ai_logs`. Since schema 10, every GenAI connection is
defined in this file under `genai_profiles` (tkn_genai_bridge profiles plus this application's
`limits` and Ollama `model_digest`) and `generation.genai_profile` selects the review connection;
no shared Bridge configuration is read. Only `~/.tkn/genai_chat_note`
is the user directory; earlier application directories are not read.

## Stable identities and references

- threadId is the original conversation ID. threadKey is UUIDv5 in the URL namespace
  for `codex-thread:<threadId>`, independent of Project membership and location.
- Catalog identity is `(sourceProvider, sourceId, threadKey)`. Identical threadKeys in
  different environments are allowed.
- `data:/` resolves under this source's data_root. `raw:/<provider>/<source_id>/` is
  a virtual prefix: remove it and resolve the remaining path under this source's raw_root.
- Canonical event IDs include threadKey, capture SHA-256 and local event/line identity.
  They identify a particular version, not a stable event across arbitrary rewrites.
- Entity versions are `sha256:<hex>` of exact bytes. Join evidence by `(id, version)`.
  Filenames and locators are not identities.
- Review IDs bind output location and period. Candidate IDs bind their review and content.
  Retain original IDs and metadata when relocating or adopting an artifact.

Raw schema-3 paths are latest source copies and may change. Historical citations use
retained provenance snapshotRef and hash, not a mutable latest Raw path. No cloud-only
history is fetched, and no complete-history claim is made.

## Archive-managed Raw

When raw_root contains `.tkn-genai-chat-archive.json`, tkn-genai-chat-archiver owns it and
this pipeline only reads it. No configuration change selects this mode. The marker must have
schemaVersion 1.x, applicationId `tkn-genai-chat-archiver`, rootKind `chatLogArchive`,
profile `codex` and this sourceId; otherwise processing stops before any write.

- Only confirmed `manifests/*.jsonl` (last line is a `run` record) are read. `.partial` and
  unconfirmed manifests are ignored and reported. An invalid record in a confirmed manifest,
  a record of another source or an unsafe path stops processing.
- Chat inputs are `sessions/` paths and, with include_archived, `archived_sessions/` paths
  ending in `.jsonl`, `.jsonl.zst` or `.jsonl.gz`. Each path uses its latest version ordered
  by capturedAt, then runId.
- When the latest versions of `x.jsonl` and `x.jsonl.zst` share content.sha256, they form one
  input whose reference is the earliest captured of those versions. Codex compression thus
  keeps existing capture refs and Canonical Events. Different contents stay separate histories.
- Stored size/SHA-256 and decoded size/SHA-256 are verified before use. A version that fails
  falls back to another version with the same content; otherwise it is reported as failed.
- captureRef is `raw:/codex/<source_id>/<storedPath>`. Adopted files keep `sessions/...` and
  `archived_sessions/...`; later versions are under `snapshots/<runId>/...`. Capture SHA-256,
  byte counts, provenance snapshots and `#L<line>` locators describe decoded content.
- Compressed versions are decoded into `<cache_root>/codex/<source_id>/archive-content/`,
  reused after hash checks and pruned when no input uses them. Dry runs decode into a
  temporary folder that is removed when the run ends.
- `.codex-global-state.json` and `session_index.jsonl` come from their latest archived
  versions. Titles fall back to source_root when the archive has no readable index.
- Nothing is written below raw_root: no copy, manifest.jsonl, metadata/, ownership marker or
  lock. Confirmed manifests and their versions never change, so reads need no lock.
  `raw ingest` refuses archive-managed roots.
- The capture report has mode `archive-read`, zero capture counts, per-input aliasRefs and an
  `archive` object with manifest counts, ignored manifests, alias and pruned counts, and the
  latest archive run. A latest run that is not complete or recovered adds a warning.

## Published artifacts

| Artifact | Contract |
| --- | --- |
| Raw manifest | Reads 1/2/3; writes 3, with refs, hashes, byte counts and capture metadata. Archive-managed Raw: reads archive manifests 1.x, writes none |
| store.json | 1.0.0; storageVersion 5, source identity, Raw prefix and reference aliases (empty in new stores) |
| catalog/threads.json | 1.0.0; asOf and threads |
| Canonical export | 1.0.0; parser 2, event locators, source metadata and diagnostics |
| Provenance entities, activities, index | 1.0.0; versioned artifacts and normalization lineage |
| Capture/normalization report | Integer 4, for source-local and multi-source responses |
| Review Markdown and JSON | 1.0.0; periods, claims, evidence, candidates and coverage |

Catalog rows include source identity, threadId/threadKey, capture and canonical refs/hashes,
status/reason, dates and observed membership/history. New rows do not publish Session Note
noteId/noteRef/noteSha256. A current status means normalized evidence is available, not that
an AI summary exists. Failed/excluded rows and diagnostics remain visible. Internal or
approval-only conversations are excluded from review inputs.

Membership preserves explicit, projectless, root-inferred, ambiguous, unmatched or
unavailable observations. Nullable IDs and candidate IDs retain uncertainty. Membership
history dates when the pipeline observed a change, not when the user made it.

Exact duplicate captures and provable byte-prefix versions are coalesced for normalization.
Divergent histories are retained, ordered by history start while preserving source order.
No active branch is inferred and history_base does not discard records. historyBranches,
sourceSetSha256 and per-file sourceDiagnostics describe the retained set. Branch events
have history-qualified localId/branchId and physical Raw line locators. Singular capture
fields identify only the first retained history; use the branch records for full coverage.

## Provenance and reads

Entities contain schemaVersion, id, version, sha256, ref, kind, mediaType, byteCount and
snapshotRef. Exact snapshots live at `data:/provenance/blobs/<hash-prefix>/<hash>`.
Entity filenames hash id + NUL + sha256. A relocated entity can retain its first locator;
use snapshotRef for historical bytes.

Activities record run/subject/stage, timestamps, agent, used/generated entity versions and
derivedFrom edges. The current upstream generation stage is deterministic normalization.
Review JSON carries its own claim-level evidence and transitive citations. A dependency
edge alone does not establish semantic truth.

The provenance index records runId/asOf/pipelineComplete/artifacts/activityRefs. Current
artifacts identify Canonical Events and thread/source identity. Older derived artifacts
in a reused store remain inactive; they are not refreshed or deleted.

Consumers should:

1. Preserve the catalog/index bytes and run metadata used for selection.
2. Verify supported schemas, source identity and hashes of selected canonical/review files.
3. Resolve historical snapshotRef safely and verify hashes and byte counts.
4. Preserve evidence IDs/versions and inferred/proposed status in downstream outputs.
5. Recheck inputs before publishing; retry if concurrent updates changed them.

Files are atomically replaced individually, not as one database transaction. Readers can
observe different generations and must reject inconsistent combinations. `raw ingest`
does not republish the derived index. `provenance validate` checks structure, identities,
snapshot hashes/lengths and current artifacts; it does not prove semantic accuracy.

## Storage

Omitted source roots resolve to `~/.tkn/genai_chat_note/<kind>/codex/<source_id>`. Configured
roots are final paths. Roots may share a parent but must not overlap one another or input
directories. Ownership markers bind source identity; locks protect actual roots. A root is used
only when it is missing, empty, or carries this application's ownership marker with this source's
provider and ID. Stores of other layouts or storage versions are not converted; processing stops
before any write.

- Raw: sessions/, archived_sessions/, manifest.jsonl and metadata/. An archive-managed
  raw_root also has manifests/ and snapshots/, all owned by tkn-genai-chat-archiver.
- Data: source-aligned/, catalog/, provenance/, store.json and reviews/.
- State: pipeline.json, normalization/, reports/, last-run.json and reviews/.
- Review state: index.json, reports/, usage/, transactions/ and backups/.

Historical Session Note files/checkpoints may coexist without being updated. Back up
Raw/data/state together. Review state is needed for restart and edit protection. Cache is
regenerable. Review stage cache lives at cache_root/reviews, keyed by generation signature
and exact prompt. Hash/schema/citation validation precedes reuse.

store.json rawRefAliases/dataRefAliases, when present, translate locators longest-prefix
first, once, rejecting traversal before and after translation. Immutable snapshots retain
bytes. The ownership marker `.tkn-genai-chat-note-root.json` keeps applicationId
`tkn-genai-chat-note-pipeline` deliberately; a package rename does not change ownership of
existing stores or of archives that tkn-genai-chat-archiver adopted.

## Review generation and usage

The [Review Note contract](review-note-format.md) specifies period boundaries, fallback,
bounded Raw verification, categories, candidate projections, publication and protection.
JSON is authoritative for higher-period generation; Markdown is a human rendering.

`generation.genai_profile` selects a root `genai_profiles` entry, which tkn_genai_bridge (a pinned
library dependency) runs directly. Effective generation settings and Bridge version bind cache
identity; profile-name and price-only changes do not. CLI providers reuse their own login; API
providers name the environment variable of a key and never store it. Authentication is lazy
and never runs during dry-run.

Review call/time and API limits apply per source invocation. Cached validated stages do
not consume calls. Budget exhaustion defers unfinished work for a later invocation.
Transport errors are not automatically retried. Missing usage/cost stays unknown, not zero.
Configured reservations are conservative estimates, not invoice guarantees.

Review usage records live under each source's state_root/reviews/usage. build-report reads
these plus historical source journals/reports, deduplicating recorded attempts. It keeps
known subtotals distinct from missing totals, and cached/reasoning tokens as subsets.
Price scenarios are reference estimates, not actual invoices. Cost calculation uses Bridge
pricing; a scenario either states rates inline or references `genai_profile` and `model`. Reports contain index.html, usage.json and usage.csv, without prompt/answer bodies.

Dry-run is read-only: no inference, authentication, network, directories, locks, reports,
cache or state writes. It describes saved evidence and planned work, not a persisted update.
