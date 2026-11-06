# Dreams Diary (自己日記)

Agents keep a diary of their own day — not a log of what the user did, but what the agent
worked on, got wrong, and owes. Inspired by Meta Muse's `dreams` file: first-person,
self-critical, and *peekable* rather than pushed into the chat.

The plugin supplies **mechanics only**: per-profile material collection, a blank diary starter,
validation, an index, and CLI-based profile setup/sync. The prose is written by the agent itself,
in its own voice.

## Tools

| Tool | Purpose |
| :--- | :--- |
| `dreams_material` | Material pack for a date: sessions/timestamps, tool usage, user utterances, that day's discovered commits across `~/Dev/git` (recursive, max depth 5 by default, then scoped by successful `patch` / `write_file` edits), and memory rows added that day. `DREAMS_REPO_DEPTH` changes the scan depth; `DREAMS_REPOS` pins the repository path list. The full redacted pack is saved to `dreams/_material/<date>.md` when saving succeeds. The response, including its locator or truncation notice, is bounded by `max_chars` (default 14,000). A budget too small for the notice returns an error. |
| `dreams_write` | Creates `<date>.md` with frontmatter, a `# <date>` title, and an empty prose body; it does not create section headings or prose. Never replaces an existing file unless `force=true`. When replacing a file with prose, it attempts to copy the old contents to `<date>.md.bak` first; a backup write failure does not stop the overwrite. Pass measured `sessions` / `messages` when known; omitted values stay blank and must be filled before the required frontmatter validates. Returns the paragraph flow and the resolved user name/source. |
| `dreams_validate` | Requires dated frontmatter; errors on the literal `あなた` outside supported closed quotations (not a general second-person or mind-reading detector); warns below 800 / above 3000 prose characters, when a session diary has no clock-like time, or when it finds a known secret shape. It checks commit-context hashes in discovered local repositories: an unresolved hash is an error when the search path list is non-empty, an empty path list yields `hash_unverifiable` (warn), and a hash explicitly marked external that is unresolved locally yields `unverified_external_hash` (warn). Section headings are not required. |
| `dreams_index` | Rebuilds `dreams/INDEX.md` (per-day excerpt = the last paragraph, newest first, clipped to 200 characters). `skipped` lists unreadable diaries and non-canonical date-like filenames. |

## Data layout — one diary per agent

Every profile gets its own directory and its own data. Nothing is shared.

```
~/.hermes/profiles/<profile>/dreams/
├── YYYY-MM-DD.md          the diary (date title + prose without section headings)
├── INDEX.md               generated index (last-paragraph excerpt)
├── persona.md             optional: this agent's voice instructions
├── user.md                optional: who the writer is writing about
│                          `---\nname: まゆ\n---` — explicit override for the form of address
│                          (otherwise memories/USER.md, memories/MEMORY.md, then memory.db)
└── _material/YYYY-MM-DD.md  raw material used for that day
```

## Install

This repository is the single source of truth. `install` links it directly into each target
profile; it does not require or create a separate default-profile link:

```sh
# 手動で profile にリンクする場合（install コマンドを使うなら不要）
cd /path/to/dreams-diary
HERMES_ROOT="${DREAMS_HERMES_ROOT:-$HOME/.hermes}"
PROFILE_HOME="$HERMES_ROOT/profiles/<profile>"
mkdir -p "$PROFILE_HOME/plugins"
ln -s "$PWD" "$PROFILE_HOME/plugins/dreams-diary"
```

`install --profile <name>` creates the per-profile link itself. Set `DREAMS_HERMES_ROOT` when using
a non-default Hermes root.

Profile-scoped CLI commands resolve `--profile` under the Hermes root (`DREAMS_HERMES_ROOT`,
then the root inferred from `HERMES_HOME` or Hermes context, then `~/.hermes`). A profile home is
`<root>/profiles/<name>`. Data commands without `--profile` resolve the home from Hermes'
context-local home, then `HERMES_HOME`, `HERMES_PROFILE`, `active_profile`, and the root.
`DREAMS_PROFILES_ROOT` overrides the profile directory used by install/sync and prompt check/apply;
it does not change where data commands resolve `--profile`.

