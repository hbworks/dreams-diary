"""v1.5 の受け入れ基準（docs/SPEC-v1.5.md）を固定するテスト。

- A: ツールの失敗を素材に追加（上限15件）
- B: 前日の第5・6節を未検証の自己申告として追加
- C: 素材の秘密情報マスク（保存と返却の出口は1か所）
- D: コミットを「自分が編集したファイル」との重なりで絞る
- E: validator の possible_secret（warn）
"""

from __future__ import annotations

import datetime
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import main as core  # noqa: E402

DAY = "2026-10-05"


def _ts(hh: int, mm: int = 0) -> float:
    return datetime.datetime.strptime(f"{DAY} {hh}:{mm:02d}", "%Y-%m-%d %H:%M").timestamp()


def _assistant_call(call_id: str, name: str, args: dict, when: float):
    return (
        "s1",
        "assistant",
        "",
        None,
        json.dumps(
            [
                {
                    "id": call_id,
                    "call_id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }
            ]
        ),
        None,
        when,
    )


def _tool_result(call_id: str, name: str, payload: dict, when: float):
    return ("s1", "tool", json.dumps(payload), call_id, None, name, when)


def make_profile(root: Path, extra_rows=()) -> Path:
    home = root / "profiles" / "tester"
    home.mkdir(parents=True)
    con = sqlite3.connect(home / "state.db")
    con.execute(
        """create table messages (
               session_id text, role text, content text, tool_call_id text,
               tool_calls text, tool_name text, timestamp real)"""
    )
    rows = [
        ("s1", "user", "最初の依頼", None, None, None, _ts(9)),
        ("s1", "assistant", "了解", None, None, None, _ts(9, 1)),
    ]
    rows.extend(extra_rows)
    con.executemany("insert into messages values (?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()

    mem = sqlite3.connect(home / "memory.db")
    mem.execute(
        "create table memories (id integer primary key, category text, content text, created_at text)"
    )
    mem.commit()
    mem.close()
    return home


def make_profile_without_state(root: Path) -> Path:
    home = root / "profiles" / "tester"
    home.mkdir(parents=True)
    return home


def _git_env() -> dict:
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "GIT_AUTHOR_DATE": "2026-10-05T12:00:00+09:00",
        "GIT_COMMITTER_DATE": "2026-10-05T12:00:00+09:00",
    }


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, env=_git_env()
    ).stdout.strip()


def make_repo_with_two_commits(root: Path) -> tuple[Path, str, str]:
    """mine.txt を触るコミットと other.txt を触るコミットを1つずつ作る。"""
    repo = root / "repos" / "demorepo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    (repo / "mine.txt").write_text("mine\n", encoding="utf-8")
    _git(repo, "add", "mine.txt")
    _git(repo, "commit", "-qm", "my edit")
    mine = _git(repo, "rev-parse", "--short=7", "HEAD")
    (repo / "other.txt").write_text("other\n", encoding="utf-8")
    _git(repo, "add", "other.txt")
    _git(repo, "commit", "-qm", "someone else")
    other = _git(repo, "rev-parse", "--short=7", "HEAD")
    return repo, mine, other


def _good_diary(day: str = DAY, commit: str = "none") -> str:
    return (
        f"---\ndate: {day}\nauthor: '@tester'\nsessions: 1\nmessages: 1\ncommit: {commit}\n---\n\n"
        f"# {day}\n\n"
        + "\n\n".join(f"{s}\n本文。検証用の文章をここに置く。" for s in core.SECTIONS)
        + "\n"
    )


def _section(material: str, heading: str) -> str:
    """素材から `## heading` の節だけ切り出す。"""
    match = re.search(rf"^{re.escape(heading)}\n(.*?)(?=^## |\Z)", material, re.S | re.M)
    return match.group(1) if match else ""


class FailureMaterialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _rows(self, count: int):
        rows = []
        for i in range(count):
            call_id = f"call_term_{i}"
            rows.append(
                _assistant_call(call_id, "terminal", {"command": f"pytest -k case{i}"}, _ts(10, i))
            )
            rows.append(
                _tool_result(
                    call_id,
                    "terminal",
                    {"output": f"Traceback line {i}\nmore", "exit_code": 1, "error": None},
                    _ts(10, i),
                )
            )
        return rows

    def test_failures_section_lists_only_failures(self):
        rows = [
            _assistant_call("call_t_ok", "terminal", {"command": "ls -la"}, _ts(10)),
            _tool_result("call_t_ok", "terminal", {"output": "ok", "exit_code": 0}, _ts(10)),
            _assistant_call("call_t_bad", "terminal", {"command": "pytest tests"}, _ts(10, 5)),
            _tool_result(
                "call_t_bad",
                "terminal",
                {"output": "setup\nboom", "exit_code": 1, "error": None},
                _ts(10, 5),
            ),
            _assistant_call("call_p_bad", "patch", {"path": "/repo/a.py"}, _ts(10, 6)),
            _tool_result(
                "call_p_bad",
                "patch",
                {"success": False, "error": "Patch validation failed\nhunk not found"},
                _ts(10, 6),
            ),
            _assistant_call("call_r_bad", "read_file", {"path": "/repo/missing.md"}, _ts(10, 7)),
            _tool_result(
                "call_r_bad",
                "read_file",
                {"not_found": True, "content": "", "is_binary": False, "is_image": False},
                _ts(10, 7),
            ),
            _assistant_call("call_p_ok", "patch", {"path": "/repo/b.py"}, _ts(10, 8)),
            _tool_result(
                "call_p_ok",
                "patch",
                {"success": True, "files_modified": ["/repo/b.py"], "resolved_path": "/repo/b.py"},
                _ts(10, 8),
            ),
        ]
        home = make_profile(self.root, rows)
        material = core.collect_material(DAY, home=home, repos=[])["material"]

        self.assertGreater(material.index("## ツールの失敗（原因は未確認）"), material.index("## 会話"))
        section = _section(material, "## ツールの失敗（原因は未確認）")
        self.assertIn("pytest tests", section)
        self.assertIn("boom", section)
        self.assertIn("Patch validation failed", section)
        self.assertIn("/repo/missing.md", section)
        self.assertNotIn("ls -la", section)
        self.assertNotIn("/repo/b.py", section)
        self.assertIn("terminal 1", section)

    def test_failures_are_capped_at_fifteen(self):
        home = make_profile(self.root, self._rows(16))
        material = core.collect_material(DAY, home=home, repos=[])["material"]
        section = _section(material, "## ツールの失敗（原因は未確認）")
        self.assertEqual(section.count(" — "), 15)
        self.assertIn("（ほか 1 件）", section)

    def test_no_failures_placeholder(self):
        home = make_profile(self.root)
        material = core.collect_material(DAY, home=home, repos=[])["material"]
        section = _section(material, "## ツールの失敗（原因は未確認）")
        self.assertIn("（失敗は記録されていない）", section)

    def _terminal_failure_line(self, output: str) -> str:
        rows = [
            _assistant_call("call_t", "terminal", {"command": "python3 x.py"}, _ts(10)),
            _tool_result("call_t", "terminal", {"output": output, "exit_code": 1, "error": None}, _ts(10)),
        ]
        home = make_profile(self.root, rows)
        material = core.collect_material(DAY, home=home, repos=[])["material"]
        section = _section(material, "## ツールの失敗（原因は未確認）")
        return next(line for line in section.splitlines() if "python3 x.py" in line)

    def test_terminal_failure_uses_the_last_meaningful_line(self):
        traceback = (
            "Traceback (most recent call last):\n"
            '  File "x.py", line 3, in <module>\n'
            "ValueError: bad input"
        )
        line = self._terminal_failure_line(traceback)
        self.assertTrue(line.endswith("→ ValueError: bad input"), line)
        self.assertNotIn("Traceback", line)

    def test_terminal_failure_skips_trailing_punctuation_only_lines(self):
        line = self._terminal_failure_line('{\n  "ok": false,\n  "state": "diff"\n}\n')
        self.assertTrue(line.endswith('→ "state": "diff"'), line)

    def test_terminal_failure_detail_is_clipped(self):
        line = self._terminal_failure_line("first\n" + "x" * 400)
        self.assertLessEqual(len(line.split("→ ", 1)[1]), core.FAIL_DETAIL_CHARS)


class SelfReportMaterialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_sections_five_and_six_are_read_back(self):
        home = make_profile(self.root)
        prev = home / "dreams" / "2026-10-04.md"
        prev.parent.mkdir(parents=True)
        markers = [f"第{i}節の本文マーカー{i}" for i in range(1, 7)]
        prev.write_text(
            "---\ndate: 2026-10-04\nauthor: '@tester'\nsessions: 1\nmessages: 1\ncommit: none\n---\n\n"
            f"# 2026-10-04\n\n"
            + "\n\n".join(f"{s}\n{m}" for s, m in zip(core.SECTIONS, markers))
            + "\n",
            encoding="utf-8",
        )
        material = core.collect_material(DAY, home=home, repos=[])["material"]
        self.assertIn("## 前日の自己申告（未検証）", material)
        section = _section(material, "## 前日の自己申告（未検証）")
        self.assertIn("2026-10-04.md", section)
        self.assertIn(markers[4], section)
        self.assertIn(markers[5], section)
        for marker in markers[:4]:
            self.assertNotIn(marker, material)

    def test_missing_previous_diary_is_marked(self):
        home = make_profile(self.root)
        material = core.collect_material(DAY, home=home, repos=[])["material"]
        section = _section(material, "## 前日の自己申告（未検証）")
        self.assertIn("（無し）", section)


class RedactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_secrets_are_masked_in_saved_and_returned_material(self):
        home = make_profile(self.root)
        secret_text = (
            "通常の文章はそのまま残る。\n"
            "key sk-abcdefghijklmnopqrstuvwxyz012345 を置いた。\n"
            "token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 も置いた。\n"
            "password=hunter2hunter2 と書いた。\n"
            "-----BEGIN RSA PRIVATE KEY-----\nMIIEsecretsecret\n-----END RSA PRIVATE KEY-----\n"
        )
        with patch.object(core, "_sessions_material", return_value=secret_text):
            got = core.collect_material(DAY, home=home, repos=[])

        material = got["material"]
        saved = Path(got["material_path"]).read_text(encoding="utf-8")
        for text in (material, saved):
            self.assertIn("[REDACTED:", text)
            self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz012345", text)
            self.assertNotIn("ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", text)
            self.assertNotIn("hunter2hunter2", text)
            self.assertNotIn("MIIEsecretsecret", text)
            self.assertIn("通常の文章はそのまま残る。", text)
        count = len(re.findall(r"\[REDACTED:[^\]]+\]", material))
        self.assertGreaterEqual(count, 4)
        self.assertIn(f"マスク: {count} 件", material)
        self.assertEqual(count, len(re.findall(r"\[REDACTED:[^\]]+\]", saved)))

    def test_truncated_material_still_shows_the_mask_count(self):
        home = make_profile(self.root)
        secret_text = "password=hunter2hunter2\n" + "会話の行。\n" * 1000
        with patch.object(core, "_sessions_material", return_value=secret_text):
            got = core.collect_material(DAY, home=home, max_chars=800, repos=[])
        self.assertTrue(got["truncated"])
        self.assertNotIn(core.MASK_COUNT_TOKEN, got["material"])
        self.assertIn("マスク: 1 件", got["material"])


