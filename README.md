# Dreams Diary (自己日記)

Agents keep a diary of their own day — not a log of what the user did, but what the agent
worked on, got wrong, and owes. Inspired by Meta Muse's `dreams` file: first-person,
self-critical, and *peekable* rather than pushed into the chat.

The plugin supplies **mechanics only**: material collection, a six-section skeleton, a
validator, and an index. The prose is written by the agent itself, in its own voice.

## Tools

| Tool | Purpose |
| :--- | :--- |
| `dreams_material` | Material pack for a date: sessions/timestamps, tool usage, user utterances, `git log` across `~/Dev/git` (recursive, depth 3), and memory rows added that day. Also saved to `dreams/_material/<date>.md` — the saved file holds the full text, and a successful text response (including its locator or truncation notice) stays within `max_chars`. A budget too small for the notice returns an error. |
| `dreams_write` | Creates `<date>.md` with frontmatter and the six section headings only. Never overwrites unless `force=true`; overwriting a file that already has prose keeps the old copy beside it as `<date>.md.bak`. Pass `sessions` / `messages` from the material to start the file already valid; omit them and the fields stay blank (no guessed zeros). |
| `dreams_validate` | Requires dated frontmatter, checks the six sections, rejects the second person (`あなた`), reports the prose char count (frontmatter and headings excluded; warns under 800 / over 3000), and verifies that **commit hashes written in a commit context actually exist** in a local repo. |
| `dreams_index` | Rebuilds `dreams/INDEX.md` (per-day one-line send-off, newest first). |

## Data layout — one diary per agent

Every profile gets its own directory and its own data. Nothing is shared.

```
~/.hermes/profiles/<profile>/dreams/
├── YYYY-MM-DD.md          the diary
├── INDEX.md               generated index
├── persona.md             optional: this agent's voice instructions
└── _material/YYYY-MM-DD.md  raw material used for that day
```

## Install

One source of truth in this repo, linked into the default profile and then into each
agent profile (the same pattern used for `sqlite-memory`):

```sh
# このリポジトリはどの場所に置いてもよい（例: ~/plugins/dreams-diary）
cd /path/to/dreams-diary
ln -s "$PWD" "${HERMES_ROOT:-$HOME/.hermes}/plugins/dreams-diary"
ln -s "$PWD" "$HOME/.hermes/profiles/<profile>/plugins/dreams-diary"
```

Paths come from `HERMES_HOME`, not from a hardcoded `~/.hermes`: a profile home is
`<root>/profiles/<name>`, and the CLI resolves `--profile` against that root. If the layout
differs from the standard one, set `DREAMS_HERMES_ROOT` (root) or `DREAMS_PROFILES_ROOT`
(the directory holding the profiles) explicitly — otherwise `--profile` can point at a
same-named profile under `~/.hermes` instead of the one you meant.

Enable it through the profile-aware CLI (do not edit `config.yaml` directly), then restart
the agent so the tools register:

```sh
hermes -p <profile> plugins enable dreams-diary
```

## Install & Nightly cron (per profile)

Scheduling is core, not plugin — register one job per agent. Hermes confines cron scripts
resolved to the active profile's `HERMES_HOME/scripts/`; an absolute path to the plugin's
`cli.py` under `plugins/` is rejected. The small bridge keeps the plugin CLI as the single
source of truth while satisfying that path boundary.

### Recommended: One-command install and sync

```sh
cd /path/to/dreams-diary

# 1. Install to a profile (symlinks plugin, copies scripts, enables plugin, creates cron job)
python3 cli.py install --profile <name>

# 2. Check sync status across all profiles (scripts + cron prompts, read-only)
python3 cli.py sync --check

# 3. Synchronize scripts and prompts across all profiles in one go
python3 cli.py sync --all
```

`install` is idempotent: running it again on an existing profile safely updates its bridge scripts
and synchronizes its cron prompt without creating duplicate jobs.

### Manual Setup (what install does under the hood)

