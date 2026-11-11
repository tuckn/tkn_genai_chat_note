# Changelog

このプロジェクトの主な変更を、新しいバージョンから順に記録します。

既存履歴は Git のコミットと `pyproject.toml` のバージョン変更から整理しました。
日付はバージョン更新コミットの日付であり、配布日を保証するものではありません。
現在のパッケージ名になった 0.15.0 以降を対象とし、それ以前の前身プロジェクトの履歴は Git を参照してください。
Git で独立したバージョン更新を確認できない 0.19.x、0.26.x、0.27.x などは個別に掲載していません。

## Unreleased

- Update tkn-genai-bridge from 0.10.0 (9cf534e) to 27ac8e4 (0.11.0). Claude Code calls no longer add an empty folder under `~/.claude/projects` for every call, and when a forced termination leaves a temporary folder behind, the first generation of the next run removes it. Folders left by the earlier Bridge version are not removed.

## 0.54.0 - 2026-11-10

- `ai-dailies build` takes the Actions as they were on a conversation's local dates for clue D (the working folder's repository) and the follow-up check: an Action created and started after the conversation's last date is left out (the earlier of `created` and `started` counts, for an Action written down after its work began), and an Action closed now whose `completed` date is on or after the conversation's first date counts as in progress. A diary written or rebuilt for a past date no longer links a conversation to an Action created later for the same repository. `actions link` still takes the Actions as they are now.
- Configuration schema 12.1.0 adds `vault.actions.frontmatter.created`, `started` and `completed` (defaults `created`, `started` and `completed`; null when the Vault does not use one). Files stating 12.0.0 are still read.

## 0.53.0 - 2026-11-10

- Rename the AI's diary from aiLog to aiDaily, following the Vault, whose person's Log became Daily (one note per date): the command `ai-logs build` is now `ai-dailies build`, and its state and counts live below `processes.state_root/ai-dailies/` and `processes.data_root/ai-dailies/metrics/`. The diary links the person's Daily as `本人のDaily：[[<note>]]`.
- Configuration schema 12.0.0 renames `vault.logs` to `vault.dailies` (default `dailies/{date:%Y}/{date:%Y%m%d}.md`) and `vault.ai_logs` to `vault.ai_dailies` (default `ai-dailies/{date:%Y}/{date:%Y%m%d} AI.md`, `type: aiDaily`). Files stating schema 11 are refused and not converted; write `schema_version: "12.0.0"` and the new keys. Earlier state below `ai-logs/` is not read; move it to `ai-dailies/` (and its note paths, when the notes moved too) to keep notes this command wrote rebuildable.

## 0.52.0 - 2026-11-09

- The default `vault.actions.record_line` also reads the current form of an Action's record lines, written below a `### YYYY-MM-DD` heading with an upper-case kind and half-width separators: `- DECIDED (user): <conclusion>。根拠: <evidence>` (kinds `DECIDED`, `DONE`, `CHECKED` and the earlier `PROPOSED`; the actor may add `, via <process>`). The earlier form that starts with the date (`- YYYY-MM-DD 決定（本人）：…。根拠：…`) is still read. A line without a date counts only with one of those kinds, so other bullets in Notes are not counted. This keeps clue B of `actions link` and the "cited by its Notes" count of `ai-logs build` working for Actions that use the current form.
- Design 4.3, 4.6 and 4.8 write proposed record lines in the current form and place them below the heading of the evidence date.

## 0.51.1 - 2026-11-07

- `ai-logs build` leaves out a conversation on a date when, on that date, it has only slash commands (such as `/model` or `/model opus`) or lines the application wrote, without a request, an AI message or a tool execution. Such conversations are counted under `withoutAiWork` in the run result and listed in the day's counts. A conversation with a request, an AI message or a tool execution without a time is never left out. A conversation's start counts belong to the first date its own transcript worked, whichever subagent files a run reads. Notes this command wrote and nobody changed are rebuilt on the next run; a note whose date no longer has any conversation is reported as `stale` and left as it is.
- Session digests (schema 2.2.0) add `assistantDates` (the local dates with an AI message) and `undatedAssistantMessages`.

