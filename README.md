# Tkn GenAI Chat Note — 生成AIとの会話ログから、既存のノートへの差分案を作るCLI

`tkn-genai-chat-note` は、Codex・Claude Codeの会話ログ（各アプリの保存場所、または [tkn-genai-chat-archiver](https://github.com/tuckn/tkn_genai_chat_archiver) のアーカイブ）を読み、作業ノート（Action）や日次のLogなど、**既存のノートへの差分案**を作るCLIです。

> [!NOTE]
> 役割を作り替えている途中です（[設計案](docs/design/role-redesign_ja.md)）。今はAIの日記の段階2a（生成AIを使わない一覧と数え上げ）までです。Vaultに書くのは `ai-logs build` が作るAIの日記だけで、既存のノートはまだ変えません。

## 1. これは何か

生成AIとの会話には、本人の判断・依頼・作業の実施記録が多く残っています。一方で、作業ノートの進捗・方針・関係の更新は追いつきません。このCLIは、会話から「既存のノートのどこを、どう変えるか」の候補を少数だけ作り、本人が採否を決める形にします。会話を要約した新しいノートは作りません。

- 根拠は、会話の内容（展開した後のSHA-256）と行で示します。本人の発言からの引用は、プログラムが文字どおり一致するかを確かめます。
- 既存のノートの本文は、本人が採用するまで変えません。
- 利用者ごとに違うもの（フォルダ、ファイル名、Frontmatterの項目、記録の行の形、生成AIの接続先）は設定で持ちます。

### 1.1. 段階と今できること

| 段階 | 内容 | 状態 |
| --- | --- | --- |
| 0. 準備 | 名前の変更、`scheduled-run` から期間レビューを外す、生成AIの接続を `genai_profiles` に移す、会話の要点、会話とActionの対応づけの試行 | 0.48.0で一部（下の注） |
| 1. Actionの照合 | 既存のActionの差分案のブロック、下書きのAction | 始める前の準備（読む場所の `mode`、会話の要点のcache、根拠ID）だけ0.50.0で実装 |
| 2. AIの日記 | AIの側の記録を1日1ファイルで作る | 2a（生成AIなし。会話の一覧と数え上げ）を0.51.0で実装。2b（各節を生成AIで書く）は未実装 |
| 3. dreams | AIの日記の1週間分を振り返り、残すべき学びの候補を出す | 未実装 |
| 4. 片付け | 期間レビュー・Raw取得・旧profileを外す | 未実装 |

注：今できるのは、名前の変更、`scheduled-run` の変更、`genai_profiles`、会話の要点、会話とActionの対応づけの試行（`actions link --dry-run`、[3.1節](#31-会話とactionを対応づける試行)）、AIの日記（`ai-logs build`、[3.2節](#32-aiの日記を作る)）です。既存のノートにはまだ何も書きません。

### 1.2. 前提

設定でほかの利用者・環境でも使えるようにしていますが、次を前提にします。

- 作業ノートの `Notes` に、日付・種類・主体・根拠付きの記録の行を残す運用。この運用をしない場合、Actionの照合の価値は小さくなります。
- 差分案のブロックはObsidianのcalloutの書き方です。ほかのMarkdownの道具では、ただの引用として見えます。
- 既定ではアプリの保存場所（`~/.codex`・`~/.claude`）を読むので、`tkn-genai-chat-archiver` は要りません。アプリが消した古い会話まで読むには、archiverを使い、読む場所を `auto` にします（[3.1節](#31-会話とactionを対応づける試行)）。
- CodexとClaude Codeのログの内部形式に依存します。形式はアプリの版によって変わることがあります。
- 最初は日本語の既定値だけを同梱します。

### 1.3. 依存するライブラリ

| ライブラリ | 使うもの |
| --- | --- |
| [tkn_genai_chat_archiver](https://github.com/tuckn/tkn_genai_chat_archiver) | `ArchiveView`：アーカイブを読み取り専用で読む（確定したmanifestだけ、SHA-256の照合、zstdの展開） |
| [tkn_genai_chat_search](https://github.com/tuckn/tkn_genai_chat_search) | `library`：Codex・Claude Codeのログの読み取り（アプリの保存場所から読んだバイト列も、アーカイブから読んだバイト列も同じ関数で読む）。発言、ツール実行の組（`call_id`・`tool_use_id`）、発言の書き手（`turnOrigin`） |
| [tkn_genai_bridge](https://github.com/tuckn/tkn_genai_bridge) | 生成AIの呼び出し（期間レビュー。段階1からActionの照合でも使う） |

ログの形式の変化への対応は、読み取りを持つこれらのリポジトリで行います。

### 1.4. 段階4まで残す機能

| 機能 | コマンド | 扱い |
| --- | --- | --- |
| 週・月・年のReview Note | `review-notes build`・`due`・`validate` | 手動で実行するときだけ動きます。`scheduled-run` からは外しました。新しい機能は足しません。生成済みのノートは消しません。[Review Noteの仕様](docs/reference/review-note-format_ja.md) |
| Rawの取得・正規化 | `clone`・`pull`・`raw ingest` | Rawの保管はarchiverに移りました。archiverのアーカイブは読むだけです（[4.2節](#42-tkn-genai-chat-archiverのアーカイブを読む)） |
| 利用量レポート | `build-report` | 残します |

## 2. セットアップ

Python 3.11以上とuvを使います。生成AIのCLI（Codex・Claude Codeなど）は別にインストールしてログインしてください。

```shell
cd <repository>
uv tool install .
tkn-genai-chat-note --help
tkn-genai-chat-note config init
```

ソースやリソースを更新したら `uv tool install . --reinstall` でインストール済みCLIへ反映します。

### 2.1. 設定

設定ファイルは `~/.tkn/genai_chat_note/config.yaml` です。優先順位は、既定値 → ユーザー設定 → 作業ディレクトリの `.tkn/config.yaml` → `--config` → CLIオプションです。`config init` は編集済みのファイルを守り、`--force` を付けたときだけバックアップして置き換えます。

```yaml
schema_version: "11.1.0"
sources:
  my-pc:
    enabled: true
    source_root: ~/.codex
    include_archived: true
    raw_root: C:/path/to/codex/raw
    data_root: C:/path/to/codex/data
    state_root: C:/path/to/codex/state
    reviews:
      language: ja
      utc_offset_minutes: 540
genai_profiles:
  codex-default:
    provider: codex
    model: null
    timeout_seconds: 1800
generation:
  genai_profile: codex-default
runtime_minutes: 230
model_timeout_seconds: 1800
```

source IDと、使っているRaw・data・stateの保存先は変えないでください。保存先を省略すると `~/.tkn/genai_chat_note/<kind>/codex/<source_id>` を使います。ほかの項目は[設定例](src/tkn_genai_chat_note/resources/config.example.yaml)を参照してください。

`actions link` と `ai-logs build` には、会話を読む場所（`chat_sources`）とVaultの約束を設定します。Vaultのフォルダ名・ファイル名・Frontmatterの項目名・status・見出し・記録の行の形は、すべて設定で変えられます。

```yaml
chat_sources:
  codex-my-pc:
    app: codex                  # codex / claude-code
    source_id: my-pc-windows    # 根拠IDの一部。アーカイブも読むなら、その所有マーカーの sourceId
    mode: auto                  # app（既定）/ archive / auto
    root: ~/.codex              # アプリの保存場所。省略すると ~/.codex・~/.claude
    archive: C:/path/to/archives/Codex/my-pc-windows  # archive・auto のときだけ
  claude-code-my-pc:
    app: claude-code
    source_id: my-pc-windows
    mode: app
digests:
  cache: true                   # false：会話の要点を保存せず、毎回作る
utc_offset_minutes: 540
path_aliases:
  - ["D:/path/to/work", "C:/path/to/work"]  # 同じ場所を指す別の書き方。先頭が正
vault:
  root: C:/path/to/vault
  actions:
    dir: actions
    frontmatter:
      id: noteId
      repositories: repositories
  logs:                         # 本人の日次のLog。AIの日記からリンクするだけで、書き込まない
    dir: logs/{date:%Y}
    filename: "{date:%Y-%m-%d}.md"
  ai_logs:                      # AIの日記（ai-logs build が作る）
    dir: ai-logs/{date:%Y}
    filename: "{date:%Y-%m-%d} AI.md"
```

`dir`・`filename` には `{date:<strftimeの形>}` だけを書けます。どちらも `vault.root` からの相対の場所で、日付ごとに別のノートになる形（年・月・日を含む）にします。`processes.state_root` と会話の要点のcacheは、`processes.data_root` と重ねられません（`--dry-run` で `data_root` に書かないため）。

```shell
tkn-genai-chat-note config list
tkn-genai-chat-note config list --json
```

### 2.2. 生成AIの接続（genai_profiles）

呼び出すCLIやモデルは、この設定ファイルのルートの `genai_profiles` に書きます。[tkn_genai_bridge](https://github.com/tuckn/tkn_genai_bridge) はこのCLIの依存ライブラリ（commitで版を固定）として入ります。Bridge CLIの別途のインストールや、Bridgeの共通の設定ファイル（`~/.tkn/genai_bridge/config.yaml`）は読みません。

```yaml
genai_profiles:
  codex-high:
    provider: codex
    model: null  # null：そのCLIの既定のモデル。固定するときは、そのCLIで使えるモデル名
    reasoning_effort: high
    timeout_seconds: 1800
  claude-default:
    provider: claude-code
    model: null

generation:
  genai_profile: codex-high  # 期間レビューが使う接続。実行ごとに --profile <名前> で変えられます
```

- 各項目はBridgeのprofile（`provider`・`model`・`reasoning_effort`・`timeout_seconds`・`cli`・`ollama`・`azure`・`pricing` など）です。項目は[Bridgeの設定仕様](https://github.com/tuckn/tkn_genai_bridge/blob/main/docs/reference/configuration.md)を参照してください。
- `codex-default`・`claude-default`・`copilot-default`・`antigravity-default` は既定で使えます。
- CLI型（Codex・Claude Code・GitHub Copilot・Antigravity）は、そのCLIの既存のログインを使います。このCLI用の別のログインや、トークンの転記は要りません。
- API型（Azure OpenAI・Ollama）は、このCLIの予算 `limits`（呼び出し回数・費用・トークン）を同じ項目に書けます。APIキーは値を書かず、Bridgeの `api_key_env` で環境変数の名前だけを指定します。
- 利用量レポートの参考価格は、`usage_report.price_scenarios.<名前>` に `genai_profile` と `model` を書くと、その接続の `pricing` を使います。

## 3. 実行する

共通オプション（`--source`・`--config`・`--profile` など）は、コマンドの前にも後ろにも書けます。同じオプションを複数回書いた場合は後の値を使います。`--profile` と `--provider`、`--quiet` と `--verbose` は一緒に使えません。

### 3.1. 会話とActionを対応づける試行

```shell
tkn-genai-chat-note actions link --dry-run
tkn-genai-chat-note actions link --dry-run --days 14 --as-of 2026-11-05
```

設定の `chat_sources` の場所から直近の会話を読み、Vaultの作業ノート（`vault.actions`）と対応づけて、結果を `processes.state_root/actions-link/<実行ID>/` に書きます（`report.md`・`report.json`・`links.jsonl`）。Vaultには何も書かず、生成AIも使いません。この版では `--dry-run` が必須です。

読む場所は、アプリと取得元ごとに `mode` で選びます。どの場所も読み取り専用で、何も書きません。

| `mode` | 読むもの | 向いている場面 |
| --- | --- | --- |
| `app`（既定） | 各アプリの保存場所（`root`。既定 `~/.codex`・`~/.claude`） | 下準備なしで始めたい。archiverを使っていない |
| `archive` | `tkn-genai-chat-archiver` のアーカイブ（`archive`）だけ | アプリの保存場所が別のPCにある・読めない |
| `auto` | アプリの保存場所を先に読み、そこにない会話（アプリが消した、圧縮・移動で名前が変わった）はアーカイブから読む。同じ会話が両方にあればアプリの保存場所の方（新しい追記を含むため） | archiverも使っていて、古い会話も確実に読みたい |

- アーカイブは、所有マーカーの `profile`・`sourceId` が設定の `app`・`source_id` と一致するときだけ読みます。確定したmanifestだけを読み、相対パスごとの最新の版を使い、保存ファイルと展開後の内容のSHA-256を照合します。
- `auto` で一方の場所が読めないときは、もう一方だけを読み、レポートに示します。`app`・`archive` で場所が読めない取得元は飛ばします。
- `processes.data_root`・`processes.state_root` と会話の要点のcacheは、Vaultと、`chat_sources` に書いたすべての場所（modeで読まない場所や、無効にした取得元の場所も含む）と重ねられません。

使う手がかりは次の4つです。

| 手がかり | 強さ | 内容 |
| --- | --- | --- |
| A | 強 | 会話中にAIがActionのファイルを開いた・変えた（サブエージェントを含む）。コマンドの引数に出ただけ（A(実行)）は弱く、同じActionにほかの手がかりがあるときだけ使う |
| B | 強 | ActionのNotesの記録の行の根拠に、その会話のセッションIDがある（`同上` は前の行の根拠） |
| C | 強 | 本人の発言にActionのWikiLink・ファイル名・title・`aliases` がある |
| D | 中 | 作業フォルダの最後のフォルダ名が、対象のActionの `repositories`（なければtitle）に1件だけ一致する |

- 対象のActionは、statusが作業中、期間内に更新日時が変わった、または期間内の会話が開いた・変えたもの（終了したものを除く）。A・B・Cは対象の外のActionにも対応づけ、そう表示します。
- Dが複数のActionに一致した会話は「曖昧」とし、段階1で生成AIに選ばせます。
- どれにも対応しない会話で、作業フォルダの名前が終了したActionの `repositories`（なければtitle。titleで比べるのは `_` か `-` を含む名前だけ）に一致したものは「続き」とします。段階1では、そのActionを再開せず、新しいActionの候補にします。
- A(実行)だけの手がかりは、レポートに「弱い」と示します。
- 会話ごとに「会話の要点」（本人の発言、各ターンの最後の回答、ツール実行、開いたVaultのノート、commitなどの事実）をJSONで作り、作り直せるcacheとして `<cache_root>/digests/` に置きます。会話の内容のSHA-256・設定・このCLIの版が変わったら作り直します。消してもかまいません。`digests.cache: false` なら保存せず、毎回作ります。
- 本人の発言の判定：Claude Codeは `turnOrigin` が `human` 以外の発言（別のセッションからの依頼など）、sidechain、引数のないスラッシュコマンド、アプリが書いた行を除きます。`turnOrigin` のない古い記録は確度を1段下げて含めます。Codexは、アプリが足すブロック（`<external_…>`・`<user_action>` など）だけの発言を除きます。貼り付け（`<pasted_content>`）は本人の発言に含めます。
- 根拠ID：`<アプリ>/<source_id>/<セッションID>@<展開した後の内容のSHA-256の先頭12桁>#L<行>`。Codexが `.jsonl` を `.jsonl.zst` に圧縮しても、アプリの保存場所から読んでもアーカイブから読んでも、同じ内容なら同じIDです。`app` だけで読む場合、アプリが後で会話を消したり書き足したりすると、古いIDの内容は確かめられなくなります。後から確かめたいなら `auto` にします。

### 3.2. AIの日記を作る

```shell
tkn-genai-chat-note ai-logs build --dry-run
tkn-genai-chat-note ai-logs build
tkn-genai-chat-note ai-logs build --date 2026-11-06 --days 7
```

`chat_sources` の会話から、生成AIの作業をAIの側から記録する日記（AIの日記）を、現地の日付ごとに1つ、`vault.ai_logs` に作ります。既定は前日の1日分です。`--date` は最後の日、`--days` はそこから遡る日数です。主な読み手はAI（次の作業をするAIと、週の振り返り）で、本人のLogとは分けます。

段階2aでは生成AIを使わず、次だけを書きます。「うまくいったこと」「本人から学んだこと」「持ち越し」の節は空にします（段階2bで生成AIが書きます）。

- 今日やったこと：その日の会話ごとに、時刻・アプリ・タイトル・作業フォルダ・本人の発言の数・対応づいたAction（[3.1節](#31-会話とactionを対応づける試行)の手がかりA〜D）・根拠（`<アプリ名> <セッションID>`）。
- うまくいかなかったこと：数え上げ。読む・探す・一覧の回数、読み込みの失敗（種類ごと）、同じ検索の繰り返し、3回以上読んだファイル、広すぎる走査（深さを限らない一覧・検索、とても大きな結果）、本人の拒否、本人の訂正らしい発言（語句で拾った候補）、Actionの規則（その日に始まり作業をした会話のうち、作業の前にActionを読んだ数と、ActionのNotesにその会話を根拠にした行がある数）、数の多い会話（3件まで）。
- その日の本人のLog（`vault.logs`）があれば、リンクします。本人のLogには書き込みません。

書き方の決まり：

- 書くのは、終わった日だけです（今日の分は `--dry-run` で見るだけ）。会話のない日は作りません。
- 新しいノートは作ります。このCLIが前に書いたままのノートは、作り直します（`noteId`・`created` は変えません）。本人が変えたノートは作り直さず、まだ書いていない会話だけを `## 追加（<日時>）` として末尾に足します（Frontmatterは `updated` の行だけを変え、BOMと改行コードは元のまま）。
- 本人が変えたノートで、最後の変更から30分以内のものは、次の実行に回します（終了コード2）。書く直前にもう一度読み、計画したときから変わっていれば計画し直します。ほかのプログラムが開いていて置き換えられないノートも、待って再試行せず、次の実行に回します。
- このCLIが書いたノートを本人が消した・移したときは、書き直しません。
- 読めない取得元・場所・会話のファイル・一覧できないフォルダがあった実行では、既存のノートを作り直さず（次の実行に回す）、既存の数え上げも置き換えません。作り直したノートから、前に書いた会話が消えないようにするためです。新しいノートの作成と「追加」は行います。
- その日にメッセージがなく、ツールの実行（サブエージェントを含む）だけがあった会話も、その日の会話に数えます。サブエージェントの親の会話のファイルが古くても、親を読みます。
- 同時に1つしか動きません（`processes.state_root/ai-logs/.lock`）。
- 数え上げは `processes.data_root/ai-logs/metrics/<西暦>/<日付>.json` に残します（週の振り返りの入力にします）。各ノートを最後に書いた内容は `processes.state_root/ai-logs/notes.json` に記録し、作り直すか足すかの判断に使います。
- `--dry-run` は、Vaultと `processes.data_root` に書かず、下書き（Vaultと同じ相対の場所）と数え上げを `processes.state_root/ai-logs/<実行ID>/` にだけ書きます。
- Claude Codeの会話をforkや再開すると、前の会話の記録が新しい会話のファイルに写ります。写された記録（前の会話のセッションIDを持つ行）は、前の会話の分として数え、新しい会話には数えません（`actions link` も同じです）。
- タイトルなどに含まれるAPIキーやトークンらしい文字列は伏せます。

### 3.3. 定期実行

```shell
tkn-genai-chat-note scheduled-run --dry-run
tkn-genai-chat-note scheduled-run
```

0.48.0の `scheduled-run` は、各sourceのRawの取得（archiverのアーカイブなら読み取り）と正規化だけを行い、生成AIを呼びません。期間レビューは作りません。AIの日記（`ai-logs build`）もまだ含めていません。段階1以降、ここに新しい処理を足します。

| Task Schedulerの項目 | 設定例 |
| --- | --- |
| プログラム | インストール済み `tkn-genai-chat-note.exe` の絶対パス |
| 引数 | `scheduled-run --config "C:/path/to/config.yaml"` |
| 開始場所 | 設定で意図した作業ディレクトリ |
| トリガー | 毎日1回（archiverの後。例：archiverが02:00なら04:00） |
| 実行ユーザー | 生成AIの認証を設定したユーザー |
| その他 | 未実行分をすぐ実行、多重実行しない |

[登録スクリプト例](scripts/register-scheduled-task.ps1)は `-WhatIf` で確認できます。CLIのインストールはタスクを登録しません。

### 3.4. 期間レビューを手動で作る

```shell
tkn-genai-chat-note pull
tkn-genai-chat-note review-notes due --dry-run --full-output
tkn-genai-chat-note review-notes due
tkn-genai-chat-note review-notes build --source my-pc --period weekly --date 2026-05-18
tkn-genai-chat-note review-notes validate "C:/path/to/data/reviews/weekly/2026/2026-W21.json"
```

`due` は保存済みの全履歴から締まった期間を探し、まだないレビューを作ります。`build` は指定した期間だけを作ります。どちらも保存済みcatalogを読むので、必要なら先に `pull` してください。上限・保護・再開の詳細は[Review Noteの仕様](docs/reference/review-note-format_ja.md)を参照してください。

### 3.5. 保存・正規化だけを実行する

```shell
tkn-genai-chat-note clone --dry-run
tkn-genai-chat-note clone
tkn-genai-chat-note pull
```

`clone` は初期化・保存・正規化、`pull` は初期化済みの領域の更新です。どちらも生成AIを呼びません。`raw ingest` はRawの保存だけで、archiverが保管している `raw_root` では止まります（取得はarchiverの `capture` で行います）。

### 3.6. 確認する

```shell
tkn-genai-chat-note status
tkn-genai-chat-note provenance validate
tkn-genai-chat-note build-report --no-open
```

`build-report` は保存済みの利用量からHTMLとJSONを作ります。生成AIや価格の検索は行いません。取得できないトークン数・費用は不明として扱います。

## 4. 仕様

- `--dry-run` は推論・ネットワーク・出力・cache・state・ロックの作成を行いません。ただし `actions link` と `ai-logs build` の `--dry-run` は、レポート・下書き（`processes.state_root`）、会話の要点のcache、`ai-logs build` のロックを書きます。Vaultとアプリの保存場所・アーカイブには書きません。
- 成功は終了コード0、失敗・保護で処理できない項目ありは1、引数の誤りや上限による未完了（AIの日記を次の実行に回したときを含む）は2、中断は130です。
- 期間レビューの `max_calls` と `runtime_minutes` は、sourceごとの1回の実行に適用します。

### 4.1. CLIの結果表示

すべてのコマンドが、最終結果を標準出力へ返します。通常は1行1項目の `key=value` 一覧、`--json` を付けると1つのJSONです。進捗・警告・診断は標準エラーです。`--full-output` は形式を変えずに、表示する詳細を増やします。機械で読む場合は必ず `--json` を付けてください。

```shell
tkn-genai-chat-note status --json
tkn-genai-chat-note review-notes due --dry-run --json --full-output
```

`--quiet` / `--verbose` は標準エラーのログの量だけを変えます。表示の形式は、TTY・パイプ・リダイレクトで自動では変わりません。詳細は[CLI出力契約](docs/reference/cli-output.md)を参照してください。

### 4.2. tkn-genai-chat-archiverのアーカイブを読む

`raw_root` に `.tkn-genai-chat-archive.json` があると、そのフォルダはarchiverが保管するRawのアーカイブとして扱い、このCLIは読むだけにします。

- アーカイブの所有マーカーの `profile` が `codex`、`sourceId` が設定のsource IDと一致しなければ、何も書かずに止まります。
- 確定したmanifest（`manifests/*.jsonl`）だけを読み、相対パスごとの最新の版を使います。使う前に、保存ファイルと展開後の内容のサイズ・SHA-256を照合します。
- Codexが圧縮した `.jsonl.zst` も読みます。同じ内容の `x.jsonl` と `x.jsonl.zst` は1つの会話として扱います。
- `raw_root` には何も書きません。取り込みの新しさは、アーカイブを最後に更新した時点までです。

詳細は[Data contract](docs/reference/data-contract.md#archive-managed-raw)を参照してください。

- [設計案：役割の作り替え](docs/design/role-redesign_ja.md)
- [Review Note仕様（日本語）](docs/reference/review-note-format_ja.md)
- [Review Note contract（English）](docs/reference/review-note-format.md)
- [Data contract](docs/reference/data-contract.md)
- [変更履歴](CHANGELOG.md)

## 5. 開発・検証

```shell
uv sync --group dev
uv run python -m pytest
uv run python -m ruff check .
uv run python -m mypy src/tkn_genai_chat_note
uv build
```

テストは一時ディレクトリと合成データを使います。実データや実モデルでの品質の評価は別に行ってください。