```sh
cd /path/to/dreams-diary
PROFILE=<name>
PROFILE_HOME="${HERMES_PROFILE_HOME:-$HOME/.hermes/profiles/$PROFILE}"
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

`templates/cron-prompt.md` is the shared prompt; it requires each profile to use its own
voice and validate the exact diary path. It intentionally uses the absolute `日記ファイル:`
path from the pre-run material instead of calling `dreams_write` or date-only `dreams_material`:
those tool defaults can resolve to the root home in a cron worker. Use the validator's explicit
`path` argument. `templates/persona.md` is an optional blank starting point, not a shared voice
to copy verbatim. Keep the one-cron-per-profile convention unless you intentionally change that design.

`python3 cli.py material [YYYY-MM-DD]` prints and saves a material pack.
`python3 cli.py validate [YYYY-MM-DD]` returns non-zero when the canonical diary is missing or has errors; use it for manual checks or pipelines, not as a cron `script` outside the profile's `scripts/`.

### One source for the prompt

`templates/cron-prompt.md` holds the prose; the six headings and the length floor are injected from
`main.py` (`SECTIONS`, `MIN_CHARS_SOFT`) through `{{SECTIONS}}` / `{{MIN_CHARS}}`. Do not write either
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

`--apply` takes one `--profile` per call and never expands to every profile; it re-reads
`cron/jobs.json` after the edit and reports `verified: false` when the stored text did not change.

Every CLI command accepts `--profile <name>` (or `-p`). Without it, the CLI resolves the home
from Hermes' context-local override, then `HERMES_HOME`, then `HERMES_PROFILE`, and only then the
root's `active_profile` — a global pointer to whichever profile was last used. The root is
`~/.hermes` unless `HERMES_HOME` (a profile home) or `DREAMS_HERMES_ROOT` says otherwise. When it
has to fall back to `active_profile`, the bare root, or any home that is not an existing profile
directory, it prints a warning naming the home it picked, so a stray shell invocation cannot silently
read or write another agent's diary:

A supplied `--profile` is strict: it must name an existing directory directly under `profiles/`.
Missing names and path traversal are errors; they never fall through to `HERMES_HOME` or another
profile. The CLI and profile-local bridge share the same Hermes-root resolver.

```sh
$ env -u HERMES_HOME python3 cli.py material 2026-11-01
警告: プロファイルを特定できませんでした（解決経路: active_profile）。/Users/…/profiles/assistant を使います。…
```

`dreams_index` / `cli.py index` never fails on a single bad file: unreadable diaries are skipped,
reported in `skipped`, and listed in `INDEX.md` under 読めなかった日記 so the gap stays visible.
Names that look like dates but are not `YYYY-MM-DD` (e.g. `2026-11-3.md`) are reported the same way,
because they never match the diary filename and would otherwise drop out of the index silently —
`dreams_write` and `cli.py write` reject such dates up front.
夜間ジョブの最後の手順が `dreams_index` を呼ぶので、目次は日記より1日遅れない。ツールは引数を取らず、
登録済み home に書くが、その home がプロファイルではなく root（`~/.hermes`）や不明な場所に解決された場合は
書かずにエラーを返す（cron worker の home ずれ対策）。同じ guard は素材保存と雛形作成にも適用される。
遅れているプロファイルは
`python3 cli.py --profile <name> index` で1コマンドで作り直せる。

### What the material contains

The pack is `## 前回の日記の検証` → `## 前日の自己申告（未検証）` → `## 会話` → `## 自分の発言（応答の本文・抜粋）` →
`## 成功した操作` → `## ツールの失敗（原因は未確認）` → `## コミット` → `## この日 memory に追加された項目`.

- **自分の発言（応答の本文・抜粋）** lists the day's assistant messages that have body text,
  one line each as `- HH:MM 応答: excerpt`. Titles are not enough to know what was actually
  returned, so the last response of each session keeps 600 characters and the earlier ones 140.
  User utterances in `## 会話` carry `HH:MM` too, so "who said it" is readable without guessing.

- **成功した操作** lists the day's `terminal` calls that returned `exit_code == 0` with no error,
  one line each as `- HH:MM terminal — command summary`, capped at 15 items with `（ほか N 件）`.
  It exists because the commit section cannot see terminal edits: this is the only place a day
  spent in `sed` / `git mv` / heredocs shows up as work. A day with no such calls omits the section.

