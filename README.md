# Dreams Diary (自己日記)

Agents keep a diary of their own day — not a log of what the user did, but what the agent
worked on, got wrong, and owes. Inspired by Meta Muse's `dreams` file: first-person,
self-critical, and *peekable* rather than pushed into the chat.

The plugin supplies **mechanics only**: material collection, a six-section skeleton, a
validator, and an index. The prose is written by the agent itself, in its own voice.

## Tools

| Tool | Purpose |
| :--- | :--- |
| `dreams_material` | Material pack for a date: sessions/timestamps, tool usage, user utterances, `git log` across `~/Dev/git` (recursive, depth 3), and memory rows added that day. Also saved to `dreams/_material/<date>.md` — the saved file always holds the full text, even when the returned pack is capped by `max_chars`. |
| `dreams_write` | Creates `<date>.md` with frontmatter and the six section headings only. Never overwrites unless `force=true`. Pass `sessions` / `messages` from the material to start the file already valid; omit them and the fields stay blank (no guessed zeros). |
| `dreams_validate` | Requires dated frontmatter, checks the six sections, rejects the second person (`あなた`), reports char count, and verifies that **commit hashes written in a commit context actually exist** in a local repo. |
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
ln -s ~/Dev/git/hermes-plugins/dreams-diary ~/.hermes/plugins/dreams-diary
ln -s ~/.hermes/plugins/dreams-diary ~/.hermes/profiles/<profile>/plugins/dreams-diary
```

Enable it through the profile-aware CLI (do not edit `config.yaml` directly), then restart
the agent so the tools register:

```sh
hermes -p <profile> plugins enable dreams-diary
```

## Nightly cron (per profile)

Scheduling is core, not plugin — register one job per agent. Hermes confines cron scripts
resolved to the active profile's `HERMES_HOME/scripts/`; an absolute path to the plugin's
`cli.py` under `plugins/` is rejected. The small bridge keeps the plugin CLI as the single
source of truth while satisfying that path boundary.

```sh
cd ~/Dev/git/hermes-plugins/dreams-diary
PROFILE=research
PROFILE_HOME="$HOME/.hermes/profiles/$PROFILE"
mkdir -p "$PROFILE_HOME/scripts"
cp scripts/dreams_diary_material.py "$PROFILE_HOME/scripts/"
hermes -p "$PROFILE" plugins enable dreams-diary
hermes -p "$PROFILE" cron create "30 23 * * *" \
  --name dreams-diary \
  --script dreams_diary_material.py \
  --continuity \
  "$(python3 -c 'from pathlib import Path; print(Path("templates/cron-prompt.md").read_text())')"
```

`templates/cron-prompt.md` is the shared prompt; it requires each profile to use its own
voice and validate the exact diary path. It intentionally uses the absolute `日記ファイル:`
path from the pre-run material instead of calling `dreams_write` or date-only `dreams_material`:
those tool defaults can resolve to the root home in a cron worker. Use the validator's explicit
`path` argument. `templates/persona.md` is an optional blank starting point, not a shared voice
to copy verbatim. Keep the one-cron-per-profile convention unless you intentionally change that design.

`python3 cli.py material [YYYY-MM-DD]` prints and saves a material pack.
`python3 cli.py validate [YYYY-MM-DD]` returns non-zero when the canonical diary is missing or has errors; use it for manual checks or pipelines, not as a cron `script` outside the profile's `scripts/`.

Every CLI command accepts `--profile <name>` (or `-p`). Without it, the CLI resolves the home
from Hermes' context-local override, then `HERMES_HOME`, then `HERMES_PROFILE`, and only then
`~/.hermes/active_profile` — a global pointer to whichever profile was last used. When it has to
fall back to `active_profile` or the bare `~/.hermes` root, it prints a warning naming the home it
picked, so a stray shell invocation cannot silently read or write another agent's diary:

```sh
$ env -u HERMES_HOME python3 cli.py material 2026-11-01
警告: プロファイルを特定できませんでした（解決経路: active_profile）。/Users/…/profiles/assistant を使います。…
```

`dreams_index` / `cli.py index` never fails on a single bad file: unreadable diaries are skipped,
reported in `skipped`, and listed in `INDEX.md` under 読めなかった日記 so the gap stays visible.

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
- `invalid_frontmatter_field` (error) — `author` must be non-empty, `sessions` and `messages` must be non-negative integers, and `commit` must contain a value or a list.
- `missing_section` (error) — one of the six headings is absent.
- `second_person` (error) — `あなた` appears. Refer to the human as `ユーザー`, quote or report
  what they said (`「〜と言っていた」`), never assert their inner state.
- `too_short` / `too_long` (warn) — under 300 or over 3000 characters (whitespace excluded).
- `unresolved_hash` (error) — a hash written in commit context does not exist in any repo
  under `~/Dev/git`. The scan is recursive (depth 3), so nested layouts such as
  `~/Dev/git/<group>/<repo>/.git` are included; `DREAMS_REPOS` (colon-separated) pins the set
  explicitly when a repo lives outside that tree. This catches the most common LLM failure in
  this format: a hash whose
  date and time are right but whose digits were invented.
- `unverified_external_hash` (warn) — an upstream commit (e.g. `origin/main`) that cannot be
  verified locally.

Hex that merely *looks* like a hash — Docker hostnames, cron job ids, run ids, session ids,
game error codes — is ignored unless the surrounding line is about a commit.

## Tests

```sh
python3 -m unittest discover -s tests
```

22 tests, standard library only. They build a throwaway profile (state.db, memory.db) and a
throwaway git repo, so nothing touches real data.

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
`scripts/dreams_diary_material.py` を各プロフィールの `scripts/` にコピーして、相対名で登録してください。
このブリッジは共有プラグインの `cli.py material` を呼び出し、実装を重複させません。

共通 cron プロンプトは `templates/cron-prompt.md`、任意の声の記入用ひな形は
`templates/persona.md` です。枠だけを共有し、内容はプロフィールごとに書き分けます。
cron では pre-run 素材にある `日記ファイル:` の絶対パスを使い、`dreams_write` や date のみの
`dreams_material` は呼びません。Cron worker の tool home が root home に解決される場合があるため、
`dreams_validate` には必ず生成したファイルの `path` を渡します。

手元で CLI を使うときは `--profile <name>`（または `-p`）を付けてください。付けない場合の
解決順は コンテキストローカル → `HERMES_HOME` → `HERMES_PROFILE` → `~/.hermes/active_profile`
（最後に触ったプロファイルを指す共通ポインタ）です。`active_profile` や `~/.hermes` 直下に
落ちたときは、選んだ home を名指しして警告を出します。`cli.py index` は読めない日記が1本あっても
全体を落とさず、`skipped` と INDEX の「読めなかった日記」に残します。
`cli.py validate [YYYY-MM-DD]` は手動検査や pipeline で使えます。
検証には `date` / `author` / `sessions` / `messages` / `commit` を含む frontmatter が必要で、
`date` はファイル名の日付と一致しなければなりません。これで素材パックを日記と誤認する
ケースを検出します。

検査でいちばん効くのは **コミットハッシュの実在確認**。日付と時刻は正しいのに桁だけ
作られたハッシュが混入する事故が実際に起きたため、`~/Dev/git` 配下の全リポジトリを横断して
`git cat-file -e` で確かめる。ホスト名・cron のジョブID・run ID など「16進に見えるが
コミットではない」文字列は、文脈で判定して除外する。
