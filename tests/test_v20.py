"""v2.0（散文の日記・この人のこと・最終段落の抜粋）のテスト。"""

from __future__ import annotations

import datetime
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main as core  # noqa: E402

DAY = "2026-10-05"
PROSE = (
    "22:46、画面にはまだ差分が残っていた。ユーザーは「そこは残して」と言っていて、"
    "その一言で優先順位が決まった。\n\n"
    "最初は表示側だけ直せば足りると考えたが、再構築の経路を読むと固定費の計算も"
    "同じだけずれていた。手間をかけさせたのは自分だ。\n\n"
    "明日は、切り詰めのテストを先に流してから本文の上限を触る。"
)


def _ts(day: str, hh: int, mm: int = 0) -> float:
    return datetime.datetime.strptime(f"{day} {hh}:{mm:02d}", "%Y-%m-%d %H:%M").timestamp()


def make_profile(root: Path, name: str | None = None) -> Path:
    home = root / "profiles" / "tester"
    home.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(home / "state.db")
    con.execute(
        "create table messages (session_id text, role text, content text, tool_name text, timestamp real)"
    )
    con.commit()
    con.close()
    mem = sqlite3.connect(home / "memory.db")
    mem.execute("create table memories (id integer primary key, category text, content text, created_at text)")
    mem.commit()
    mem.close()
    if name:
        (home / "dreams").mkdir(parents=True, exist_ok=True)
        (home / "dreams" / "user.md").write_text(
            f"---\nname: {name}\n---\n", encoding="utf-8"
        )
    return home


class UserNameTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _home(self) -> Path:
        home = make_profile(self.root)
        (home / "memories").mkdir(exist_ok=True)
        return home

    def test_explicit_file_wins_over_memory_files(self):
        home = self._home()
        (home / "dreams").mkdir(parents=True, exist_ok=True)
        (home / "dreams" / "user.md").write_text("---\nname: まゆ\n---\n", encoding="utf-8")
        (home / "memories" / "USER.md").write_text("ユーザーの名前は「すと」。\n", encoding="utf-8")
        self.assertEqual(core._user_name_source(home), ("まゆ", "dreams/user.md"))

    def test_user_memory_file_is_used(self):
        home = self._home()
        (home / "memories" / "USER.md").write_text(
            "User prefers short answers.\n§\nユーザーの呼び名は「まゆ」です。\n", encoding="utf-8"
        )
        self.assertEqual(core._user_name_source(home), ("まゆ", "memories/USER.md"))
        self.assertIn("- 呼び名: まゆ", core._person_material(DAY, home))

    def test_memory_md_is_used_next(self):
        home = self._home()
        (home / "memories" / "MEMORY.md").write_text("name: まゆ\n", encoding="utf-8")
        self.assertEqual(core._user_name_source(home), ("まゆ", "memories/MEMORY.md"))

    def test_memory_db_is_the_last_source(self):
        home = self._home()
        con = sqlite3.connect(home / "memory.db")
        con.execute(
            "insert into memories (category, content, created_at) values (?, ?, ?)",
            ("user", "ユーザーの名前は「まゆ」", "2026-10-01 10:00:00"),
        )
        con.commit()
        con.close()
        self.assertEqual(core._user_name_source(home), ("まゆ", "memory.db"))

    def test_sentences_without_an_explicit_name_give_nothing(self):
        home = self._home()
        (home / "memories" / "USER.md").write_text(
            "User prefers direct summaries.\nユーザーは日本語話者。\n", encoding="utf-8"
        )
        self.assertEqual(core._user_name_source(home), ("", ""))
        self.assertIn("呼び名: （未設定", core._person_material(DAY, home))

    def test_agent_handle_and_placeholders_are_rejected(self):
        home = self._home()
        (home / "memories" / "USER.md").write_text("ユーザー名は「@coding」\n", encoding="utf-8")
        self.assertEqual(core._user_name_source(home), ("", ""))
        (home / "memories" / "USER.md").write_text("ユーザーの名前は「ユーザー」\n", encoding="utf-8")
        self.assertEqual(core._user_name_source(home), ("", ""))

    def test_honorific_is_stripped(self):
        home = self._home()
        (home / "memories" / "USER.md").write_text("ユーザーの名前はまゆさん。\n", encoding="utf-8")
        self.assertEqual(core._user_name(home), "まゆ")


class PersonMaterialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_configured_name_is_used(self):
        home = make_profile(self.root, name="まゆ")
        material = core.collect_material(DAY, home=home, repos=[])["material"]
        section = core._person_material(DAY, home)
        self.assertIn("## この人のこと（直近14日の機械抽出）", material)
        self.assertIn("- 呼び名: まゆ", section)

    def test_missing_user_file_falls_back_to_user(self):
        home = make_profile(self.root)
        section = core._person_material(DAY, home)
        self.assertIn("呼び名: （未設定。本文では「ユーザー」と書く）", section)
        self.assertIn("（直近14日に材料は無い）", section)

    def test_memory_lists_only_user_and_preference_within_window(self):
        home = make_profile(self.root)
        con = sqlite3.connect(home / "memory.db")
        con.executemany(
            "insert into memories (category, content, created_at) values (?, ?, ?)",
            [
                ("preference", "返答は簡潔に。前置きを省く", "2026-10-03 10:00:00"),
                ("user", "呼び方は「まゆ」", "2026-09-30 09:00:00"),
                ("rule", "これは人物像ではない", "2026-10-04 10:00:00"),
                ("preference", "古すぎる項目", "2026-08-01 10:00:00"),
            ],
        )
        con.commit()
        con.close()
        lines = core._person_memory(DAY, home)
        self.assertEqual(len(lines), 2)
        self.assertIn("返答は簡潔に", " ".join(lines))
        self.assertIn("呼び方は「まゆ」", " ".join(lines))
        self.assertNotIn("人物像ではない", " ".join(lines))
        self.assertNotIn("古すぎる項目", " ".join(lines))

    def _seed_messages(self):
        home = make_profile(self.root)
        con = sqlite3.connect(home / "state.db")
        rows = []
        for day in ("2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05"):
            rows.append(("s", "user", "もっと見やすくして。確認してから進めて", None, _ts(day, 10)))
        rows.append(("s", "user", "今日だけの言い回し", None, _ts(DAY, 11)))
        con.executemany(
            "insert into messages (session_id, role, content, tool_name, timestamp) values (?,?,?,?,?)",
            rows,
        )
        con.commit()
        con.close()
        return home

    def test_phrases_are_deterministic_and_pick_words(self):
        home = self._seed_messages()
        first = core._person_phrases(DAY, home)
        second = core._person_phrases(DAY, home)
        self.assertEqual(first, second)
        self.assertTrue(first)
        joined = " ".join(first)
        self.assertIn("確認", joined)
        # 語の断片（英字・助詞だけ・1日だけの言い回し）は拾わない
        for item in first:
            self.assertNotIn("「me」", item)
            self.assertNotIn("「のに」", item)
            self.assertNotIn("「して」", item)
        self.assertNotIn("今日だけ", joined)

    def test_person_section_survives_truncation(self):
        home = make_profile(self.root, name="まゆ")
        long_conversation = "".join(f"- 10:00 発言: 長い会話{index}\n" for index in range(200))
        with patch.object(core, "_sessions_material", return_value=long_conversation), patch.object(
            core, "_own_statements_material", return_value="- 23:59 応答: 抜粋\n"
        ):
            got = core.collect_material(DAY, home=home, max_chars=1600, repos=[])
        self.assertTrue(got["truncated"])
        self.assertIn("## この人のこと（直近14日の機械抽出）", got["material"])
        self.assertIn("- 呼び名: まゆ", got["material"])
        self.assertIn("## 自分の発言", got["material"])
        self.assertLessEqual(len(got["material"]), 1600)


class ProseDiaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = make_profile(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, text: str) -> Path:
        path = self.home / "dreams" / f"{DAY}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_prose_diary_is_valid(self):
        self._write(
            f"---\ndate: {DAY}\nauthor: '@tester'\nsessions: 1\nmessages: 1\ncommit: none\n---\n\n"
            f"# {DAY}\n\n{PROSE}\n"
        )
        result = core.validate_diary(DAY, home=self.home, repos=[])
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["errors"], [])

    def test_index_excerpt_is_the_last_paragraph(self):
        self._write(
            f"---\ndate: {DAY}\nauthor: '@tester'\nsessions: 1\nmessages: 1\ncommit: none\n---\n\n"
            f"# {DAY}\n\n{PROSE}\n"
        )
        result = core.build_index(home=self.home)
        index = Path(result["path"]).read_text(encoding="utf-8")
        self.assertIn("明日は、切り詰めのテストを先に流してから本文の上限を触る。", index)
        self.assertNotIn("22:46、画面にはまだ差分が残っていた。", index)

    def test_last_paragraph_ignores_heading_only_tail(self):
        text = (
            f"---\ndate: {DAY}\ncommit: none\n---\n\n# {DAY}\n\n"
            "本文の段落。\n\n## 明日の自分へ\n"
        )
        self.assertEqual(core._last_paragraph(text), "本文の段落。")


if __name__ == "__main__":
    unittest.main(verbosity=2)