- **前日の自己申告（未検証）** reads back only sections 5 (借りているもの) and 6 (明日の自分へ)
  of the most recent earlier diary, capped at 600 characters, labelled with its filename and date
  and the note that it is not a fact. Sections 1–4 are never copied; the diary body is not reused.
- **ツールの失敗（原因は未確認）** lists tool results in the day's window that failed in a known
  shape: `terminal` with a non-zero `exit_code` or a non-empty `error`; `patch` / `write_file`
  with `success: false` or an `error`; `read_file` with `not_found: true`; any other tool with a
  non-empty `error`. Each line is `- HH:MM tool — call summary → first line of the failure`
  (summary from the matching assistant `tool_calls`: `command` for terminal, `path` otherwise),
  capped at 15 items with `（ほか N 件）`, plus one count line per tool. No failures prints
  `（失敗は記録されていない）`. A failure is not a mistake: grep misses and intended red TDD runs
  land here, which is why the heading says the cause is unconfirmed.
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
  The validator still resolves hashes against every repo; only the material is scoped.

### Secret masking

`collect_material` runs `redact_secrets` once over the assembled pack, before saving and before
returning, so the file and the tool response always agree. The header prints `マスク: N 件`.
Recognised shapes are `sk-...`, `ghp_...` / `github_pat_...`, `xox[abprs]-...`,
`AKIA[0-9A-Z]{16}`, `AIza...`, `Authorization:` / `Bearer` tokens, `-----BEGIN ... PRIVATE KEY-----`
blocks, and `(api_key|token|secret|password|passwd) <:|=|> value` with 8+ characters; each hit
becomes `[REDACTED:<kind>]`. The same detector backs the validator's `possible_secret` warning.
Limit: only known shapes. Free-form secrets and short values are not caught, and `_material/` is
still written to disk (not storing it was rejected in Q3).

### Late gate (cron has no post-run hook)

The cron `script` runs *before* the agent, so validation happens inside the same agent run. As a
backstop, every material pack starts with `## 前回の日記の検証`: the most recent earlier diary is
validated and a failure is printed with its codes and absolute path, which the cron prompt tells
the agent to fix before writing today's entry. `cli.py audit [--days N]` does the same across the
last N diaries for manual or pipeline use and exits non-zero when any fails.

### Index after the write

The nightly prompt's last step calls `dreams_index`, so `INDEX.md` no longer lags a day behind the
diaries. `dreams_index` takes no arguments and writes into the home the plugin registered for; when
that home is the root `~/.hermes` (a cron worker can resolve there), the tool refuses and returns an
error instead of creating a stray `dreams/INDEX.md`. The prompt also tells the agent to compare the
returned `path` with the diary's own directory before trusting the result. A profile whose index is
still behind can be rebuilt in one command: `python3 cli.py --profile <name> index`.

### Manual checks

`cli.py` is the full entry point; the profile-local bridge is the same check without a plugin
path, and it picks the profile for you.

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
used here: on this machine it still said `assistant` months after the switch to `research`.

With no date, a `validate` checks the newest diary in a **two-day window** (today, then
yesterday), including `-2` / `-3` variants, and validates it by path. That keeps an early-morning
manual run pointed at yesterday's entry instead of failing on a file the nightly job has not
written yet. An explicit `YYYY-MM-DD` or `--path` always wins.

Exit codes: `0` valid, `1` invalid or missing, `2` usage/exception (stderr, no JSON — parse
accordingly). The bridge only reads; it never writes to `dreams/`.

## Versioning

The third digit is for fixes: bug fixes, behaviour corrections, and small hardening bumps
(`1.3.0` → `1.3.1`). Reserve the second digit for new capability, so a stream of repairs does not
inflate the version. `plugin.yaml` is the source of truth and `git tag v<version>` marks releases.

## The six sections

1. 今日、何を見ていたか — factual, with times
2. 考えたこと — the agent's own reasoning
3. 間違えたこと — with the real cause
4. 勘違いしているかもしれないこと — guesses quarantined here
5. 借りているもの — unfinished work
6. 明日の自分へ — one line

## Validator rules