Enable it through the profile-aware CLI (do not edit `config.yaml` directly), then restart
the agent so the tools register:

```sh
hermes -p <profile> plugins enable dreams-diary
```

## Install & Nightly cron (per profile)

Scheduling is core, not plugin — register one job per profile. Hermes confines cron scripts
resolved to the active profile's `HERMES_HOME/scripts/`; an absolute path to the plugin's
`cli.py` under `plugins/` is rejected. The small bridge keeps the plugin CLI as the single
source of truth while satisfying that path boundary.

### Recommended: One-command install and sync

```sh
cd /path/to/dreams-diary

# 1. Install to a profile (links plugin, copies scripts, calls plugins enable, creates or updates the job)
python3 cli.py install --profile <name>

# 2. Check sync status across all profiles (scripts + existing cron prompts, read-only)
python3 cli.py sync --check

# 3. Synchronize scripts and existing prompts across all profiles (does not create missing jobs)
python3 cli.py sync --all
```

When `install` finds an existing job by its `script` (`dreams_diary_material.py`), it updates that
job's prompt and synchronizes the bridge scripts instead of creating another job. A symlink at
`plugins/dreams-diary` is updated if it points elsewhere; a non-symlink entry at that path is left
untouched. If `cron/jobs.json`
is unreadable, the current implementation treats it like no matching job and may create a duplicate;
repair the file before rerunning `install`.

### Manual Setup (what install does under the hood)

```sh
cd /path/to/dreams-diary
PROFILE=<name>
HERMES_ROOT="${DREAMS_HERMES_ROOT:-$HOME/.hermes}"  # set explicitly for a non-default Hermes root
PROFILE_HOME="$HERMES_ROOT/profiles/$PROFILE"
mkdir -p "$PROFILE_HOME/plugins" "$PROFILE_HOME/scripts"
ln -s "$PWD" "$PROFILE_HOME/plugins/dreams-diary"
cp scripts/dreams_diary_material.py scripts/dreams_diary_validate.py scripts/dreams_diary_paths.py "$PROFILE_HOME/scripts/"
hermes -p "$PROFILE" plugins enable dreams-diary
hermes -p "$PROFILE" cron create "30 23 * * *" \
  --name dreams-diary \
  --script dreams_diary_material.py \
  --continuity \
  "$(python3 -c 'import main; print(main.render_cron_prompt())')"
```

`templates/cron-prompt.md` is the shared prompt; it requires each profile to use its own voice and
validate the exact diary path. It uses the absolute `日記ファイル:` path from the pre-run material
instead of recollecting material or creating a second starter: this keeps the run on the pre-run date
and the exact file selected by the cron workflow. Use the validator's explicit `path` argument.
`templates/persona.md` is an optional blank starting point, not a shared voice to copy verbatim;
`install` does not copy it into a profile's `dreams/` directory.
Keep the one-cron-per-profile convention unless you intentionally change that design.

`python3 cli.py material [YYYY-MM-DD]` prints and saves a material pack. For a date-specific CLI
read, pass the date to `cli.py`. The `dreams_material` tool uses the home captured at plugin
registration and accepts `date` / `max_chars`, not a `--profile` flag.
`python3 cli.py validate [YYYY-MM-DD]` returns non-zero when the canonical diary is missing or has
errors; `validate --path FILE` checks an exact file. Use it for manual checks or pipelines, not as a
cron `script` outside the profile's `scripts/`.

### One source for the prompt

`templates/cron-prompt.md` holds the prose; the paragraph flow and the length floor are injected from
`main.py` (`FLOW`, `MIN_CHARS_SOFT`) through `{{FLOW}}` / `{{MIN_CHARS}}`. Do not write either
into the prose: each cron job stores its own copy of the rendered text, so a duplicated number goes
stale in one copy at a time. Lines starting with `<!--` are editor notes and are stripped on render.

