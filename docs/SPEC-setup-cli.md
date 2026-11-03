# dreams-diary セットアップ・運用改善仕様（案）— install / sync コマンド

状態: **決定済み（Q1〜Q3 回答済み）。実装前の最終確認待ち**。版: `1.5.x` → `1.6.0`（新機能追加のためマイナーバージョン更新）。

## 背景

現在のセットアップと運用には、手動でのファイルコピーや CLI 実行が必要で、プロファイル増加時の初期摩擦やスクリプト陳腐化のリスクがある。
Hermes 公式推奨の設計規約（`HERMES_HOME/scripts/` への薄いブリッジ配置、公式 `hermes cron` CLI の利用）を厳格に遵守した上で、これらを安全かつ冪等に自動化する。

| # | 課題 | 決定（回答） |
| :-- | :-- | :-- |
| 1 | スクリプト手動コピーとジョブ登録の摩擦 | `cli.py install`（新規導入）と `cli.py sync`（一括同期）を新設（Q1:1） |
| 2 | プロファイルごとの個別実行の手間 | `--all` フラグで全プロファイルを一括対象に指定可能とする（Q2:1） |
| 3 | 既存ジョブが存在する場合の再実行リスク | エラーにせず、スクリプトとプロンプトを最新化して完了とする（冪等性）（Q3:1） |

---

## 規約・安全性の境界（チート防止）

- **公式 CLI 経由の操作:** Cron ジョブの登録・編集・有効化は `hermes -p <profile> cron create` / `cron edit` / `hermes -p <profile> plugins enable` を実行し、`jobs.json` の直接書き換えや内部ハックは一切行わない。
- **公式パス境界の遵守:** Cron pre-run script の実行境界に従い、各プロファイルの `<profile_home>/scripts/` に薄いブリッジスクリプト（3ファイル）を配置する。
- **冪等性の保証:** 何度実行しても環境を壊さず、常に最新・正常な状態に収束する。

---

## A. コマンド仕様

### 1. `cli.py install --profile <name> [--schedule <expr>]`

新規プロファイルへの導入、または既存プロファイルへの再セットアップを行う。

- **引数:**
  - `--profile <name>`: 対象プロファイル名（必須）。
  - `--schedule <expr>`: Cron スケジュール式（デフォルト: `"30 23 * * *"`）。
- **処理手順:**
  1. **プラグインの symlink:**
     `<profile_home>/plugins/dreams-diary` から当リポジトリのルートへのシンボリックリンクを作成（既に正しいリンクがあればスキップ、別ターゲットなら更新）。
  2. **スクリプトの配置:**
     当リポジトリの `scripts/*.py`（`dreams_diary_material.py`, `dreams_diary_validate.py`, `dreams_diary_paths.py`）を `<profile_home>/scripts/` へコピー（ディレクトリが無ければ作成）。
  3. **プラグインの有効化:**
     `hermes -p <profile> plugins enable dreams-diary` を実行。
  4. **Cron ジョブの登録・更新（冪等）:**
     対象プロファイル内のジョブを走査し、`script == "dreams_diary_material.py"` を持つ既存ジョブを検索。
     - **存在しない場合:**
       `hermes -p <profile> cron create "<schedule>" --name dreams-diary --script dreams_diary_material.py --continuity "<rendered_prompt>"` を実行して新規作成。
     - **既に存在する場合:**
       既存ジョブに対して `apply_cron_prompt()` を呼び出し、プロンプトを最新の共通テンプレートに更新。
  5. **結果出力:**
     JSON 形式で各ステップの成否を出力。

### 2. `cli.py sync [--check] [--all | --profile <name> ...]`

既存プロファイルのスクリプトおよび Cron プロンプトを点検・同期する。

- **引数:**
  - `--check`: 読み取り専用の差分確認モード。スクリプトやプロンプトに変更は加えず、差分があるプロファイルがあれば終了コード 1、完全一致なら 0 を返す。
  - `--all`: 検出された全プロファイルを対象にする。
  - `--profile <name>`: 指定したプロファイルを対象にする（複数指定可）。
  - （`--all` も `--profile` も無い場合、`--check` では全プロファイル点検、適用時はエラーとする）
- **同期処理:**
  1. **スクリプトの同期:**
     各プロファイルの `scripts/` にある 3 ファイルの SHA256 をリポジトリ本体と比較。
     - `--check`: `same` / `diff` / `missing` を記録。
     - 適用時: `diff` または `missing` のスクリプトをコピーして最新化。
  2. **プロンプトの同期:**
     既存の `check_cron_prompt` / `apply_cron_prompt` と連動。
     - `--check`: プロンプトの `state`（same / diff / missing）を記録。
     - 適用時: 最新の描画結果を `hermes cron edit` で適用し、直後に読み戻し検証。
- **結果出力:**
  JSON 形式でプロファイルごとのスクリプト状態とプロンプト状態を出力。

---

## B. 既存コマンドとの関係・整理

- `cli.py prompt --check` / `cli.py prompt --apply` は後方互換性のため維持する。
- 内部ロジックは `main.py` に関数（`sync_scripts`, `check_scripts`, `install_profile` 等）として実装し、`cli.py` から呼び出す。
- テストは `tests/test_setup_cli.py` を新設し、テスト用一時ディレクトリ内でモックランナー（`runner`）を用いて Hermes CLI コマンドの呼び出し引数と成否を検証する。

---

## 受け入れ基準

1. `cli.py install --profile test-p` を実行すると、プラグイン symlink、3つのスクリプト、プラグイン有効化、cron ジョブ作成が一括で実行される。
2. 既に cron ジョブがあるプロファイルに対して `install` を再実行した場合、エラーにならずプロンプトとスクリプトが最新化されて正常終了する（冪等性）。
3. `cli.py sync --check --all` を実行すると、全プロファイルのスクリプト差分とプロンプト差分が JSON で一覧表示される。
4. `cli.py sync --all` を実行すると、全プロファイルのスクリプトとプロンプトが最新に同期され、直後の `--check` で全プロファイルが `same` になる。
5. Hermes のプライベートファイル（`jobs.json`）の直接編集は行わず、すべて公式 CLI 経由で操作される。