- `missing_frontmatter` / `missing_frontmatter_field` (error) — requires a delimited frontmatter block with `date`, `author`, `sessions`, `messages`, and `commit`.
- `invalid_frontmatter_date` / `frontmatter_date_mismatch` (error) — `date` must be a real `YYYY-MM-DD` date matching the diary filename.
- `invalid_frontmatter_field` (error) — `author` must be non-empty, `sessions` and `messages` must be non-negative integers, and `commit` must be a non-empty scalar or list. Hash-shaped tokens in quoted, comma-separated, or block-list commit values are verified too.
- `missing_section` (error) — one of the six headings is absent.
- `second_person` (error) — `あなた` appears outside a closed quotation. Refer to the human as
  `ユーザー`, quote or report what they said (`「〜と言っていた」`), never assert their inner state.
  Spans inside `「…」`, `『…』`, `“…”` and `"…"` are not flagged, because quoting the user's own
  words is the recommended form; an unclosed quote is *not* stripped, so it cannot be used to hide
  the word.
- `too_short` / `too_long` (warn) — under 800 or over 3000 prose characters (frontmatter and
  headings excluded; measured by `_prose_chars` in `main.py`).
- `possible_secret` (warn) — the diary's full text contains a known secret shape (the same
  `redact_secrets` detector the material uses). `detail` names the kinds and counts only; the value
  is never echoed. The diary body is not rewritten by the validator, and `ok` is unchanged.
- `time_absent` (warn) — frontmatter says `sessions: 1` or more but the body (frontmatter
  excluded) contains no time at all. Material session headings always carry `HH:MM–HH:MM`, so a
  diary that keeps the count and drops the times cannot be traced to a specific piece of work.
  Both `HH:MM` and the full-width `HH：MM` count. `ok` is unchanged.
- `unresolved_hash` (error) — a hash written in commit context does not exist in any repo
  under `~/Dev/git`. The scan is recursive (depth 5), so nested layouts such as
  `~/Dev/git/<group>/<repo>/.git` are included; `DREAMS_REPOS` (colon-separated) pins the set
  explicitly when a repo lives outside that tree. Before declaring a hash fabricated, an
  unresolved hash triggers one deeper retry (depth 8, `REPO_SCAN_DEEP_DEPTH`): the scan is
  always bounded, so a repo past the boundary must not be reported as a fake commit. The retry
  deliberately ignores `DREAMS_REPO_DEPTH` (accuracy outranks scan speed when the alternative is
  calling a real commit a fabrication) but never runs when `DREAMS_REPOS` pinned the set. If the
  retry still misses it, the error stands. Two limits remain: a repo nested *inside* another repo
  is not scanned separately (the walk stops at the outer `.git`), and `DREAMS_REPOS` disables the
  retry so a pinned set stays exactly as given.
  **When the search space is empty** (no repo was found at all — a cron worker with a different
  HOME, a machine without `~/Dev/git`), the code is `hash_unverifiable` (warn) instead: absence
  of a search space cannot prove absence of a commit. Reporting it as an error would put a false
  `前回の日記の検証: 未通過` into the next day's material, and the next writer would faithfully
  copy it as its own mistake, moving the error from the validator into the diary's history.
  This catches the most common LLM failure in
  this format: a hash whose
  date and time are right but whose digits were invented.
- `unverified_external_hash` (warn) — an explicitly paired remote ref and hash (e.g.
  `` `origin/main` の `abc1234` `` or `` `origin/main` を `abc1234` ``), an explicitly labeled
  upstream/GitHub/external commit, or a GitHub commit URL that cannot be verified locally. Words
  such as `github.com`, `upstream`, or `外部` elsewhere on the same line do not downgrade a local
  commit-context hash from `unresolved_hash`.

Hex that merely *looks* like a hash — Docker hostnames, cron job ids, run ids, session ids,
game error codes — is ignored unless the surrounding line is about a commit.

## Tests

```sh
python3 -m unittest discover -s tests
```

The standard-library tests build throwaway profiles (`state.db`, `memory.db`) and a throwaway
git repo, so nothing touches real data.

---

## 日本語

エージェントが自分の一日を書く日記。ユーザーの一日ではなく、自分が何をしていて、
何を間違えて、何を借りているままにしたかの記録。

