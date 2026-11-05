"""dreams-diary プラグインのテスト（標準ライブラリのみ）。"""

from __future__ import annotations

import datetime
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main as core  # noqa: E402

DAY = "2026-10-05"

# v2.0 の本文（見出しなしの散文）。検査は節を見ないので、本文は段落で作る。
PROSE = (
    "23:11、画面にはまだ差分が残っていた。ユーザーは「そこは残して」と言っていて、"
    "その一言で優先順位が決まった。\n\n"
    "最初は表示側だけ直せば足りると考えたが、再構築の経路を読むと固定費の計算も"
    "同じだけずれていた。そのせいでユーザーには確認をもう一度させた。\n\n"
    "明日は、切り詰めのテストを先に流してから本文の上限を触る。"
)

# 旧形式（6節）の本文。v2.0 でも有効なままであることを確かめるために置く。
LEGACY_PROSE = "\n\n".join(
    f"{heading}\n本文。"
    for heading in (
        "## 今日、何を見ていたか",
        "## 考えたこと",
        "## 間違えたこと",
        "## 勘違いしているかもしれないこと",
        "## 借りているもの",
        "## 明日の自分へ",
    )
)


def _ts(day: str, hh: int, mm: int = 0) -> float:
    return datetime.datetime.strptime(f"{day} {hh}:{mm:02d}", "%Y-%m-%d %H:%M").timestamp()


def make_profile(root: Path) -> Path:
    home = root / "profiles" / "tester"
    home.mkdir(parents=True)
    con = sqlite3.connect(home / "state.db")
    con.execute(
        """create table messages (
               session_id text, role text, content text, tool_name text, timestamp real)"""
    )
    rows = [
        ("s1", "user", "最初の依頼", None, _ts(DAY, 10)),
        ("s1", "assistant", "了解", None, _ts(DAY, 10, 1)),
        ("s1", "user", "最初の依頼", None, _ts(DAY, 10, 1)),  # 重複
        ("s1", "user", "[IMPORTANT: Background process exited]", None, _ts(DAY, 10, 2)),
        ("s1", "assistant", "", "terminal", _ts(DAY, 10, 3)),
        ("s2", "user", "別セッションの発言", None, _ts(DAY, 23)),
        ("s2", "user", "翌日の発言", None, _ts("2026-10-06", 1)),
    ]
    con.executemany("insert into messages values (?,?,?,?,?)", rows)
    con.commit()
    con.close()

    mem = sqlite3.connect(home / "memory.db")
    mem.execute("create table memories (id integer primary key, category text, content text, created_at text)")
    mem.execute(
        "insert into memories (category, content, created_at) values ('rule','テスト用の記憶','2026-10-05 00:00:00')"
    )
    mem.commit()
    mem.close()
    return home


