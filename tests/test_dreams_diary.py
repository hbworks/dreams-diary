"""dreams-diary プラグインのテスト（標準ライブラリのみ）。"""

from __future__ import annotations

import datetime
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main as core  # noqa: E402

DAY = "2026-10-05"


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

    def test_material_truncates(self):
        got = core.collect_material(DAY, home=self.home, max_chars=80, repos=[self.repo])
        self.assertTrue(got["truncated"])
        self.assertLessEqual(len(got["material"]), 200)


class SkeletonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = make_profile(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_skeleton_has_all_sections(self):
        res = core.write_skeleton(DAY, home=self.home)
        self.assertTrue(res["ok"])
        text = Path(res["path"]).read_text(encoding="utf-8")
        for section in core.SECTIONS:
            self.assertIn(section, text)
        self.assertIn(f"date: {DAY}", text)

    def test_refuses_overwrite(self):
        core.write_skeleton(DAY, home=self.home)
        res = core.write_skeleton(DAY, home=self.home)
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "exists")
        forced = core.write_skeleton(DAY, home=self.home, force=True)
        self.assertTrue(forced["ok"])


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

    def _good_body(self) -> str:
        return (
            f"# {DAY}\n\n"
            + "\n\n".join(f"{s}\n本文。（{self.sha}）" for s in core.SECTIONS)
            + "\n"
        )

    def test_valid_entry_passes(self):
        self._write(self._good_body())
        res = core.validate_diary(DAY, home=self.home, repos=[self.repo])
        self.assertTrue(res["ok"], res)

    def test_missing_section_fails(self):
        body = self._good_body().replace(core.SECTIONS[-1], "")
        self._write(body)
        res = core.validate_diary(DAY, home=self.home, repos=[self.repo])
        self.assertFalse(res["ok"])
        self.assertIn("missing_section", res["errors"])

    def test_second_person_fails(self):
        self._write(self._good_body().replace("本文。", "あなたは今日こう言った。"))
        res = core.validate_diary(DAY, home=self.home, repos=[self.repo])
        self.assertFalse(res["ok"])
        self.assertIn("second_person", res["errors"])

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


class IndexAndRegistrationTests(unittest.TestCase):
    def test_index_counts_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = make_profile(Path(tmp))
            for day in (DAY, "2026-10-06"):
                core.write_skeleton(day, home=home)
            res = core.build_index(home=home)
            self.assertEqual(res["count"], 2)
            self.assertIn("2026-10-06", Path(res["path"]).read_text(encoding="utf-8"))

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
        out = ctx.calls[0][3]({"date": "not-a-date"})
        self.assertIn("error", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