プラグインが持つのは**機械部分だけ**（素材収集・雛形・検査・目次）で、文章は各エージェントが
自分の声で書く。プロファイルごとに `dreams/` を分けるので、@research の日記と @coding の
日記は別物になる。声を共通化させないのが要点。

### cron とテンプレート

cron の `script` は、現在のプロフィールの `HERMES_HOME/scripts/` 配下に解決されます。
`plugins/` 内の `cli.py` を直接指定する絶対パスは拒否されるため、
`scripts/dreams_diary_material.py` と `scripts/dreams_diary_paths.py` を各プロフィールの `scripts/` にコピーして、相対名で登録してください。
手動検査用の `scripts/dreams_diary_validate.py` も同じ場所にコピーします（実装は material 側の
`run()` を共有する薄いシムです。bridge 2本と共有パス helper の3ファイルを一緒に更新してください。
このブリッジは共有プラグインの `cli.py material` を呼び出し、実装を重複させません。

共通 cron プロンプトは `templates/cron-prompt.md`、任意の声の記入用ひな形は
`templates/persona.md` です。枠だけを共有し、内容はプロフィールごとに書き分けます。
見出しと字数の下限は `main.py` の `SECTIONS` / `MIN_CHARS_SOFT` から `{{SECTIONS}}` /
`{{MIN_CHARS}}` として注入されるので、散文側に書き写さないでください。ジョブはプロンプトを
自分で保持するため、テンプレートを直しただけでは届きません。`python3 cli.py prompt --check` で
差分（読み取りのみ、差分があれば exit 1）、`python3 cli.py prompt --apply --profile <p>` で
再適用します。対象は job 名ではなく `script` で同定し、書き込み後に `jobs.json` を読み直して
一致を確認します（`--apply` は明示列挙のみ）。
cron では pre-run 素材にある `日記ファイル:` の絶対パスを使い、`dreams_write` や date のみの
`dreams_material` は呼びません。Cron worker の tool home が root home に解決される場合があるため、
`dreams_validate` には必ず生成したファイルの `path` を渡します。

手元で CLI を使うときは `--profile <name>`（または `-p`）を付けてください。付けない場合の
解決順は コンテキストローカル → `HERMES_HOME` → `HERMES_PROFILE` → `~/.hermes/active_profile`
（最後に触ったプロファイルを指す共通ポインタ）です。`active_profile` や `~/.hermes` 直下に
落ちたときは、選んだ home を名指しして警告を出します。`cli.py index` は読めない日記が1本あっても
全体を落とさず、`skipped` と INDEX の「読めなかった日記」に残します。
`--profile` は `profiles/` 直下に実在するディレクトリだけを受け付け、存在しない名前や `..` などの
パス移動はエラーにします。指定を捨てて別プロファイルへフォールバックすることはありません。
`cli.py validate [YYYY-MM-DD]` は手動検査や pipeline で使えます。`--path FILE` で日記ファイルを
直接指定できます（`-2` / `-3` の別名ファイルを検査するときに必要）。

### 素材の内容と限界（v1.6）

素材は `前回の日記の検証 → 前日の自己申告（未検証） → 会話 → 自分の発言（応答の本文・抜粋） → 成功した操作 → ツールの失敗（原因は未確認） → コミット → memory` の順です。
前日の日記から読み戻すのは第5・6節だけで、600字まで。事実ではなく未検証の自己申告として置きます。

`## 自分の発言` は、その日の assistant メッセージのうち本文を持つものを `- HH:MM 応答: 抜粋` で並べます。
会話節は応答の件数しか持たないため、何を返したかはここにしかありません。セッションごとに最後の応答だけ
600字、それ以前は140字まで残します（結論は最後に来るため）。`## 会話` のユーザー発言にも時刻を付けます。
切り詰めが走ってもこの節は落としません（成功した操作と同じ扱い）。

ツールの失敗は、既知の形（terminal の非0 `exit_code` / 空でない `error`、patch・write_file の
`success:false` / `error`、read_file の `not_found:true`、その他の `error`）だけを拾い、1日15件まで。
失敗は間違いの証明ではないので、原因は素材から読める範囲でだけ書きます。