def make_repo(root: Path) -> tuple[Path, str]:
    repo = root / "repos" / "demorepo"
    repo.mkdir(parents=True)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "GIT_AUTHOR_DATE": "2026-10-05T12:00:00+09:00",
        "GIT_COMMITTER_DATE": "2026-10-05T12:00:00+09:00",
    }
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env)
    (repo / "a.txt").write_text("x")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "test commit"], cwd=repo, check=True, env=env)
    sha = subprocess.run(
        ["git", "rev-parse", "--short=7", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    return repo, sha


class MaterialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = make_profile(self.root)
        self.repo, self.sha = make_repo(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_collect_counts_and_filters(self):
        got = core.collect_material(DAY, home=self.home, repos=[self.repo])
        material = got["material"]
        self.assertIn("メッセージ 6 件", material)  # 翌日ぶんは含まない
        self.assertIn("セッション 2 件", material)
        self.assertEqual(material.count("最初の依頼"), 1)  # 連続重複は落とす
        self.assertNotIn("Background process exited", material)  # 注入ノイズは落とす
        self.assertIn("テスト用の記憶", material)
        self.assertIn(self.sha, material)
        self.assertTrue(Path(got["material_path"]).exists())

    def test_memory_zero_is_not_reported_as_failure(self):
        got = core.collect_material("2026-10-04", home=self.home, repos=[self.repo])
        self.assertIn("0 件", got["material"])
        self.assertNotIn("取得失敗", got["material"])

    def test_sessions_retry_wal_open_failure_with_normal_connection(self):
        original_connect = sqlite3.connect
        calls = []

        def connect(*args, **kwargs):
            calls.append((args, kwargs))
            if len(calls) == 1:
                raise sqlite3.OperationalError("unable to open database file")
            return original_connect(*args, **kwargs)

        with patch.object(core.sqlite3, "connect", side_effect=connect):
            material = core._sessions_material(DAY, self.home)
        self.assertIn("メッセージ 6 件", material)
        self.assertEqual(len(calls), 2)
        self.assertIn("mode=ro", calls[0][0][0])
        self.assertEqual(calls[1][0][0], str(self.home / "state.db"))

    def test_material_truncates(self):
        got = core.collect_material(DAY, home=self.home, max_chars=300, repos=[self.repo])
        self.assertTrue(got["truncated"])
        self.assertLessEqual(len(got["material"]), 300)
        self.assertEqual(got["chars"], len(got["material"]))
        self.assertIn(str(Path(got["material_path"])), got["material"])

    def test_material_rejects_budget_too_small_for_truncation_notice(self):
        with self.assertRaisesRegex(ValueError, "max_chars|字以上"):
            core.collect_material(DAY, home=self.home, max_chars=10, repos=[self.repo])
        self.assertFalse((self.home / "dreams" / "_material" / f"{DAY}.md").exists())

    def test_failed_material_save_does_not_claim_a_full_text_path(self):
        with patch.object(Path, "write_text", side_effect=PermissionError("read-only")), \
             patch.object(core, "_sessions_material", return_value="会話。\n" * 2000):
            got = core.collect_material(DAY, home=self.home, max_chars=500, repos=[self.repo])
        self.assertFalse(got["material_saved"])
        self.assertIn("全文の保存に失敗した", got["material"])
        self.assertNotIn(str(Path(got["material_path"])), got["material"])

    def test_material_tool_omits_path_when_save_failed(self):
        import __init__ as plugin  # noqa: PLC0415

        material_path = self.home / "dreams" / "_material" / f"{DAY}.md"
        with patch.object(Path, "write_text", side_effect=PermissionError("read-only")):
            result = plugin._material_handler(
                {"date": DAY, "max_chars": 14000}, home=self.home
            )
        self.assertIn("保存に失敗", result)
        self.assertNotIn(str(material_path), result)

    def test_material_tool_counts_the_saved_file_locator_in_its_limit(self):
        import __init__ as plugin  # noqa: PLC0415

        result = plugin._material_handler(
            {"date": DAY, "max_chars": 300}, home=self.home
        )
        self.assertLessEqual(len(result), 300)
        self.assertIn(str(self.home / "dreams" / "_material" / f"{DAY}.md"), result)

    def test_material_file_keeps_full_text_when_truncated(self):
        # 返り値は切り詰めてよいが、_material 側は全文でなければ案内文が嘘になる
        got = core.collect_material(DAY, home=self.home, max_chars=400, repos=[self.repo])
        self.assertTrue(got["truncated"])
        self.assertLess(len(got["material"]), got["full_chars"])
        full = Path(got["material_path"]).read_text(encoding="utf-8")
        self.assertEqual(len(full), got["full_chars"])
        self.assertIn(self.sha, full)

    def test_truncation_keeps_commit_and_memory_sections(self):
        # 会話が上限を食っても、根拠になる「コミット」「memory」節は落とさない
        with patch.object(core, "_sessions_material", return_value="会話の行。\n" * 2000):
            got = core.collect_material(DAY, home=self.home, max_chars=1000, repos=[self.repo])
        material = got["material"]
        self.assertTrue(got["truncated"])
        self.assertLessEqual(len(material), 1000)
        self.assertIn("## コミット", material)
        self.assertIn(self.sha, material)
        self.assertIn("テスト用の記憶", material)

    def test_truncation_keeps_successful_operations_section(self):
        successes = "## 成功した操作\n- 12:34 terminal — python3 scripts/check.py\n"
        with (
            patch.object(core, "_sessions_material", return_value="会話の行。\n" * 2000),
            patch.object(core, "_successful_operations_material", return_value=successes),
        ):
            got = core.collect_material(DAY, home=self.home, max_chars=1000, repos=[self.repo])

        self.assertTrue(got["truncated"])
        material = got["material"]
        self.assertIn("## 成功した操作", material)
        self.assertIn("python3 scripts/check.py", material)


    def test_material_reports_previous_diary_ok(self):
        # post-run フックが無い代わりに、翌日の素材で前回分の検証結果を知らせる
        prev = self.home / "dreams" / "2026-10-04.md"
        prev.parent.mkdir(parents=True, exist_ok=True)
        prev.write_text(
            "---\ndate: 2026-10-04\nauthor: '@tester'\nsessions: 1\nmessages: 1\n"
            "commit: none\n---\n\n# 2026-10-04\n\n"
            + PROSE
            + "\n",
            encoding="utf-8",
        )
        material = core.collect_material(DAY, home=self.home, repos=[self.repo])["material"]
        self.assertIn("前回の日記の検証", material)
        self.assertIn("- 2026-10-04.md: ok", material)

    def test_material_reports_previous_diary_failure(self):
        prev = self.home / "dreams" / "2026-10-04.md"
        prev.parent.mkdir(parents=True, exist_ok=True)
        prev.write_text("---\ndate: 2026-10-04\n---\n\n# 2026-10-04\n\n本文だけ。\n", encoding="utf-8")
        material = core.collect_material(DAY, home=self.home, repos=[self.repo])["material"]
        self.assertIn("2026-10-04.md", material)
        self.assertIn("未通過", material)
        self.assertIn("missing_frontmatter_field", material)

    def test_audit_reports_failed_files(self):
        dreams = self.home / "dreams"
        dreams.mkdir(parents=True, exist_ok=True)
        (dreams / "2026-10-04.md").write_text(
            "---\ndate: 2026-10-04\nauthor: '@tester'\nsessions: 1\nmessages: 1\n"
            "commit: none\n---\n\n# 2026-10-04\n\n"
            + PROSE
            + "\n",
            encoding="utf-8",
        )
        (dreams / "2026-10-05.md").write_text("---\ndate: 2026-10-05\n---\n本文だけ。\n", encoding="utf-8")
        res = core.audit_diaries(home=self.home, days=5, repos=[self.repo])
        self.assertFalse(res["ok"])
        self.assertEqual(res["checked"], 2)
        self.assertEqual(res["failed"], ["2026-10-05.md"])

    def test_audit_counts_warns_and_errors_by_code(self):
        # warn が常に出る状態で「新規か履歴か」を1コマンドで切り分けられるよう、
        # audit は level ごとのコード別件数を持つ。ok の有無だけでは見えない。
        dreams = self.home / "dreams"
        dreams.mkdir(parents=True, exist_ok=True)
        (dreams / "2026-10-04.md").write_text(
            "---\ndate: 2026-10-04\nauthor: '@tester'\nsessions: 1\nmessages: 1\n"
            "commit: none\n---\n\n# 2026-10-04\n\n"
            + PROSE
            + "\n",
            encoding="utf-8",
        )
        (dreams / "2026-10-05.md").write_text("---\ndate: 2026-10-05\n---\n本文だけ。\n", encoding="utf-8")
        res = core.audit_diaries(home=self.home, days=5, repos=[self.repo])
        self.assertEqual(res["warn_counts"], {"too_short": 2})
        self.assertEqual(res["error_counts"], {"missing_frontmatter_field": 1})

    def test_audit_orders_variants_by_numeric_suffix(self):
        dreams = self.home / "dreams"
        dreams.mkdir(parents=True, exist_ok=True)
        for variant in (2, 10):
            (dreams / f"{DAY}-{variant}.md").write_text("invalid diary", encoding="utf-8")
        result = core.audit_diaries(home=self.home, days=2, repos=[])
        self.assertEqual(
            [row["file"] for row in result["results"]],
            [f"{DAY}-10.md", f"{DAY}-2.md"],
        )

    def test_author_falls_back_from_root_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            root_home = Path(tmp) / ".hermes"
            self.assertEqual(core._author_name(root_home), "agent")

    def test_collect_material_refuses_to_write_under_root_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "custom-hermes"
            (root / "profiles").mkdir(parents=True)
            with self.assertRaisesRegex(ValueError, "プロファイル|profile"):
                core.collect_material(DAY, home=root, repos=[])
            self.assertFalse((root / "dreams").exists())


class SkeletonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = make_profile(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_skeleton_has_no_section_headings(self):
        res = core.write_skeleton(DAY, home=self.home)
        self.assertTrue(res["ok"])
        text = Path(res["path"]).read_text(encoding="utf-8")
        self.assertIn(f"date: {DAY}", text)
        self.assertNotIn("## ", text)
        self.assertEqual(len(res["flow"]), len(core.FLOW))

    def test_refuses_overwrite(self):
        core.write_skeleton(DAY, home=self.home)
        res = core.write_skeleton(DAY, home=self.home)
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "exists")
        forced = core.write_skeleton(DAY, home=self.home, force=True)
        self.assertTrue(forced["ok"])

    def test_skeleton_with_counts_passes_own_validator(self):
        # 雛形と検査の食い違いを塞ぐ: 実測値を渡せば、作った時点で valid
        res = core.write_skeleton(DAY, home=self.home, sessions=2, messages=7)
        self.assertTrue(res["counts_filled"])
        text = Path(res["path"]).read_text(encoding="utf-8")
        self.assertIn("sessions: 2", text)
        self.assertIn("messages: 7", text)
        checked = core.validate_diary(path=res["path"], home=self.home, repos=[])
        self.assertTrue(checked["ok"], checked)
        self.assertNotIn("invalid_frontmatter_field", checked["errors"])

    def test_skeleton_without_counts_still_leaves_blanks(self):
        # 推測で 0 を入れない: 省略時は空欄のまま
        res = core.write_skeleton(DAY, home=self.home)
        self.assertFalse(res["counts_filled"])
        self.assertIn("sessions:\n", Path(res["path"]).read_text(encoding="utf-8"))

    def test_skeleton_rejects_negative_counts(self):
        with self.assertRaises(ValueError):
            core.write_skeleton(DAY, home=self.home, sessions=-1)
        with self.assertRaises(ValueError):
            core.write_skeleton(DAY, home=self.home, messages="many")

    def test_force_keeps_backup_of_written_body(self):
        # force は従来どおり上書きするが、本文があるときだけ元の内容を .bak に残す。
        res = core.write_skeleton(DAY, home=self.home, sessions=2, messages=7)
        path = Path(res["path"])
        path.write_text(path.read_text(encoding="utf-8") + "\n書いた本文。\n", encoding="utf-8")
        forced = core.write_skeleton(DAY, home=self.home, force=True)
        self.assertTrue(forced["ok"])
        self.assertIsNotNone(forced["backup_path"])
        backup = Path(forced["backup_path"])
        self.assertEqual(backup.name, f"{DAY}.md.bak")
        self.assertIn("書いた本文。", backup.read_text(encoding="utf-8"))
        # 目次には載らない
        self.assertEqual(core.build_index(home=self.home)["count"], 1)

    def test_force_on_empty_skeleton_makes_no_backup(self):
        core.write_skeleton(DAY, home=self.home)
        forced = core.write_skeleton(DAY, home=self.home, force=True)
        self.assertTrue(forced["ok"])
        self.assertIsNone(forced["backup_path"])
        self.assertEqual(list((self.home / "dreams").glob("*.bak")), [])

    def test_skeleton_rejects_non_padded_dates(self):
        # strptime は "2026-11-3" を通すが、そのファイル名は DIARY_NAME に一致せず
        # INDEX から静かに漏れる。書き込みの入口で書式を固定する。
        for bad in ("2026-11-3", "2026-1-1", "26-01-01"):
            with self.subTest(day=bad):
                with self.assertRaises(ValueError):
                    core.write_skeleton(bad, home=self.home)
        self.assertEqual(list((self.home / "dreams").glob("*.md")), [])

    def test_skeleton_refuses_to_write_under_root_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "custom-hermes"
            (root / "profiles").mkdir(parents=True)
            with self.assertRaisesRegex(ValueError, "プロファイル|profile"):
                core.write_skeleton(DAY, home=root)
            self.assertFalse((root / "dreams").exists())

    def test_string_false_does_not_force_overwrite(self):
        import __init__ as plugin  # noqa: PLC0415

        result = core.write_skeleton(DAY, home=self.home)
        path = Path(result["path"])
        original = path.read_text(encoding="utf-8")
        with patch.object(plugin.logger, "warning"):
            response = plugin._safe(plugin._write_handler, home=self.home)(
                {"date": DAY, "force": "false"}
            )
        self.assertFalse(json.loads(response)["ok"])
        self.assertEqual(path.read_text(encoding="utf-8"), original)


class ValidateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home = make_profile(root)
        self.repo, self.sha = make_repo(root)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, body: str) -> Path:
        path = self.home / "dreams" / f"{DAY}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return path

    def _prose_body(self) -> str:
        return f"# {DAY}\n\n{PROSE}（{self.sha}）\n"

    def _good_body(self) -> str:
        return (
            f"---\ndate: {DAY}\nauthor: '@tester'\nsessions: 1\nmessages: 1\ncommit: none\n---\n\n"
            + self._prose_body()
        )

    def test_valid_entry_passes(self):
        self._write(self._good_body())
        res = core.validate_diary(DAY, home=self.home, repos=[self.repo])
        self.assertTrue(res["ok"], res)

    def test_missing_frontmatter_fails(self):
        self._write(self._prose_body())
        res = core.validate_diary(DAY, home=self.home, repos=[self.repo])
        self.assertFalse(res["ok"], res)
        self.assertIn("missing_frontmatter", res["errors"])

    def test_missing_frontmatter_field_fails(self):
        body = self._good_body().replace("messages: 1\n", "")
        self._write(body)
        res = core.validate_diary(DAY, home=self.home, repos=[self.repo])
        self.assertFalse(res["ok"], res)
        self.assertIn("missing_frontmatter_field", res["errors"])

    def test_frontmatter_date_must_match_filename(self):
        body = self._good_body().replace(f"date: {DAY}", "date: 2026-10-04")
        self._write(body)
        res = core.validate_diary(DAY, home=self.home, repos=[self.repo])
        self.assertFalse(res["ok"], res)
        self.assertIn("frontmatter_date_mismatch", res["errors"])

    def test_frontmatter_counts_must_be_integers(self):
        body = self._good_body().replace("messages: 1\n", "messages: many\n")
        self._write(body)
        res = core.validate_diary(DAY, home=self.home, repos=[self.repo])
        self.assertFalse(res["ok"], res)
        self.assertIn("invalid_frontmatter_field", res["errors"])

    def test_prose_without_headings_passes(self):
        # v2.0: 本文は見出しを持たない散文。節の欠落はもう error ではない。
        self._write(self._good_body())
        res = core.validate_diary(DAY, home=self.home, repos=[self.repo])
        self.assertNotIn("missing_section", res["errors"])
        self.assertTrue(res["ok"], res)

    def test_legacy_six_section_diary_still_passes(self):
        # 既存の日記を遡及で壊さない（v2.0 は検査を減らすだけで、要求を増やさない）
        self._write(
            f"---\ndate: {DAY}\nauthor: '@tester'\nsessions: 1\nmessages: 1\n"
            f"commit: none\n---\n\n# {DAY}\n\n{LEGACY_PROSE}\n"
        )
        res = core.validate_diary(DAY, home=self.home, repos=[self.repo])
        self.assertTrue(res["ok"], res)

    def test_second_person_fails(self):
        self._write(self._good_body().replace("明日は、", "あなたは今日こう言った。"))
        res = core.validate_diary(DAY, home=self.home, repos=[self.repo])
        self.assertFalse(res["ok"])
        self.assertIn("second_person", res["errors"])

    def test_quoted_utterance_may_contain_second_person(self):
        # README は「引用してよい」と案内している。引用符の中の「あなた」は話者の言葉であって
        # エージェントが二人称で呼んでいるわけではないため、error にしない。
        self._write(
            self._good_body().replace(
                "明日は、",
                'ユーザーは「あなたは直して」と言っていた。"あなたの番です" とも。明日は、',
            )
        )
        res = core.validate_diary(DAY, home=self.home, repos=[self.repo])
        self.assertNotIn("second_person", res["errors"])

    def test_unclosed_quote_does_not_hide_second_person(self):
        # 閉じていない引用を除去すると検査を抜けられる。閉じた引用だけを対象にする。
        self._write(self._good_body().replace("明日は、", "ユーザーは「あなたは直して と言っていた。明日は、"))
        res = core.validate_diary(DAY, home=self.home, repos=[self.repo])
        self.assertIn("second_person", res["errors"])

    def test_short_prose_warns_even_when_file_is_long(self):
        # frontmatter＋見出し（実測 140〜160 字）を字数に含めると、要約1文の日記が通ってしまう
        self._write(self._good_body())
        res = core.validate_diary(DAY, home=self.home, repos=[self.repo])
        self.assertTrue(res["ok"], res)
        self.assertIn("too_short", [i["code"] for i in res["issues"]])
        self.assertLess(res["chars"], core.MIN_CHARS_SOFT)

    def test_prose_chars_exclude_frontmatter_and_headings(self):
        long = "\n\n".join("あ" * 150 for _ in range(6))
        body = (
            f"---\ndate: {DAY}\nauthor: '@tester'\nsessions: 1\nmessages: 1\ncommit: none\n---\n\n"
            f"# {DAY}\n\n{long}\n"
        )
        self._write(body)
        res = core.validate_diary(DAY, home=self.home, repos=[self.repo])
        self.assertTrue(res["ok"], res)
        self.assertNotIn("too_short", [i["code"] for i in res["issues"]])
        self.assertEqual(res["chars"], 900)

    def test_hash_without_commit_context_is_ignored(self):
        # 文脈の無いバッククォート付き16進は、ホスト名やIDの可能性が高いので検査しない
        self._write(self._good_body() + "\nメモ `0c257b3`\n")
        res = core.validate_diary(DAY, home=self.home, repos=[self.repo])
        self.assertTrue(res["ok"], res)

    def test_fabricated_hash_in_commit_context_fails(self):
        self._write(self._good_body() + "\nコミット `0c257b3` を打った。\n")
        res = core.validate_diary(DAY, home=self.home, repos=[self.repo])
        self.assertFalse(res["ok"])
        self.assertIn("unresolved_hash", res["errors"])

    def test_quoted_frontmatter_commit_hash_is_verified(self):
        body = self._good_body().replace("commit: none", 'commit: "deadbeefcafebabe"')
        self._write(body)
        res = core.validate_diary(DAY, home=self.home, repos=[self.repo])
        self.assertFalse(res["ok"], res)
        self.assertIn("unresolved_hash", res["errors"])

    def test_frontmatter_commit_list_hashes_are_verified(self):
        body = self._good_body().replace(
            "commit: none", "commit:\n  - deadbeefcafebabe"
        )
        self._write(body)
        res = core.validate_diary(DAY, home=self.home, repos=[self.repo])
        self.assertFalse(res["ok"], res)
        self.assertIn("unresolved_hash", res["errors"])

    def test_hash_with_no_searchable_repo_is_unverifiable_not_fabricated(self):
        """repo が1つも見つからない環境で「捏造」と断定しない。

        cron worker の HOME ずれなどで `discover_repos()` が空を返すと、この判定が
        error になり、翌日の素材の `前回の日記の検証` に「未通過」として載る。次の
        書き手はそれを自分の誤りとして書き写すので、誤りが日記の履歴に定着する。
        """
        self._write(self._good_body() + "\nコミット `0c257b3` を打った。\n")
        res = core.validate_diary(DAY, home=self.home, repos=[])
        self.assertTrue(res["ok"], res)
        self.assertIn("hash_unverifiable", [i["code"] for i in res["issues"]])
        self.assertNotIn("unresolved_hash", res["errors"])

    def test_frontmatter_commit_list_accepts_a_local_hash(self):
        body = self._good_body().replace(
            "commit: none", f'commit:\n  - "{self.sha}"'
        )
        self._write(body)
        res = core.validate_diary(DAY, home=self.home, repos=[self.repo])
        self.assertTrue(res["ok"], res)

    def test_comma_separated_frontmatter_hashes_are_all_verified(self):
        body = self._good_body().replace(
            "commit: none", f"commit: {self.sha}, deadbeefcafebabe"
        )
        self._write(body)
        res = core.validate_diary(DAY, home=self.home, repos=[self.repo])
        self.assertFalse(res["ok"], res)
        self.assertIn("unresolved_hash", res["errors"])

    def test_non_hash_commit_description_remains_valid(self):
        body = self._good_body().replace("commit: none", "commit: no commits recorded")
        self._write(body)
        res = core.validate_diary(DAY, home=self.home, repos=[])
        self.assertTrue(res["ok"], res)
        self.assertNotIn("invalid_frontmatter_field", res["errors"])

    def test_hash_substring_in_commit_description_is_not_a_candidate(self):
        body = self._good_body().replace("commit: none", "commit: notdeadbeefcafebabe")
        self._write(body)
        res = core.validate_diary(DAY, home=self.home, repos=[])
        self.assertTrue(res["ok"], res)
        self.assertNotIn("unresolved_hash", res["errors"])

    def test_explicit_github_commit_url_remains_a_warning(self):
        self._write(
            self._good_body()
            + "\nSource: https://github.com/acme/project/commit/deadbeefcafebabe\n"
        )
        res = core.validate_diary(DAY, home=self.home, repos=[])
        self.assertTrue(res["ok"], res)
        self.assertIn("unverified_external_hash", [issue["code"] for issue in res["issues"]])

    def test_explicit_external_commit_label_remains_a_warning(self):
        self._write(self._good_body() + "\nupstream commit `deadbeefcafebabe`\n")
        res = core.validate_diary(DAY, home=self.home, repos=[])
        self.assertTrue(res["ok"], res)
        self.assertIn("unverified_external_hash", [issue["code"] for issue in res["issues"]])

    def test_incidental_external_words_do_not_downgrade_local_hashes(self):
        for marker in ("github.com", "upstream", "外部"):
            with self.subTest(marker=marker):
                self._write(
                    self._good_body()
                    + f"\nコミット `deadbeefcafebabe` を記録。参照先の説明: {marker}\n"
                )
                res = core.validate_diary(DAY, home=self.home, repos=[self.repo])
                self.assertFalse(res["ok"], res)
                self.assertIn("unresolved_hash", res["errors"])

    def test_non_commit_hex_tokens_are_ignored(self):
        noise = (
            "\n- hostname `605f4728ab04` が同じだった。\n"
            "- cron `68a852e01acc` を登録した。\n"
            "- Run ID `20261013-web-design-trends-2026-10-001-3974` は pass。\n"
            "- エラーコード `MAGPIE-00001701`。\n"
            "- 検証環境は `local:space-7ff381e523`。\n"
        )
        self._write(self._good_body() + noise)
        res = core.validate_diary(DAY, home=self.home, repos=[self.repo])
        self.assertTrue(res["ok"], res)

    def test_external_hash_is_only_a_warning(self):
        self._write(self._good_body() + "\n`origin/main` の `54455f3ec17` まで見た。\n")
        res = core.validate_diary(DAY, home=self.home, repos=[self.repo])
        self.assertTrue(res["ok"], res)
        self.assertIn("unverified_external_hash", [i["code"] for i in res["issues"]])

    def test_external_hash_pair_accepts_the_particle_wo(self):
        self._write(
            self._good_body() + "\n`origin/main` を `deadbeefcafebabe` として参照した。\n"
        )
        res = core.validate_diary(DAY, home=self.home, repos=[])
        self.assertTrue(res["ok"], res)
        self.assertIn("unverified_external_hash", [issue["code"] for issue in res["issues"]])


class RepoDiscoveryTests(unittest.TestCase):
    @staticmethod
    def _repo_at_depth(root: Path, depth: int) -> tuple[Path, str]:
        repo = root.joinpath(*[f"lvl{i}" for i in range(1, depth + 1)])
        repo.mkdir(parents=True)
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        }
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env)
        (repo / "a.txt").write_text("x")
        subprocess.run(["git", "add", "."], cwd=repo, check=True, env=env)
        subprocess.run(["git", "commit", "-qm", "test commit"], cwd=repo, check=True, env=env)
        sha = subprocess.run(
            ["git", "rev-parse", "--short=7", "HEAD"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        return repo, sha

    def _diary_with_hash(self, root: Path, sha: str) -> tuple[Path, Path]:
        home = make_profile(root)
        diary = home / "dreams" / f"{DAY}.md"
        diary.parent.mkdir(parents=True, exist_ok=True)
        diary.write_text(
            f"---\ndate: {DAY}\nauthor: '@tester'\nsessions: 1\nmessages: 1\n"
            f"commit: {sha}\n---\n\n# {DAY}\n\n"
            + PROSE
            + "\n",
            encoding="utf-8",
        )
        return home, diary

    def test_depth_five_repo_is_discovered_by_default(self):
        # ~/Dev/git/<org>/<team>/<area>/<repo> まで既定の探索で届くこと
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, _ = self._repo_at_depth(root, 5)
            self.assertIn(repo, core.discover_repos(base=str(root)))

    def test_hash_beyond_the_scan_depth_is_not_called_fabricated(self):
        # 既定深度を超える場所のリポジトリでも、深追いの再探索で実在を確認できれば error にしない
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, sha = self._repo_at_depth(root, core.REPO_SCAN_MAX_DEPTH + 1)
            self.assertNotIn(repo, core.discover_repos(base=str(root)))
            home, diary = self._diary_with_hash(root, sha)
            real = core.discover_repos
            with patch.object(
                core,
                "discover_repos",
                side_effect=lambda base=None, max_depth=None: real(
                    base=str(root), max_depth=max_depth
                ),
            ):
                res = core.validate_diary(path=str(diary), home=home)
            self.assertNotIn("unresolved_hash", res["errors"], res)
            self.assertTrue(res["ok"], res)

    def test_missing_hash_still_errors_after_the_deep_scan(self):
        # 深追いしても見つからないハッシュは従来どおり error（検査を弱めない）
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._repo_at_depth(root, 2)
            home, diary = self._diary_with_hash(root, "deadbeef1")
            real = core.discover_repos
            with patch.object(
                core,
                "discover_repos",
                side_effect=lambda base=None, max_depth=None: real(
                    base=str(root), max_depth=max_depth
                ),
            ):
                res = core.validate_diary(path=str(diary), home=home)
            self.assertIn("unresolved_hash", res["errors"], res)

    def test_nested_repos_are_discovered(self):
        # ~/Dev/git/<group>/<repo> のような入れ子も見つける（1階層だけだと捏造扱いになる）
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, _ = make_repo(root)  # root/repos/demorepo（深さ2）
            found = core.discover_repos(base=str(root))
            self.assertIn(repo, found)

    def test_hash_in_nested_repo_validates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = make_profile(root)
            _, sha = make_repo(root)
            diary = home / "dreams" / f"{DAY}.md"
            diary.parent.mkdir(parents=True, exist_ok=True)
            diary.write_text(
                f"---\ndate: {DAY}\nauthor: '@tester'\nsessions: 1\nmessages: 1\n"
                f"commit: {sha}\n---\n\n# {DAY}\n\n"
                + PROSE
                + "\n",
                encoding="utf-8",
            )
            res = core.validate_diary(
                path=str(diary), home=home, repos=core.discover_repos(base=str(root))
            )
            self.assertTrue(res["ok"], res)


class IndexAndRegistrationTests(unittest.TestCase):
    def test_index_refuses_custom_root_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "custom-hermes"
            (root / "profiles").mkdir(parents=True)
            result = core.build_index(home=root)
            self.assertFalse(result["ok"])
            self.assertIn("error", result)
            self.assertFalse((root / "dreams" / "INDEX.md").exists())

    def test_index_skips_unreadable_file(self):
        # 1本壊れても目次全体を失わない。理由は INDEX に残す
        with tempfile.TemporaryDirectory() as tmp:
            home = make_profile(Path(tmp))
            core.write_skeleton(DAY, home=home)
            broken = home / "dreams" / "2026-10-06.md"
            broken.write_bytes(b"\xff\xfe\x00bad")
            res = core.build_index(home=home)
            self.assertTrue(res["ok"])
            self.assertEqual(res["count"], 1)
            self.assertEqual([s["file"] for s in res["skipped"]], ["2026-10-06.md"])
            index = Path(res["path"]).read_text(encoding="utf-8")
            self.assertIn("読めなかった日記", index)
            self.assertIn("2026-10-06.md", index)

    def test_index_reports_date_like_names_it_cannot_list(self):
        # 書式違いの名前は INDEX から漏れる。黙って消えると気づけないので理由を残す。
        with tempfile.TemporaryDirectory() as tmp:
            home = make_profile(Path(tmp))
            core.write_skeleton(DAY, home=home)
            (home / "dreams" / "2026-11-3.md").write_text("noop", encoding="utf-8")
            (home / "dreams" / "persona.md").write_text("noop", encoding="utf-8")
            res = core.build_index(home=home)
            self.assertEqual(res["count"], 1)
            self.assertEqual([s["file"] for s in res["skipped"]], ["2026-11-3.md"])
            index = Path(res["path"]).read_text(encoding="utf-8")
            self.assertIn("2026-11-3.md", index)

    def test_index_counts_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = make_profile(Path(tmp))
            for day in (DAY, "2026-10-06"):
                core.write_skeleton(day, home=home)
            res = core.build_index(home=home)
            self.assertEqual(res["count"], 2)
            self.assertIn("2026-10-06", Path(res["path"]).read_text(encoding="utf-8"))

    def test_index_orders_variants_by_numeric_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = make_profile(Path(tmp))
            dreams = home / "dreams"
            dreams.mkdir(parents=True, exist_ok=True)
            for variant in (2, 10):
                (dreams / f"{DAY}-{variant}.md").write_text(
                    f"---\ndate: {DAY}\ncommit: none\n---\n\n## 明日の自分へ\nvariant {variant}\n",
                    encoding="utf-8",
                )
            result = core.build_index(home=home)
            index = Path(result["path"]).read_text(encoding="utf-8")
            self.assertLess(index.index(f"[{DAY}-10]"), index.index(f"[{DAY}-2]"))

    def test_index_reads_frontmatter_not_body(self):
        # frontmatter に無い項目を本文から拾うと目次が嘘になる。件数は frontmatter だけを読む
        with tempfile.TemporaryDirectory() as tmp:
            home = make_profile(Path(tmp))
            diary = home / "dreams" / f"{DAY}.md"
            diary.parent.mkdir(parents=True, exist_ok=True)
            diary.write_text(
                f"---\ndate: {DAY}\nauthor: \"@t\"\nsessions: 2\ncommit: none\n---\n\n"
                f"# {DAY}\n\n本文の抜粋マーカー。\n",
                encoding="utf-8",
            )
            res = core.build_index(home=home)
            index = Path(res["path"]).read_text(encoding="utf-8")
            self.assertIn("セッション 2", index)
            # frontmatter に messages が無いので、件数は目次に載らない（本文に書いても拾わない）
            self.assertNotIn("メッセージ", index)
            self.assertIn("本文の抜粋マーカー", index)

    def test_index_tool_refuses_root_home(self):
        # cron worker で home が root に解決されたとき、別ディレクトリへ INDEX を書かない
        import __init__ as plugin  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".hermes"
            root.mkdir()
            res = json.loads(plugin._index_handler({}, home=root))
        self.assertFalse(res["ok"])
        self.assertIn("プロファイル", res["error"])

    def test_register_exposes_four_tools(self):
        import __init__ as plugin  # noqa: PLC0415

        class Ctx:
            def __init__(self):
                self.calls = []

            def register_tool(self, name, toolset, schema, handler):
                self.calls.append((name, toolset, schema, handler))

        ctx = Ctx()
        plugin.register(ctx)
        names = [c[0] for c in ctx.calls]
        self.assertEqual(
            names, ["dreams_material", "dreams_write", "dreams_validate", "dreams_index"]
        )
        self.assertTrue(all(c[1] == "dreams_diary" for c in ctx.calls))
        # ハンドラは例外を握って JSON を返す
        with patch.object(plugin.logger, "warning"):
            out = ctx.calls[0][3]({"date": "not-a-date"})
        self.assertIn("error", out)

    def test_expected_input_error_does_not_log_traceback(self):
        import __init__ as plugin  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            home = make_profile(Path(tmp))
            wrapped = plugin._safe(plugin._material_handler, home=home)
            with patch.object(plugin.logger, "warning") as warning, \
                 patch.object(plugin.logger, "exception") as exception:
                response = wrapped({"date": "not-a-date"})
        self.assertIn("ValueError", response)
        warning.assert_called_once()
        exception.assert_not_called()


class ProfileHomeTests(unittest.TestCase):
    def test_context_local_hermes_home_beats_root_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".hermes"
            profile_home = root / "profiles" / "assistant"
            profile_home.mkdir(parents=True)
            host = types.ModuleType("hermes_constants")
            host.get_hermes_home = lambda: profile_home
            with patch.dict(os.environ, {
                "HERMES_HOME": str(root),
                "HERMES_PROFILE": "coding",
            }), patch.dict(sys.modules, {"hermes_constants": host}):
                self.assertEqual(core.resolve_profile_home(), profile_home)

    def test_registered_tools_use_manager_scope_not_worker_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root-home"
            profile_home = root / "profiles" / "assistant"
            target = profile_home / "dreams" / f"{DAY}.md"
            target.parent.mkdir(parents=True)
            target.write_text("existing diary", encoding="utf-8")
            active_home = {"path": root}
            host = types.ModuleType("hermes_constants")
            host.get_hermes_home = lambda: active_home["path"]

            class Ctx:
                def __init__(self):
                    self.calls = {}
                    self._manager = types.SimpleNamespace(home_path=profile_home)

                def register_tool(self, name, toolset, schema, handler):
                    self.calls[name] = handler

            import __init__ as plugin  # noqa: PLC0415

            with patch.dict(sys.modules, {"hermes_constants": host}):
                ctx = Ctx()
                plugin.register(ctx)
                result = ctx.calls["dreams_write"]({"date": DAY})

            self.assertIn(str(target), result)
            self.assertIn('"reason": "exists"', result)
            self.assertFalse((root / "dreams" / f"{DAY}.md").exists())


class ProfileSourceTests(unittest.TestCase):
    """home 解決の経路が呼び出し側に分かること（黙って別プロファイルへ落ちない）。"""

    def _stub(self, root: Path):
        stub = types.ModuleType("hermes_constants")
        stub.get_hermes_home = lambda: root
        stub.get_hermes_home_override = lambda: None
        return stub

    def test_profile_resolves_under_a_custom_hermes_root(self):
        # HERMES_HOME が標準外の場所を指すとき、~/.hermes の同名プロファイルを選ばない
        with tempfile.TemporaryDirectory() as tmp:
            custom = Path(tmp) / "custom-hermes"
            profile_home = custom / "profiles" / "tester"
            profile_home.mkdir(parents=True)
            decoy = Path(tmp) / ".hermes" / "profiles" / "tester"
            decoy.mkdir(parents=True)
            stub = self._stub(profile_home)
            with patch.dict(
                os.environ,
                {"HOME": tmp, "HERMES_HOME": str(profile_home), "HERMES_PROFILE": ""},
            ), patch.dict(sys.modules, {"hermes_constants": stub}):
                home, source = core.resolve_profile_home_source("tester")
            self.assertEqual(source, "explicit")
            self.assertEqual(home, profile_home)

    def test_hermes_root_override_is_honoured(self):
        with tempfile.TemporaryDirectory() as tmp:
            explicit = Path(tmp) / "elsewhere"
            with patch.dict(os.environ, {"DREAMS_HERMES_ROOT": str(explicit)}):
                self.assertEqual(core._hermes_root(), explicit)
                self.assertEqual(core._profiles_root(), explicit / "profiles")

    def test_active_profile_is_reported_as_unreliable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".hermes"
            (root / "profiles" / "bloger").mkdir(parents=True)
            (root / "active_profile").write_text("bloger\n", encoding="utf-8")
            with patch.dict(os.environ, {"HOME": tmp, "HERMES_HOME": "", "HERMES_PROFILE": ""}), \
                 patch.dict(sys.modules, {"hermes_constants": self._stub(root)}):
                home, source = core.resolve_profile_home_source()
            self.assertEqual(source, "active_profile")
            self.assertEqual(home.name, "bloger")

    def test_env_home_is_reliable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".hermes"
            profile_home = root / "profiles" / "research"
            profile_home.mkdir(parents=True)
            with patch.dict(os.environ, {"HERMES_HOME": str(profile_home)}), \
                 patch.dict(sys.modules, {"hermes_constants": self._stub(root)}):
                home, source = core.resolve_profile_home_source()
            self.assertEqual(source, "env")
            self.assertEqual(home, profile_home)

    def test_explicit_profile_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".hermes"
            (root / "profiles" / "bloger").mkdir(parents=True)
            (root / "active_profile").write_text("bloger\n", encoding="utf-8")
            with patch.dict(os.environ, {"HOME": tmp, "HERMES_HOME": "", "HERMES_PROFILE": ""}), \
                 patch.dict(sys.modules, {"hermes_constants": self._stub(root)}):
                home, source = core.resolve_profile_home_source("bloger")
            self.assertEqual(source, "explicit")
            self.assertEqual(home.name, "bloger")

    def test_missing_explicit_profile_does_not_fall_through(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "custom-hermes"
            research = root / "profiles" / "research"
            research.mkdir(parents=True)
            stub = self._stub(research)
            with patch.dict(
                os.environ,
                {"HERMES_HOME": str(research), "HERMES_PROFILE": "research"},
            ), patch.dict(sys.modules, {"hermes_constants": stub}):
                with self.assertRaisesRegex(ValueError, "profile|プロファイル"):
                    core.resolve_profile_home_source("this-profile-does-not-exist-xyz")

    def test_explicit_profile_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "custom-hermes"
            (root / "profiles" / "research").mkdir(parents=True)
            stub = self._stub(root)
            with patch.dict(
                os.environ,
                {"HERMES_HOME": "", "HERMES_PROFILE": ""},
            ), patch.dict(sys.modules, {"hermes_constants": stub}):
                for profile in ("..", "../..", "../custom-hermes"):
                    with self.subTest(profile=profile), self.assertRaises(ValueError):
                        core.resolve_profile_home_source(profile)

    def test_root_home_detection_uses_profile_layout_not_dot_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "custom-hermes"
            profile = root / "profiles" / "research"
            profile.mkdir(parents=True)
            self.assertTrue(core.is_root_home(root))
            self.assertFalse(core.is_root_home(profile))


class CliTests(unittest.TestCase):
    def test_cli_index_returns_failure_when_root_home_is_refused(self):
        import cli as cli_mod  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "custom-hermes"
            (root / "profiles").mkdir(parents=True)
            stub = types.ModuleType("hermes_constants")
            stub.get_hermes_home = lambda: root
            stub.get_hermes_home_override = lambda: None
            out = io.StringIO()
            err = io.StringIO()
            with patch.dict(
                os.environ,
                {"HOME": tmp, "HERMES_HOME": str(root), "HERMES_PROFILE": ""},
            ), patch.dict(sys.modules, {"hermes_constants": stub}), \
                 redirect_stderr(err), redirect_stdout(out):
                code = cli_mod.main(["index"])
            self.assertEqual(code, 1)
            self.assertFalse((root / "dreams" / "INDEX.md").exists())
            self.assertFalse(json.loads(out.getvalue())["ok"])
            self.assertIn("警告", err.getvalue())

    def test_cli_rejects_missing_explicit_profile_instead_of_writing_fallback(self):
        import cli as cli_mod  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "custom-hermes"
            research = root / "profiles" / "research"
            research.mkdir(parents=True)
            stub = types.ModuleType("hermes_constants")
            stub.get_hermes_home = lambda: research
            stub.get_hermes_home_override = lambda: None
            err = io.StringIO()
            with patch.dict(
                os.environ,
                {"HERMES_HOME": str(research), "HERMES_PROFILE": "research"},
            ), patch.dict(sys.modules, {"hermes_constants": stub}), \
                 redirect_stderr(err), redirect_stdout(io.StringIO()):
                code = cli_mod.main(["--profile", "missing", "write", DAY])
            self.assertEqual(code, 2)
            self.assertIn("missing", err.getvalue())
            self.assertFalse((research / "dreams" / f"{DAY}.md").exists())

    def test_cli_warns_when_profile_is_unscoped(self):
        import cli as cli_mod  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".hermes"
            (root / "profiles" / "bloger" / "dreams").mkdir(parents=True)
            (root / "active_profile").write_text("bloger\n", encoding="utf-8")
            stub = types.ModuleType("hermes_constants")
            stub.get_hermes_home = lambda: root
            stub.get_hermes_home_override = lambda: None
            err = io.StringIO()
            with patch.dict(os.environ, {"HOME": tmp, "HERMES_HOME": "", "HERMES_PROFILE": ""}), \
                 patch.dict(sys.modules, {"hermes_constants": stub}), \
                 redirect_stderr(err), redirect_stdout(io.StringIO()):
                code = cli_mod.main(["index"])
            self.assertEqual(code, 0)
            self.assertIn("警告", err.getvalue())
            self.assertIn("bloger", err.getvalue())

    def test_cli_profile_flag_is_silent(self):
        import cli as cli_mod  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".hermes"
            (root / "profiles" / "bloger" / "dreams").mkdir(parents=True)
            (root / "profiles" / "tester" / "dreams").mkdir(parents=True)
            (root / "active_profile").write_text("bloger\n", encoding="utf-8")
            stub = types.ModuleType("hermes_constants")
            stub.get_hermes_home = lambda: root
            stub.get_hermes_home_override = lambda: None
            err = io.StringIO()
            with patch.dict(os.environ, {"HOME": tmp, "HERMES_HOME": "", "HERMES_PROFILE": ""}), \
                 patch.dict(sys.modules, {"hermes_constants": stub}), \
                 redirect_stderr(err), redirect_stdout(io.StringIO()):
                code = cli_mod.main(["--profile", "tester", "index"])
            self.assertEqual(code, 0)
            self.assertEqual(err.getvalue(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