A job keeps the prompt it was created with, so editing the template changes nothing until it is
re-applied. Jobs are identified by their `script` (`dreams_diary_material.py`), not by name — a job
can be renamed (research's was once `muse-dreams-diary`), and a name match would then miss it:

```sh
python3 cli.py prompt                             # render to stdout
python3 cli.py prompt --check                     # stored vs rendered (read-only; exit 1 on drift)
python3 cli.py prompt --apply --profile research  # rewrite, then read back and verify
```

`--apply` requires explicit `--profile` names (repeat the flag to name multiple profiles); it never
expands to every profile. It re-reads `cron/jobs.json` and reports `verified: false` if the stored
prompt still differs from the rendered text after the edit.

The `material` / `write` / `validate` / `audit` / `index` data commands accept `--profile <name>`
(or `-p`), and that name must resolve to an existing directory directly under `profiles/`.
Without it, the CLI resolves the home from Hermes' context-local home, then `HERMES_HOME`, then
`HERMES_PROFILE`, and only then the root's `active_profile` or the root itself. The root is
`~/.hermes` unless `DREAMS_HERMES_ROOT` or the active Hermes home identifies another root. When
resolution falls back to `active_profile`, the bare root, or a home that is not an existing profile
directory, it warns with the selected home. `prompt` is profile-independent. `install` can create a
profile directory; `sync` and prompt check/apply use explicit targets instead of validating them
through the data-command resolver, so treat their profile names as trusted input. The `--profile`
accepted by CLI `material` is not an argument accepted by the `dreams_material` plugin tool:

For the data commands, `--profile` is strict: it must name an existing directory directly under
`profiles/`. Missing names and path traversal are errors; they never fall through to `HERMES_HOME`
or another profile. The CLI and profile-local bridge share the same Hermes-root resolver.

```sh
$ env -u HERMES_HOME python3 cli.py material 2026-11-01
警告: プロファイルを特定できませんでした（解決経路: active_profile）。/Users/…/profiles/assistant を使います。…
```

`dreams_index` / `cli.py index` skip unreadable diary files, return them in `skipped`, and list them
in `INDEX.md` under 読めなかった日記 so the gap stays visible.
Names that look like dates but are not `YYYY-MM-DD` (e.g. `2026-11-3.md`) are reported in `skipped`,
because they never match the diary filename and would otherwise drop out of the index silently —
`dreams_write` and `cli.py write` reject such dates up front.
夜間ジョブの最後の手順が `dreams_index` を呼ぶので、目次は日記より1日遅れない。ツールは引数を取らず、
登録済み home に書くが、その home がプロファイルではなく root（`~/.hermes`）や不明な場所に解決された場合は
書かずにエラーを返す（cron worker の home ずれ対策）。同じ guard は素材保存と雛形作成にも適用される。
遅れているプロファイルは
`python3 cli.py --profile <name> index` で1コマンドで作り直せる。

### What the material contains

The pack is `## 前回の日記の検証` → `## 前日の自己申告（未検証）` → `## この人のこと（直近14日の機械抽出）` → `## 会話` → `## 自分の発言（応答の本文・抜粋）` →
`## 成功した操作` → `## ツールの失敗（原因は未確認）` → `## コミット` → `## この日 memory に追加された項目`.

- **この人のこと（直近14日の機械抽出）** is material for writing *who the user is*, not a verdict.
  It carries the resolved form of address (`dreams/user.md`, else `memories/USER.md` /
  `memories/MEMORY.md`, else `memory.db`), the `user` / `preference` memory rows added in
  the last 14 days (10 max, 120 chars each), and the words that recurred on 3+ days — katakana words
  and kanji compounds only, ranked by the number of days, `- 「語」N日`. Mechanism-cut 2–4 character
  n-grams were rejected: they produce `me`, `ージ`, `認し` fragments that say nothing about a person.
  With no material at all the section prints `（直近14日に材料は無い）`.

- **自分の発言（応答の本文・抜粋）** lists the day's assistant messages that have body text,
  one line each as `- HH:MM 応答: excerpt`. The last response of each session keeps 600 characters
  and earlier responses 140; only the latest eight responses per session are included. User
  utterances in `## 会話` carry `HH:MM` too, so "who said it" is readable without guessing.

- **成功した操作** lists the day's `terminal` calls with an integer `exit_code == 0`, no error,
  and a recovered command summary, one line each as `- HH:MM terminal — command summary`.
  It is capped at 15 items with `（ほか N 件）`.
  It complements the commit section, which cannot see terminal edits; a day with `sed` / `git mv` /
  heredocs can show those commands here. A day with no qualifying calls omits the section.

- **前日の自己申告（未検証）** reads back the most recent earlier diary's **last paragraph** (the
  day's send-off), labelled with its filename and date and the note that it is not a fact. The
  whole self-report block, including that metadata, is capped at 600 characters. For a legacy
  six-section diary the paragraph is `## 明日の自分へ`; the rest of the body is never copied.
- **ツールの失敗（原因は未確認）** lists tool results in the day's window that failed in a known
  shape: `terminal` with a non-zero `exit_code` or a non-empty `error`; `patch` / `write_file`
  with `success: false` or an `error`; `read_file` with `not_found: true`; any other tool with a
  non-empty `error`. Each line is `- HH:MM tool — call summary → failure detail`
  (summary from the matching assistant `tool_calls`: `command` for terminal, `path` otherwise),
  capped at 15 items with `（ほか N 件）`, plus a tool-count line. Terminal failures use the last
  meaningful output line; other tools use the first error/detail line. No failures prints
  `（失敗は記録されていない）`; an absent or unreadable `state.db` is reported as unavailable.
  A failure is not a mistake: grep misses and intended red TDD runs land here, which is why the
  heading says the cause is unconfirmed.
- **コミット** only shows commits whose changed files overlap files this profile successfully
  `patch`ed or `write_file`d that day. Reads (`read_file`, `search_files`) and failed edits do not
  count. Matching is absolute-path based after `Path.resolve()`. The section ends with
  `ほかのコミット: N 件（patch / write_file で編集したファイルと重ならない）`, capped at 20 commits
  per repo with `（ほか N 件）`. Zero edits prints
  `（patch / write_file で編集したファイルはない…）` — the scope is named in the line itself, because
  a terminal-only day is not a day without edits. A DB without the tool-call columns falls back to
  every repo plus `（編集を特定できないため全リポジトリ）`. Known limits:
  edits made through `terminal` (`sed`, heredocs, `git mv`) are invisible here — they appear in
  `## 成功した操作` instead — a file touched by two profiles the same day appears as "mine" in both
  packs, and a commit made on a later day never appears in the edit day's pack.
  **A missing commit is not a missing day's work.**
  The validator checks the configured/discovered repository set; only the material's commit list is
  scoped to edits.

### Secret masking

`collect_material` runs `redact_secrets` once over the assembled pack, before saving and before
returning, so the file and the tool response always agree. The header prints `マスク: N 件`.
Recognised shapes include `sk-` keys (16+ characters after the prefix), `gh[pousr]_` tokens
(16+), `github_pat_` tokens (20+), `xox[abprs]-` tokens (8+), `AKIA` keys (16 uppercase
alphanumeric characters after the prefix), `AIza` keys (30+ characters after the prefix),
HTTP authorization header values (8+ characters, optionally preceded by `Bearer `), constrained
bare `Bearer` tokens, private-key blocks, and values of 8+ non-space characters after `api_key` /
`api-key` / `apikey` / `token` / `secret` / `password` / `passwd` followed
by `:` or `=`. Each hit becomes
`[REDACTED:<kind>]`. The same detector backs the
validator's `possible_secret` warning. Only known shapes are caught: free-form secrets and short
values can pass through, and the redacted `_material/` file is still written to disk.

### Late gate (cron has no post-run hook)

The cron `script` runs *before* the agent, so there is no automatic post-run hook. The prompt
validates the newly written diary by its exact path in the same run. As a next-run backstop, every
material pack starts with `## 前回の日記の検証`; on failure, the prompt leaves the prior diary
unchanged and reports the failure rather than rewriting it. `cli.py audit [--days N]` checks the
latest N diary files (not calendar days) for manual or pipeline use and exits non-zero on errors.
Its `warn_counts` and `error_counts` summarize issue codes and levels. `audit` passes an explicit
repository list, so it does not perform the deeper hash-scan retry used by a direct `validate` call.
The previous-diary gate uses that retry when material collection uses automatic repository discovery.

Continuity is deliberately limited: the next day's material includes the previous diary's final
paragraph as an **unverified self-report**, and a newly created nightly job requests Hermes cron's
`--continuity` option. The plugin does not generate `LATEST_HANDOFF.md` or run a weekly memory-rule
promotion job.

### Index after the write

After the diary passes validation, the nightly prompt calls `dreams_index` to refresh `INDEX.md`.
When the call succeeds, the index reflects the current diary set. The tool takes no arguments and
writes into the home the plugin registered for; if that home is the
Hermes root rather than a profile, it refuses and returns an error instead of creating a stray
`dreams/INDEX.md`. The prompt also tells the agent to compare the returned `path` with the diary's
own directory before trusting the result. A stale index can be rebuilt with
`python3 cli.py --profile <name> index`.

### Manual checks

`cli.py` is the full entry point; the profile-local bridge dispatches the same validator command
without requiring a plugin repo path and can pick the profile for you.

```sh
python3 cli.py --profile research validate 2026-10-22
python3 cli.py --profile research validate --path ~/.hermes/profiles/research/dreams/2026-11-01-3.md
python3 cli.py --profile research audit --days 7

# manual bridge run: profile auto-detected, date optional
python3 ~/.hermes/profiles/research/scripts/dreams_diary_validate.py
python3 ~/.hermes/profiles/research/scripts/dreams_diary_validate.py 2026-08-22
python3 ~/.hermes/profiles/research/scripts/dreams_diary_validate.py --profile buddy
```

Without `--profile`, the bridge resolves the profile in this order: `HERMES_HOME` (set by cron
and the in-app terminal) → Hermes' context-local home → `HERMES_SESSION_PROFILE` → Hermes Desktop's
`~/Library/Application Support/Hermes/active-profile.json` → `HERMES_PROFILE` → the profile with
the most recently written `state.db`. The legacy `~/.hermes/active_profile` pointer is **not**
used here because it is a shared pointer and may be stale.

With no date, the **bridge** checks the newest diary in a **two-day window** (today, then yesterday),
including `-2` / `-3` variants, and validates it by path. That keeps an early-morning manual run
pointed at yesterday's entry instead of failing on a file the nightly job has not written yet.
An explicit `YYYY-MM-DD` or `--path` always wins. Direct `cli.py validate` without a date checks
today's canonical `<date>.md`.

Exit codes: `0` valid, `1` invalid or missing, `2` usage/exception (stderr, no JSON — parse
accordingly). The bridge only reads; it never writes to `dreams/`.

## Versioning

The third digit is for fixes: bug fixes, behaviour corrections, and small hardening bumps
(`2.0.0` → `2.0.1`). Reserve the second digit for new capability, so a stream of repairs does not
inflate the version. `plugin.yaml` is the source of truth and `git tag v<version>` marks releases.

## The diary body

The starter contains a `# YYYY-MM-DD` title; the diary body below it uses prose without section
headings. The prompt describes three required flow topics and one optional topic; paragraph
boundaries and wording are left to the profile. `main.py`'s `FLOW` supplies that description:

1. what the user was doing today and **what kind of person they seemed to be** — quote an utterance
   with its time (include each session's start/end time when the material has them), and state how
   sure you are (「かなり確信している」／「まだ手探り」)
2. what you decided, where you were wrong, and **what that cost the user** (made them wait, redo it,
   or spend effort on your behalf)
3. what you will do first tomorrow, in order (1–3 items)
4. (optional) what you are still unsure about

The prompt asks for at least 800 prose characters by default, with about 1,200 as a target; the
floor comes from `MIN_CHARS_SOFT`. The validator warns below 800 or above 3,000. Legacy six-section
diaries remain accepted by format: headings are not required or checked, but the other validator
rules still apply. The index and next day's material read the **last paragraph**, which for a legacy
diary is `## 明日の自分へ`.

The form of address is resolved in this order: `dreams/user.md` (`name:`) → `memories/USER.md` →
`memories/MEMORY.md` → `memory.db` (`user` / `preference` rows). Only an explicit statement counts
(`ユーザーの名前は「まゆ」`, `name: まゆ`, `the user's name is …`); a plain fact like "User prefers
short answers" is not a name, and an agent handle (`@coding`) or a placeholder (`ユーザー`) is
rejected. With nothing recorded the writer says 「ユーザー」. Names are never inferred from the
material or from conversation.

## Validator rules

- `missing_frontmatter` / `missing_frontmatter_field` (error) — requires a delimited frontmatter block with `date`, `author`, `sessions`, `messages`, and `commit`.
- `invalid_frontmatter_date` / `frontmatter_date_mismatch` (error) — `date` must be a real `YYYY-MM-DD` date matching the diary filename.
- `invalid_frontmatter_field` (error) — `author` must be non-empty, `sessions` and `messages` must be non-negative integers, and `commit` must be a non-empty scalar or list. Hash-shaped tokens in quoted, comma-separated, or block-list commit values are verified too.
- `second_person` (error) — the literal `あなた` appears in the diary text (including frontmatter)
  outside a closed quotation. This is not a general second-person or user-intent detector. The
  writing prompt separately asks the agent to refer to the human by the configured name
  (`dreams/user.md` or the profile's user memory) or as `ユーザー`, quote or report what they said
  (`「〜と言っていた」`), and avoid asserting their inner state.
  Spans inside `「…」`, `『…』`, `“…”` and `"…"` are not flagged, because quoting the user's own
  words is the recommended form; an unclosed quote is *not* stripped, so it cannot be used to hide
  the word.
- `too_short` / `too_long` (warn) — under 800 or over 3000 prose characters (frontmatter and
  headings excluded; measured by `_prose_chars` in `main.py`).
- `possible_secret` (warn) — the diary's full text contains a known secret shape (the same
  `redact_secrets` detector the material uses). `detail` names the kinds and counts only; the value
  is never echoed. The diary body is not rewritten by the validator, and `ok` is unchanged.
- `time_absent` (warn) — frontmatter says `sessions: 1` or more but the body (frontmatter
  excluded) contains no clock-time-shaped text. Both `HH:MM` and the full-width `HH：MM` count.
  It is only a presence check; it does not verify that the time is a session start/end time.
  `ok` is unchanged. The prompt separately asks writers to include each session's start/end time
  when those times are in the material.
- `unresolved_hash` (error) — a hash written in commit context does not exist in the searched
  repositories. Default discovery scans `~/Dev/git` recursively through maximum depth 5;
  `DREAMS_REPO_DEPTH` changes that limit and `DREAMS_REPOS` (colon-separated) pins the repository
  path list. The deeper retry starts only when there is at least one unresolved hash; a direct
  `validate` call using automatic discovery then retries once through
  depth 8, unless `DREAMS_REPOS` pins the set. An explicitly supplied repository list and `audit`
  stay within their initial set and do not use the deeper retry. Discovery stops at the first `.git`,
  so a repository nested *inside* another repository is not scanned separately; a repo deeper than
  the retry boundary can still produce `unresolved_hash`.
  **When the search path list is empty**, the code is `hash_unverifiable` (warn): an empty search
  space cannot prove that a commit does not exist. If one or more paths were searched but none
  contains the hash, `unresolved_hash` is an error.
- `unverified_external_hash` (warn) — an explicitly paired remote ref and hash that is not found
  in the searched local repositories (e.g. `` `origin/main` の `abc1234` `` or
  `` `origin/main` を `abc1234` ``), an explicitly labeled upstream/GitHub/external commit, or a
  GitHub commit URL that cannot be verified locally. Words such as `github.com`, `upstream`, or
  `外部` elsewhere on the same line do not downgrade a local commit-context hash from
  `unresolved_hash`.

Hex that merely *looks* like a hash — Docker hostnames, cron job ids, run ids, session ids,
game error codes — is ignored unless the surrounding line is about a commit.

## Tests

```sh
python3 -m unittest discover -s tests
```

The standard-library tests build throwaway profiles (`state.db`, `memory.db`) and a throwaway
git repo, so nothing touches real data. Tests are isolated from the live cron/profile tree.

## Commit log hook (per repository)

`scripts/git_commit_log_hook.sh` is a git `post-commit` hook. After each commit made under a
Hermes profile (`HERMES_HOME` set), it appends one line to
`$HERMES_HOME/logs/git-commits/<YYYY-MM-DD>.tsv`:

```
<ISO8601 time>	<repo name>	<sha>	<profile>
```

Commits made without `HERMES_HOME` write nothing. The hook only appends; the material step
removes day files older than 30 days.

The diary material counts a commit as this profile's own work when either holds (OR):

- **log**: the day's file has the commit's sha on a row whose `profile` is this profile
  (`[log]`, or `[log+edit]` when both hold).
- **edit**: the commit touches a file this profile edited with `patch` / `write_file` (`[edit]`).

If the day's log is missing or unreadable, only **edit** applies, and the material says so.
Absence from the log means "not recorded", not "not mine": a commit made with `--no-verify`,
before the hook was installed, or outside Hermes is not listed by the log rule.

Install into a repository (idempotent; refuses to overwrite a different `post-commit` or when
`core.hooksPath` is set):

```sh
python3 scripts/install_commit_hook.py <repo-path>
```

The hook is copied, not linked, so re-run the installer after changing the script. Only
`dreams-diary` has it installed so far.

---

## 日本語

Dreams Diary は、ユーザーの一日ではなくエージェント自身の仕事・判断・失敗・翌日の予定を記録する
Hermes Agent プラグインです。素材収集・空の日記ファイル・検査・INDEX・プロフィール単位のセットアップを
担当し、本文は各プロフィールのエージェントが自分の声で書きます。

### 導入と cron

`python3 cli.py install --profile <name>` は対象 profile にプラグインのリンクと3つの bridge script を用意し、
公式 Hermes CLI で plugin enable を実行し、日次 cron を作成または既存プロンプトを更新します。
標準スケジュールは毎日23:30です。既存の `cron/jobs.json` が壊れて読めない場合、現在の install は
既存 job が無いものとして扱い、重複 job を作る可能性があります。修復してから再実行してください。

cron の `script` はプロフィールの `HERMES_HOME/scripts/` 配下に置いた
`dreams_diary_material.py` を相対名で指定します。`dreams_diary_validate.py` は material bridge の
`run()` を共有する手動検査用 shim、`dreams_diary_paths.py` は共有 helper です。共通プロンプトは
`templates/cron-prompt.md`、任意の声のひな形は `templates/persona.md`。本文の流れ・字数下限は
`main.py` の `FLOW` / `MIN_CHARS_SOFT` が唯一の定義元です。

cron は pre-run 素材の `日記ファイル:` に記された絶対パスに書き、その同じ `path` を
`dreams_validate` に渡します。合格後は `dreams_index` を呼び、返された INDEX のパスも確認します。
cron の prompt は各 job に保存されたコピーなので、テンプレートを変えただけでは反映されません。
`python3 cli.py prompt --check` は読み取り専用、`python3 cli.py prompt --apply --profile <name>` は
指定 profile の job に再適用します。`python3 cli.py sync --check` / `sync --all` は bridge と prompt を
各 profile で点検／同期します。`sync --all` は外部状態を変更するので、対象を確認して実行してください。

データ系 CLI の `--profile <name>` は既存の `profiles/` 直下のディレクトリに限定されます。
省略時の解決順は Hermes の context-local home → `HERMES_HOME` → `HERMES_PROFILE` →
`active_profile` → Hermes root です。最後の2経路や profile 以外の home に落ちた場合は警告します。
`install` は profile directory を作成でき、`sync` と prompt の check/apply は指定名を対象 path に使うため、これらの対象名は信頼できる値にしてください。
profile-local bridge の自動選択は
`HERMES_HOME` → context-local home → `HERMES_SESSION_PROFILE` → Desktop の active profile →
`HERMES_PROFILE` → 最終更新 `state.db` の順で、古い `active_profile` ファイルは参照しません。

### 日記と素材

`dreams_write` / `cli.py write` が作るのは必須 frontmatter と `# YYYY-MM-DD` タイトルだけです。
本文は見出しのない散文で、prompt `FLOW` の3つの必須トピック（4つ目は任意）に沿って書きます。
段落数や各トピックの分け方は規定していません。
`sessions` / `messages` を write に渡さなければ frontmatter は空欄のままなので、素材の実測値を転記してください。
既存ファイルは通常上書きされません。`force=true` で本文のあるファイルを置き換えるときは `.bak` を
作ろうとしますが、バックアップの書き込みに失敗しても置き換えは続きます。

素材パックの順番は、前回日記の検証 → 前日の自己申告 → この人のこと → 会話 → 自分の発言 →
成功した操作 → ツールの失敗 → コミット → memory です。前日の日記から読むのは最終段落のみ（最大600字）で、
未検証の自己申告として扱います。呼び名は `dreams/user.md` → `memories/USER.md` →
`memories/MEMORY.md` → `memory.db` の順に、明示された名前だけを使います。

会話には時刻付きのユーザー発言、`自分の発言` には assistant の本文抜粋（各セッション末尾600字、
それ以前140字、最大8件）を含みます。`成功した操作` は整数 exit code が0で error のない terminal 呼び出しのうち、
コマンド要約を復元できるものを最大15件記録します。失敗節も既知の失敗 payload のみを最大15件記録し、
terminal の詳細は末尾の有意な行、他ツールは先頭の詳細を使います。コミット節はその日に成功した
`patch` / `write_file` の変更ファイルと重なるコミットに絞ります。terminal 経由の変更、日をまたいだコミット、
複数プロフィールが触った同一ファイルは正確に帰属できません。

素材は保存・返却前に既知形式の秘密情報を `[REDACTED:<kind>]` に置換しますが、自由文や短い秘密を全て検出する
保証はありません。日記の `possible_secret` は warn のみで、値を表示せず本文も書き換えません。

### 検査と継続性

validator は frontmatter、`あなた`（対応する閉じた引用符の外側）、字数（800未満 / 3000超は warn）、
セッションがある本文の時刻（時刻形式が1つでもあるかだけを確認する warn）、秘密情報、コミット文脈の hash を検査します。
見出しの有無は検査しません。ハッシュ探索は標準で `~/Dev/git` を深さ5まで行い、直接の validate で自動探索した場合は
未解決 hash を深さ8まで一度だけ再探索します。`DREAMS_REPOS` は探索先を固定し、`audit` も最初の探索先だけを使います。
探索先が空なら `hash_unverifiable` warn、探索先があるのに見つからない local hash は `unresolved_hash` error です。

cron の script は pre-run なので自動 post-run hook はありません。書いた日記は同一 run で exact path を検査し、
翌日の素材生成時にも直前の日記を再検査する遅延ゲートがあります。cron job は Hermes の `--continuity` を
要求しますが、これは別の通常セッションへの申し送り機能ではありません。プラグインは `LATEST_HANDOFF.md`、
`build_handoff()`、週次の memory ルール昇華を実装していません。

詳細なコマンド例、validator issue、Material の仕様は上記英語セクションを参照してください。