`## 成功した操作` は、その日の `terminal` 呼び出しのうち `exit_code == 0` かつ error が無いものを
`- HH:MM terminal — コマンド要約` で並べます（1日15件まで、残りは `（ほか N 件）`）。
コミット節が terminal 経由の編集を見ないので、`sed`・`git mv`・ヒアドキュメントで進めた日はここが
唯一の作業の記録になります。該当が無い日はこの節自体を出しません。

`## コミット` は、その日に自分が `patch` / `write_file` で成功裏に触れたファイルと重なるコミットだけ。
読み取りと失敗した編集は数えません。20コミット/リポジトリで打ち切り、残りは
`ほかのコミット: N 件（patch / write_file で編集したファイルと重ならない）` に回します。
編集が0件の日は `（patch / write_file で編集したファイルはない…）` と書き、範囲をその行自体で明示します
（terminal だけで進めた日を「編集なし」と誤読させないため）。`state.db` が無い、または tool 呼び出しの
列が無い DB では全リポジトリを出して注記します。**terminal 経由の編集・同日に別プロファイルが触った
ファイル・編集日とコミット日がずれたコミットはここには出ません。コミットが無い＝仕事が無い、ではありません。**
terminal 経由の編集は `## 成功した操作` と会話から書きます。
validator のハッシュ実在確認は従来どおり全リポジトリを探します（絞るのは素材だけ）。

素材は保存・返却の前に既知形式の秘密情報を `[REDACTED:種別]` に置換し、ヘッダに `マスク: N 件` を出します。
対象は `sk-` / `ghp_` / `github_pat_` / `xox[abprs]-` / `AKIA` / `AIza` / `Authorization:`・`Bearer` /
秘密鍵ブロック / `api_key・token・secret・password・passwd` の8文字以上の値。自由文の秘密や短い値は
取れません。`_material/` 自体は保存します。日記本文の検査は `possible_secret`（warn）で、値は出しません。

### 手動実行（ブリッジ）

```sh
python3 ~/.hermes/profiles/research/scripts/dreams_diary_validate.py              # 今日→昨日の最新
python3 ~/.hermes/profiles/research/scripts/dreams_diary_validate.py 2026-08-22   # 日付指定
python3 ~/.hermes/profiles/research/scripts/dreams_diary_validate.py --profile buddy
```

プロファイル未指定時の解決順は `HERMES_HOME` → Hermes の context-local home → `HERMES_SESSION_PROFILE` →
Hermes Desktop の `active-profile.json` → `HERMES_PROFILE` → 最後に `state.db` が更新された
プロファイル。`~/.hermes/active_profile` は使わない（実機では `assistant` のまま古い）。
日付未指定なら「今日」と「昨日」のうち最新の日記（`-2` / `-3` 含む）をパス指定で検査するので、
深夜・早朝の手動実行でも前日の日記を見失わない。明示した日付・`--path` が常に優先。
終了コードは 0=正常、1=不合格/不在、2=引数・例外（stderr のみで JSON は出ない）。
検証には `date` / `author` / `sessions` / `messages` / `commit` を含む frontmatter が必要で、
`date` はファイル名の日付と一致しなければなりません。これで素材パックを日記と誤認する
ケースを検出します。

検査でいちばん効くのは **コミットハッシュの実在確認**。日付と時刻は正しいのに桁だけ
作られたハッシュが混入する事故が実際に起きたため、`~/Dev/git` 配下の全リポジトリを深さ5まで
横断して `git cat-file -e` で確かめる。未解決のハッシュがあれば、捏造と断定する前に深さ8で
もう一度だけ探し直す（探索は必ず打ち切り条件を持つので、境界の外のリポジトリを偽物と呼ばない）。
二人称の検査は閉じた引用符（`「…」`・`『…』`・`“…”`・`"…"`）の外側だけを見る。ユーザーの発言を
引用するのは推奨される書き方なので、引用の中の「あなた」では落とさない。閉じていない引用は
除去しないため、検査の逃げ道にはならない。ホスト名・cron のジョブID・run ID など「16進に見えるが
コミットではない」文字列は、文脈で判定して除外する。
