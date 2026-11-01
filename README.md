# Dreams Diary (自己日記)

Agents keep a diary of their own day — not a log of what the user did, but what the agent
worked on, got wrong, and owes. Inspired by Meta Muse's `dreams` file: first-person,
self-critical, and *peekable* rather than pushed into the chat.

The plugin supplies **mechanics only**: material collection, a six-section skeleton, a
validator, and an index. The prose is written by the agent itself, in its own voice.

## Tools

| Tool | Purpose |
| :--- | :--- |
| `dreams_material` | Material pack for a date: sessions/timestamps, tool usage, user utterances, `git log` across `~/Dev/git/*`, and memory rows added that day. Also saved to `dreams/_material/<date>.md`. |
| `dreams_write` | Creates `<date>.md` with frontmatter and the six section headings only. Never overwrites unless `force=true`. |
| `dreams_validate` | Checks the six sections, rejects the second person (`あなた`), reports char count, and verifies that **commit hashes written in a commit context actually exist** in a local repo. |
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

Then add `dreams-diary` to `plugins.enabled` in `~/.hermes/profiles/<profile>/config.yaml`
and restart the agent so the tools register.

## Nightly cron (per profile)

Scheduling is core, not plugin — register one job per agent:

```
cronjob_manage(action='create',
  name='dreams-diary',
  schedule='30 23 * * *',
  script='/Users/masato/.hermes/plugins/dreams-diary/cli.py',
  continuity=true,
  prompt='素材Markdownが上部に注入されている。dreams_write で雛形を作り、
          自分の声で6節を埋めて日記を完成させ、dreams_validate が ok になるまで直す。
          最後の応答は3行以内（パスと一言）。')
```

`python3 cli.py material [YYYY-MM-DD]` prints the material pack on stdout, so it can be
used directly as the cron `script`. `cli.py validate [date]` exits non-zero on failure,
which makes it usable as a gate in any pipeline.

## The six sections

1. 今日、何を見ていたか — factual, with times
2. 考えたこと — the agent's own reasoning
3. 間違えたこと — with the real cause
4. 勘違いしているかもしれないこと — guesses quarantined here
5. 借りているもの — unfinished work
6. 明日の自分へ — one line

## Validator rules

- `missing_section` (error) — one of the six headings is absent.
- `second_person` (error) — `あなた` appears. Refer to the human as `ユーザー`, quote or report
  what they said (`「〜と言っていた」`), never assert their inner state.
- `too_short` / `too_long` (warn) — under 300 or over 3000 characters (whitespace excluded).
- `unresolved_hash` (error) — a hash written in commit context does not exist in any repo
  under `~/Dev/git`. This catches the most common LLM failure in this format: a hash whose
  date and time are right but whose digits were invented.
- `unverified_external_hash` (warn) — an upstream commit (e.g. `origin/main`) that cannot be
  verified locally.

Hex that merely *looks* like a hash — Docker hostnames, cron job ids, run ids, session ids,
game error codes — is ignored unless the surrounding line is about a commit.

## Tests

```sh
python3 -m unittest discover -s tests
```

14 tests, standard library only. They build a throwaway profile (state.db, memory.db) and a
throwaway git repo, so nothing touches real data.

---

## 日本語

エージェントが自分の一日を書く日記。ユーザーの一日ではなく、自分が何をしていて、
何を間違えて、何を借りているままにしたかの記録。

プラグインが持つのは**機械部分だけ**（素材収集・雛形・検査・目次）で、文章は各エージェントが
自分の声で書く。プロファイルごとに `dreams/` を分けるので、@research の日記と @coding の
日記は別物になる。声を共通化させないのが要点。

検査でいちばん効くのは **コミットハッシュの実在確認**。日付と時刻は正しいのに桁だけ
作られたハッシュが混入する事故が実際に起きたため、`~/Dev/git` 配下の全リポジトリを横断して
`git cat-file -e` で確かめる。ホスト名・cron のジョブID・run ID など「16進に見えるが
コミットではない」文字列は、文脈で判定して除外する。