## 0.51.0 - 2026-11-07

Stage 2a of the role redesign: the AI's diary without generative AI (design 5).

- Add `ai-logs build [--date YYYY-MM-DD] [--days N] [--dry-run]`: one diary note per finished local date below `vault.ai_logs` (default `ai-logs/{date:%Y}/{date:%Y-%m-%d} AI.md`, `type: aiLog`). It lists the day's Codex and Claude Code conversations (time, title, working folder, human requests, Actions linked by clues A-D, evidence) and the counts of design 5.3 (lookups, read failures by kind, repeated searches, files read three times or more, broad scans, rejections, correction candidates, and whether conversations that did work read an Action first and are cited by its Notes). The sections for what went well, what was learned from the person and what is carried over stay empty until stage 2b. It links the person's Log (`vault.logs`) when that note exists and never writes it. No generative AI is called.
- Diary notes this command wrote and nobody changed are rebuilt with the same `noteId` and `created`; a changed note is not rebuilt, and conversations it does not name yet are appended under `## 追加（<日時>）` (only the `updated` line of the Frontmatter changes; BOM and line breaks are kept). A changed note modified in the last 30 minutes is left for the next run (exit code 2). Notes are compared right before one replacement without retry (a note open in another program is left for the next run), a note this command wrote and the person deleted or moved is not written again, and runs hold a lock. While a chat source, a location, a folder or a conversation file cannot be read, notes are not rebuilt and existing counts are not replaced. A conversation counts for a date when it has a message or a tool execution (subagents included) on it, and a subagent's parent transcript is read whatever its modification time (this also applies to `actions link`). A folder of an application's folder that cannot be listed is reported in the source's `notes` (`folder-unlisted`). Counts go to `processes.data_root/ai-logs/metrics/`, the last written state to `processes.state_root/ai-logs/notes.json`. `--dry-run` writes previews and counts below `processes.state_root` only; today can be previewed but not written.
- Session digests (schema 2.1.0) leave out the records a Claude Code main transcript copied from another session when it was forked or resumed (they carry that session's `sessionId`) and count them in `copiedRecords`, so the earlier conversation's requests, tool executions and clues are not counted twice. This also changes `actions link`. Digests add `dayRanges` and, for Codex, `originator`.
- `Unregister-ScheduledTask` no longer counts as registering a scheduled task.
- Configuration schema 11.1.0 adds `vault.logs` and `vault.ai_logs` (`dir` and `filename` take `{date:<strftime>}` fields only and must name a different note for every date). Files stating 11.0.0 are still read. `ai-logs build` refuses a `processes.state_root` or digest cache that overlaps `processes.data_root`.
- `actions link` and `ai-logs build` share the reading of conversations and the matching with Actions.

## 0.50.0 - 2026-11-06

Preparation for stage 1 of the role redesign (design 3.1, 3.2 and 3.4).

- Reading locations: `chat_sources` replaces `archives`. Each entry names `app` (`codex` or `claude-code`), `source_id`, `mode`, `root` and `archive`. `mode: app` (default) reads the application's folder (`root`, default `~/.codex` or `~/.claude`) read only; `archive` reads only a tkn-genai-chat-archiver archive (finished manifests, the latest version of each relative path, SHA-256 checked) whose marker names the same app and source; `auto` reads the application's folder first and takes the conversations it no longer has from the archive (a conversation in both is read from the folder; Codex rollouts are matched by file name without `.zst`, Claude Code transcripts by session and agent below the project folder). Neither location is written. An unreadable location is reported; `auto` continues with the other one.
- Session digests move from `processes.data_root/digests/` to the rebuildable cache `<cache_root>/digests/` (digest schema 2.0.0: `file` depends only on the content, `location` says where the copy was read). They are rebuilt when the decoded content's SHA-256, the settings or the application version changes, and for Codex when the thread's names in `session_index.jsonl` differ. `digests.cache: false` keeps no cache. The same content in two files of one run is digested once. Existing `processes.data_root/digests/` folders are no longer read and are not deleted.
- Evidence IDs use the first 12 hex of the decoded content's SHA-256, so `.jsonl` and `.jsonl.zst`, and the application's folder and the archive, give the same ID for the same content.
- `actions link` report schema 2.0.0: `sources`, `skippedSources`, `digestCache` and `readFrom` replace `archives` and `skippedArchives`; each row's `digest` names `source`, `location`, `relativePath` and `contentSha256`. Work folders and the digest cache must not overlap the Vault or any folder named under `chat_sources`, whatever its mode and even when the entry is disabled.
- Configuration schema 11.0.0 (`archives` removed, `chat_sources` and `digests` added). Files of schema 10 stop with an error and are not converted. Layer validation errors name the key.

## 0.49.0 - 2026-11-06

- Align the names with the repository `tkn_genai_chat_note`: distribution `tkn-genai-chat-note` (was `tkn-genai-chat-note-pipeline`), user directory `~/.tkn/genai_chat_note` and cache `~/.cache/genai_chat_note` (were `…_pipeline`). The CLI `tkn-genai-chat-note` and the package `tkn_genai_chat_note` are unchanged.
- Ownership markers keep `applicationId: tkn-genai-chat-note-pipeline`, so existing stores and archives stay readable. New normalization records name the software `tkn-genai-chat-note`.
- Remove the migration support, since the CLI has no other users and the only existing setup already uses the new names: the `config migrate` and `storage migrate` commands (and the `storage` command group), the detection of earlier application directories and their default storage, the in-memory handling of older configuration schemas and retired keys (`summary_prompt`), and the acceptance of earlier store layouts (migration receipts, the earlier ownership marker `.tkn-codex-context-root.json`, markers without a source identity, Raw layouts of storage 2-4).
- `actions link`: an A clue that only names the Action file in a command's arguments no longer links by itself; it is reported as a weak link and counts only together with another clue for the same Action, and it no longer makes the Action a target. A conversation that links to nothing, but whose repository matches a finished Action, is reported as `followup` (a candidate for a new Action in stage 1). Report schema 1.1.0.
- A configuration file must state schema 10.0.0; an older schema stops with an error and is not converted. `status` no longer reports `migrationRequired`, and `config list` no longer reports `configSchema.hasInMemoryMigrations` or `layers[].migration`.

## 0.48.0 - 2026-11-05

Stage 0 of the role redesign: the CLI moves from calendar reviews toward source-cited proposals for existing notes (see `docs/design/role-redesign_ja.md`).

- Rename the project to `tkn_genai_chat_note_pipeline`: distribution `tkn-genai-chat-note-pipeline`, package `tkn_genai_chat_note`, CLI `tkn-genai-chat-note`, user directory `~/.tkn/genai_chat_note_pipeline`, cache `~/.cache/genai_chat_note_pipeline`. Storage-5 ownership markers and store identities are unchanged; usage reports written by 0.47.0 are still recognized.
- `scheduled-run` only captures and normalizes; it no longer generates weekly, monthly or yearly reviews. `review-notes build`, `due` and `validate` remain as manual commands without new features. Existing Review Notes are not deleted.
- Configuration schema 10.0.0: GenAI connections are defined in this file under `genai_profiles` (tkn_genai_bridge profiles plus `limits` and Ollama `model_digest`), selected by `generation.genai_profile` and `--profile`. The shared Bridge configuration (`bridge_profile`) is no longer read. Price scenarios reference `genai_profile` and `model`. Schema 9 is rejected with guidance.
- Add `config migrate`: convert a schema-9 file, copying each referenced shared Bridge profile once (checked to be identical), and move the previous user config to the new location. Old files are never modified. Omitted storage roots that still live under the previous default directory are pinned; processing stops instead of starting an empty store there.
- `--provider` prefers a connection the user declared over a built-in default of the same provider.
- Rename the Task Scheduler example to `scripts/register-scheduled-task.ps1` (default task name `TknGenAIChatNote`).
- Add `actions link --dry-run`: read recent Codex and Claude Code conversations from tkn-genai-chat-archiver archives, build session digests (the person's requests, final answers, paired tool executions, Vault notes opened or changed, facts) and match conversations with Action notes by clues A-D only. The report goes to `processes.state_root`; nothing is written to the Vault and no generative AI is used.
- Configuration adds `archives`, `utc_offset_minutes`, `path_aliases`, `processes` and `vault` (all Vault conventions are settings).
- Depend on `tkn-genai-chat-archiver` (`ArchiveView`) and `tkn-genai-chat-search` (`library`) for reading archives and log formats. Both are pinned to GitHub commits, like `tkn-genai-bridge`, so the project installs without sibling checkouts.

## 0.47.0 - 2026-11-02

- Read Raw archives managed by tkn-genai-chat-archiver without writing to them. A raw_root containing `.tkn-genai-chat-archive.json` is read only: `clone`, `pull` and `scheduled-run` use the latest version of each path from confirmed archive manifests, after checking stored and decoded sizes and SHA-256.
- Read Codex `.jsonl.zst` conversations. Same-content `.jsonl` and `.jsonl.zst` copies form one input that keeps the earliest captured reference, so compression does not regenerate Canonical Events.
- Read the Codex app state and session titles from the archive. Decoded copies go to the source cache; dry runs use a temporary folder.
- Refuse `raw ingest` and `storage migrate` for archive-managed roots. Write no copy, manifest, metadata snapshot, ownership marker or lock below such a raw_root.
- Add the `zstandard` dependency.

## 0.46.0 - 2026-10-29

- All commands emit a final, copyable `key=value` result by default. `--json` selects one machine-readable JSON document; `--full-output` includes every report item in either format.
- Accept `--json` and `--full-output` at every command level. Detail selection is independent of format, logging verbosity, TTY, pipes and redirection.
- Preserve failures, attention targets, reasons and counts in ordinary output; summarize routine details with saved report paths or `--full-output` guidance. Display diagnostic target paths as separate fields.
- Emit selected-format argument/configuration/runtime errors and interrupted results, retaining exit codes 0/1/2 and handling interruption as 130. Keep ordinary help/version output.
- Keep Python API results, saved artifacts, config schemas, side effects and dry-run behavior unchanged.

## 0.45.1 - 2026-10-26

- Report undated history once per source, including resolution guidance; remove duplicate final and misleading generic review errors.
- Preserve distinct generation/protection errors and identify scheduled capture failures with source-specific reports.

## 0.45.0 - 2026-10-25

- Skip empty weekly, monthly and yearly periods without creating notes; preserve existing empty notes.
- Count only actual pending work, inspecting remaining periods without inference after generation stops and excluding empty/current periods.
- Validate saved history before classifying periods as empty; report missing, corrupt or failed source inputs as errors.
- Report undated source messages separately from pending periods, allowing dated work to continue without claiming full completion.

## 0.44.0 - 2026-10-25

- Stream review preparation, model call start/response duration, cache reuse and period results during execution.
- Distinguish model call and runtime limits; warn on incomplete generation with remaining periods and resume instructions. Save structured stop details and include deferred periods in pending counts.

## 0.43.0 - 2026-10-24

- Accept shared options before, between or after command names, preserving existing prefix usage and mutual exclusions across command levels.
- Standardize README examples on command-first option ordering.

## 0.42.0 - 2026-10-24

- Make `review-notes due` and scheduled reviews catch up all saved history regardless of configured start dates or initial run dates.
- Remove the `--all-history` option; keep closed-period selection, output protection and resumable generation limits.

## 0.41.0 - 2026-10-24

- Add `review-notes due --all-history` to discover the first saved source event and build closed weekly, monthly and yearly periods without configuring dates.

## 0.40.2 - 2026-10-16

- Identify Codex chats in Japanese/English review titles for every period.
- Add an Overview heading and collect general notices plus overview qualifications/checks under Coverage and sources, separating them from the opening summary without inference.

## 0.40.1 - 2026-10-16

- Add flat Frontmatter v3.1 properties for previous-period availability, period/source, model-input reference status, selected-item count and distinct cited-item count.
- Derive metadata from saved comparison snapshots; keep legacy unknown states explicit and refresh existing Markdown without inference or content/date changes.

## 0.40.0 - 2026-10-15

- Compare current findings with up to six related items from the immediately preceding weekly/monthly/yearly JSON, enabled by reviews.previous_period.
- Keep historical snapshots and citations separate from current-period evidence; require current evidence for every item and preserve historical proposals, uncertainty and dates.
- Select context without inference and add it only to existing composition/audit stages; reuse extraction caches when the previous review changes.
- Avoid recursive historical carryover and re-aggregate current dated supports for lower-period rows containing comparisons. Retain the existing Markdown sections and prose limits.

## 0.39.0 - 2026-10-15

- Prefer actual chat names from the local session index, with meaningful request-text fallbacks; exclude standalone app-page metadata from normalization, review inputs and bounded Raw neighbors.
- Link each item to its exact evidence subset and expose bounded, deduplicated source excerpts, speakers, original timestamps and event positions in collapsed source details.
- Retain supported reusable explanation takeaways in question/learning text through extraction, synthesis and audits without adding a model stage or changing main-body layout.
- Group coverage limitations by kind and interpretation instead of conflating compaction, truncation, retrieval limits and normalization failures; retain all original diagnostics.

## 0.38.0 - 2026-10-15

- Classify Markdown as chatReview, identify sourceApplication: Codex independently of generation provider, and place created/updated/noteId last in Frontmatter v3.
- Record note creation/content-update dates for newly generated JSON; retain unknown legacy creation dates and preserve lifecycle dates for unchanged content or presentation-only changes.
- Package default-jp/default-en YAML and Markdown templates, schema, generation/audit/repair prompts and request framing; keep source/citation transformation in Python.
- Preserve existing inference request contents during relocation; track external request-template changes separately from presentation refreshes.

## 0.37.0 - 2026-10-14

- Add grouped, flat review Frontmatter v2 with description, JSON URI, stable noteId and stored generation provenance; preserve date strings and distinguish requested model from confirmed telemetry.
- Package a shared Frontmatter template and refresh presentation from saved JSON without inference, retaining edited/reviewed notes and backing up previous output.
- Document review resources versus the retired Session Note profiles; keep JSON v1 and existing review prose unchanged.

## 0.36.0 - 2026-10-14

- Pack source audits by shared evidence while preserving every selected excerpt, per-item citation scope, and final note order.
- Use lossless request-local evidence aliases and restore canonical IDs before validation, caching, and storage; include mappings in cache identity.
- Record repeated source transmission, retain correction/completion and overview checks for all review periods, and reuse unchanged extraction/coverage caches.

## 0.35.0 - 2026-10-14

- Reconcile distinct extracted findings even when citations overlap, and carry the sources of mapped findings into each theme's audit so later decisions and completion reports remain available.
- Check the overview after the body, using the source-audited statements and their citations, without rewriting the body.
- Allocate row budgets within one review-wide character ceiling and validate unchanged rows against their allocations too; keep bounded repairs and publication protection.
- Apply the same checks to weekly, monthly and yearly reviews, recording allocations and overview decisions in the audit trail.

## 0.34.0 - 2026-10-14

- Bound weekly/monthly/yearly composition and source corrections by item and prose limits.
- Limit restoration to six consequential findings and two new topics per review; keep considered
  findings and omission reasons in JSON instead of expanding the readable note into a full inventory.
- Reject overlong replacements, repair bounded responses once, and preserve existing notes on failure.
  Include measured counts in length errors and resend only affected rows and sources for their repair.
- Include turn-ending and terminal replies plus the next user message in audit context to avoid
  mistaking progress-only excerpts for absent completion or correction reports.

## 0.33.1 - 2026-10-14

- Label the insights section neutrally so source-backed hypotheses from a single conversation
  are not presented as conclusions supported by multiple conversations.

## 0.33.0 - 2026-10-13

- Automatically reconcile missing findings and verify composed weekly/monthly/yearly items against
  bounded source excerpts before publication, including request/completion and temporal scope.
- Split unrelated topics and retain material reuse purposes; validate audit coverage and per-item
  citation permissions, and retain the quality decisions in JSON.
- Share repeated sources within audit batches, checkpoint completed audits, and resume under existing
  budgets. Failed audits preserve existing notes; added model calls appear in usage records.

## 0.32.0 - 2026-10-13

- Edit weekly, monthly and yearly findings in a dedicated final synthesis pass; connect themes,
  decisions, outcomes and remaining questions instead of repeating classified event lists.
- Render thematic tables, narrative cross-chat insights and verifiable AI improvement proposals.
  Group source links by document and retain full excerpts and exact claim citations in JSON.
- Reuse extraction checkpoints when editorial policy changes; budget and cache the final pass,
  including single-batch reviews. Empty periods still require no model calls.

## [Unreleased]

## [0.31.0] - 2026-10-13

- 出力検証エラー時は応答だけを修復し、呼出し上限を超えた場合も元資料を再送せず再開できるようにする。

- 週次レビューの通常ツール本文を出典付きの索引へ圧縮し、診断抜粋と会話本文を優先。原文は保存したまま必要時に照合する。
- 会話ごとのまとまりを保って分割し、正規化失敗を対象範囲の不足として明示する。
- Raw照合でUnicode区切り文字を物理行の改行と誤認しないよう修正。

## [0.30.0] - 2026-10-12

### 変更

- Session Note生成・検証コマンド、専用プロファイルとテンプレートを削除。週次はRawから機械的に正規化したイベントを直接使用する。既存の生成済みファイルは削除しない。
- config 9.0：レビュー設定を `sources.<id>.reviews` へ統合。出力と実行状態は各sourceのdata/state配下のreviewsへ保存する。sourceごとに独立した週次→月次→年次を生成。
- `clone` / `pull` は取得・正規化のみ。`scheduled-run` は取得・正規化後に期限到来レビューを生成。Session Noteの生成上限・idle設定・言語プロファイルを廃止。
- 旧8.x設定は明示更新が必要。旧source横断レビューは自動移行しない。Raw・正規化イベントのstorage 5は継続利用できる。

## [0.29.1] - 2026-10-12

### 変更

- 年次レビューの主入力を週次から月次JSONへ変更。未生成・不正な月次はその月の週次へ戻り、不足する元資料を補完する。
- 月次・週次からRawまでの出典を保持し、年次でも限定的な原文照合を可能にする。入力一覧に `monthlyInputs` を追加。
- 月次の判断理由・未解決事項・候補・留保を年次へ引き継ぐ生成指針と、日英ドキュメントを更新。

## [0.29.0] - 2026-10-12

### 追加

- 週次・月次・年次の `review-notes build` と、未処理期間に追いつく `review-notes due`。月次と年次はそれぞれ週次を主な入力にし、期間境界や不足情報は元資料で補完。
- 人向けMarkdownと、出典・留保・Intelligence候補・生成AIの改善案を持つJSON。限定的なRaw照合、入力ごとの再利用、途中結果の再開、編集済み・確認済み出力の保護。
- 日次のRaw保全と期限到来時の生成を行う `scheduled-run`。Windows Task Schedulerの登録例（`-WhatIf`対応、登録は利用者が実行）。
- config 8.4の `reviews` 設定。既存8.x設定は読み込み時に互換変換し、設定ファイルは書き換えない。レビューの利用量記録も `build-report` の対象に追加。
- `CHANGELOG.md` を追加。

### 変更

- `config show` を `config list` に変更。既定はコピー可能な `key=value` 一覧とし、構造化出力には `--json` を追加。旧コマンドは削除したため、スクリプトや手順を更新し、JSON を使う処理は `config list --json` に変更してください。インストール済みCLIには `uv tool install . --reinstall` で反映します。
- ドキュメントのコマンド例のコードブロックを `console` から `shell` に変更（`782b4d7`）。

## [0.28.2] - 2026-09-27

### 変更

- `tkn-genai-bridge` を 0.7.0 から 0.10.0 に更新し、公開コミット `9cf534e` の ZIP URL に固定。
- 本CLIの入力は引き続きテキストのみ。Bridgeの画像入力機能は使用しません。

## [0.28.1] - 2026-09-25

### 変更

- `tkn-genai-bridge` の依存先を Git URL からコミット固定の ZIP URL に変更（`55d55d7`）。

## [0.28.0] - 2026-09-25

### 変更

- 推論処理を `tkn-genai-bridge` に統合し、共有 Bridge プロファイルを参照する設定へ移行。
- 利用量・費用の集計を Bridge の形式に対応させ、キャッシュ利用量や部分的に取得できた利用量を扱う処理を追加。
- README を日本語の `README.md` に集約し、セットアップ・実行・参照情報を再構成。

根拠: `dfd2f92`。

## [0.25.0] - 2026-09-20

### 追加

- 利用状況レポートに診断情報と根拠の追跡を追加。
- HTTP ステータス、エラー、根拠の保存先、検証失敗・通信再試行の診断をレポートと CSV 出力に追加（`f06407e`）。

## [0.24.0] - 2026-09-19

### 追加

- プロバイダーに依存しない利用量記録と、保存済みデータからの利用状況レポート生成を追加。
- 推論の処理段階ごとの利用量追跡と、レポート設定を追加（`da3a64d`）。

## [0.23.1] - 2026-09-17

### 変更

- Session Note 生成時の進捗表示を改善（`feec6c0`）。

## [0.23.0] - 2026-09-17

### 追加

- 途中結果の未解決項目を扱う `pendingStateItems` のスキーマ、構造・引用の検証、最終状態との整合処理を追加。

### 変更

- `pendingStateItems` を公開用ノートのデータから除外し、生成プロンプトと検証処理を更新（`2c49553`）。

## [0.22.1] - 2026-09-16

### 変更

- 入力検証と再生成に関するドキュメント・ログを改善（`e62b436`）。

## [0.22.0] - 2026-09-15

### 変更

- 推論入力の重複排除・圧縮を改善し、HTML や大量の抽出テキストの処理を拡充。
- 圧縮・重複排除した文字数の計測を追加し、日本語・英語プロンプトを更新（`21ac544`）。

## [0.21.2] - 2026-09-15

### 変更

- 出力動作とメタデータの扱いに関するドキュメントを更新（`51c7489`）。
- SDK の通信詳細をコンソールに出さず、警告・エラーを残すようログ表示を改善（`6b634ed`）。

## [0.21.1] - 2026-09-15

### 変更

- 予算の扱いに関するドキュメントを更新（`8006967`）。

## [0.21.0] - 2026-09-14

### 変更

- 生成設定を名前付きプロファイルで選択する構成に変更し、設定スキーマを 8.0.0 に更新。
- 生成時のプロファイル・プロバイダー情報を記録し、設定移行を対応させた（`0351f76`）。

## [0.20.0] - 2026-09-14

### 追加

- 分割生成で残る未解決項目と最終状態の整合処理を追加。
- 生成前のトークン・費用見積もりと Azure 認証関連の処理を拡充（`d7b5f33`）。

## [0.18.0] - 2026-09-14

### 追加

- Azure OpenAI・Ollama の API 推論、利用量追跡、API 上限・接続設定の検証を追加。
- ハッシュで固定した Canonical Events を用いて Session Note を評価するスクリプトを追加（`59d9977`）。

## [0.17.0] - 2026-09-14

### 追加

- 生成途中の検証済み結果を再利用するキャッシュと、再利用件数の表示を追加。

### 変更

- 推論向け入力の圧縮・重複排除を導入し、生成効率を改善（`7e93a0f`）。

## [0.16.1] - 2026-09-13

### 変更

- ノート内の画像の扱いを改善（`6d16445`）。

## [0.16.0] - 2026-09-13

### 追加

- 同一スレッドの異なる履歴を、単一の履歴に決めずに保全する機能を追加。
- 履歴分岐と取得元集合の SHA-256 を Session Note の来歴に追加。

### 変更

- 不正な JSONL レコードの診断と旧形式ログの読み取りを改善（`564febb`）。

## [0.15.0] - 2026-09-13

### 変更

- パッケージを `tkn-genai-chat-note-pipeline` から `tkn-codex-chat-note-pipeline` に改名。
- モジュール・設定・テストを `genai_chat_note` から `codex_chat_note` の名称に移行（`0322a68`）。