class BearerRedactionTests(unittest.TestCase):
    def _kinds(self, text: str) -> dict:
        return core.redact_secrets(text)[1]

    def test_plain_prose_after_bearer_is_not_a_secret(self):
        self.assertEqual(self._kinds("bearer authentication is used"), {})
        self.assertEqual(self._kinds("the bearer tokens expire"), {})

    def test_bare_bearer_with_a_digit_or_long_value_is_masked(self):
        self.assertEqual(self._kinds("bearer abcdefgh12345"), {"bearer": 1})
        self.assertEqual(self._kinds("bearer " + "a" * 24), {"bearer": 1})

    def test_authorization_header_is_masked_even_when_short(self):
        masked, counts = core.redact_secrets("Authorization: Bearer abcdefgh")
        self.assertEqual(counts, {"bearer": 1})
        self.assertNotIn("abcdefgh", masked)
        self.assertEqual(self._kinds("Authorization: abcdefgh"), {"bearer": 1})


class CommitScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo, self.mine, self.other = make_repo_with_two_commits(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_commits_touching_my_edited_files_are_mine(self):
        rows = [
            _assistant_call("call_p", "patch", {"path": str(self.repo / "mine.txt")}, _ts(12)),
            _tool_result(
                "call_p",
                "patch",
                {
                    "success": True,
                    "files_modified": [str(self.repo / "mine.txt")],
                    "resolved_path": str(self.repo / "mine.txt"),
                },
                _ts(12),
            ),
        ]
        home = make_profile(self.root, rows)
        material = core.collect_material(DAY, home=home, repos=[self.repo])["material"]
        commits = _section(material, "## コミット")
        self.assertIn(self.mine, commits)
        self.assertNotIn(self.other, commits)
        self.assertIn("ほかのコミット: 1 件", material)

    def test_read_only_and_failed_edits_do_not_count_as_mine(self):
        rows = [
            _assistant_call("call_r", "read_file", {"path": str(self.repo / "mine.txt")}, _ts(12)),
            _tool_result(
                "call_r",
                "read_file",
                {"not_found": False, "resolved_path": str(self.repo / "mine.txt")},
                _ts(12),
            ),
            _assistant_call("call_p", "patch", {"path": str(self.repo / "mine.txt")}, _ts(12, 5)),
            _tool_result(
                "call_p",
                "patch",
                {"success": False, "error": "not found", "files_modified": [str(self.repo / "mine.txt")]},
                _ts(12, 5),
            ),
        ]
        home = make_profile(self.root, rows)
        material = core.collect_material(DAY, home=home, repos=[self.repo])["material"]
        commits = _section(material, "## コミット")
        self.assertIn("（自分が編集したファイルはない）", commits)
        self.assertNotIn(self.mine, commits)
        self.assertNotIn(self.other, commits)
        self.assertIn("ほかのコミット: 2 件", material)

        # 絞った素材でも、validator は全リポジトリでハッシュの実在を確認する
        diary = home / "dreams" / f"{DAY}.md"
        diary.parent.mkdir(parents=True, exist_ok=True)
        diary.write_text(_good_diary(commit=self.other), encoding="utf-8")
        checked = core.validate_diary(path=str(diary), home=home, repos=[self.repo])
        self.assertTrue(checked["ok"], checked)
        self.assertNotIn("unresolved_hash", checked["errors"])

    def test_missing_state_db_falls_back_to_all_repos_with_a_note(self):
        home = make_profile_without_state(self.root)
        material = core.collect_material(DAY, home=home, repos=[self.repo])["material"]
        commits = _section(material, "## コミット")
        self.assertIn(self.mine, commits)
        self.assertIn(self.other, commits)
        self.assertIn("編集を特定できないため全リポジトリ", material)

    def test_commit_cap_is_twenty_commits(self):
        for i in range(20):
            (self.repo / "mine.txt").write_text(f"mine {i}\n", encoding="utf-8")
            _git(self.repo, "add", "mine.txt")
            _git(self.repo, "commit", "-qm", f"edit {i}")
        rows = [
            _assistant_call("call_w", "write_file", {"path": str(self.repo / "mine.txt")}, _ts(13)),
            _tool_result(
                "call_w",
                "write_file",
                {
                    "files_modified": [str(self.repo / "mine.txt")],
                    "resolved_path": str(self.repo / "mine.txt"),
                },
                _ts(13),
            ),
        ]
        home = make_profile(self.root, rows)
        material = core.collect_material(DAY, home=home, repos=[self.repo])["material"]
        commits = _section(material, "## コミット")
        self.assertEqual(commits.count("  mine.txt"), 20)
        self.assertIn("（ほか 1 件）", commits)


class PossibleSecretValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = make_profile(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def _validate(self, body: str):
        path = self.home / "dreams" / f"{DAY}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return core.validate_diary(path=str(path), home=self.home, repos=[])

    def test_known_secret_shape_warns_without_leaking_the_value(self):
        token = "sk-abcdefghijklmnopqrstuvwxyz012345"
        result = self._validate(_good_diary().replace("本文。", f"鍵 {token} を見た。", 1))
        self.assertTrue(result["ok"], result)
        warn = [i for i in result["issues"] if i["code"] == "possible_secret"]
        self.assertTrue(warn, result)
        self.assertNotIn(token, json.dumps(warn, ensure_ascii=False))
        self.assertIn("1", warn[0]["detail"])

    def test_normal_diary_has_no_secret_warning(self):
        result = self._validate(_good_diary())
        self.assertTrue(result["ok"], result)
        self.assertNotIn("possible_secret", [i["code"] for i in result["issues"]])

    def test_generic_hyphenated_words_are_not_secrets(self):
        # "task-management-system" の "sk-..." を鍵と誤検出しない
        result = self._validate(_good_diary().replace("本文。", "task-management-system の方針。", 1))
        self.assertTrue(result["ok"], result)
        self.assertNotIn("possible_secret", [i["code"] for i in result["issues"]])


class TimePresenceValidationTests(unittest.TestCase):
    """sessions がある日は本文に時刻（HH:MM）を1つ以上書く。

    素材のセッション見出しは `HH:MM–HH:MM` を持つので、時刻を落とした日記は
    どの作業の話か追えなくなる。error ではなく warn に留める（古い日記を壊さない）。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = make_profile(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def _validate(self, body: str):
        path = self.home / "dreams" / f"{DAY}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return core.validate_diary(path=str(path), home=self.home, repos=[])

    def _codes(self, result):
        return [i["code"] for i in result["issues"]]

    def test_session_diary_without_time_warns(self):
        result = self._validate(_good_diary())
        self.assertTrue(result["ok"], result)
        self.assertIn("time_absent", self._codes(result))

    def test_session_diary_with_time_does_not_warn(self):
        body = _good_diary().replace("本文。検証用の文章をここに置く。", "12:27 から 13:39 まで調査した。", 1)
        result = self._validate(body)
        self.assertTrue(result["ok"], result)
        self.assertNotIn("time_absent", self._codes(result))

    def test_full_width_colon_counts_as_a_time(self):
        body = _good_diary().replace("本文。検証用の文章をここに置く。", "12：27 から調査した。", 1)
        result = self._validate(body)
        self.assertNotIn("time_absent", self._codes(result))

    def test_diary_with_no_sessions_is_exempt(self):
        result = self._validate(_good_diary().replace("sessions: 1", "sessions: 0", 1))
        self.assertNotIn("time_absent", self._codes(result))

    def test_time_only_in_frontmatter_does_not_count(self):
        body = _good_diary().replace("author: '@tester'", "author: '@tester 12:27'", 1)
        result = self._validate(body)
        self.assertIn("time_absent", self._codes(result))

    def test_detail_names_the_session_count(self):
        result = self._validate(_good_diary())
        warn = [i for i in result["issues"] if i["code"] == "time_absent"]
        self.assertTrue(warn, result)
        self.assertIn("1", warn[0]["detail"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
