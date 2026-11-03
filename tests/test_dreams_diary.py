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
        self.assertLessEqual(len(material), 1200)
        self.assertIn("## コミット", material)
        self.assertIn(self.sha, material)
        self.assertIn("テスト用の記憶", material)


    def test_material_reports_previous_diary_ok(self):
        # post-run フックが無い代わりに、翌日の素材で前回分の検証結果を知らせる
        prev = self.home / "dreams" / "2026-10-04.md"
        prev.parent.mkdir(parents=True, exist_ok=True)
        prev.write_text(
            "---\ndate: 2026-10-04\nauthor: '@tester'\nsessions: 1\nmessages: 1\n"
            "commit: none\n---\n\n# 2026-10-04\n\n"
            + "\n\n".join(f"{s}\n本文。" for s in core.SECTIONS)
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
            + "\n\n".join(f"{s}\n本文。" for s in core.SECTIONS)
            + "\n",
            encoding="utf-8",
        )
        (dreams / "2026-10-05.md").write_text("---\ndate: 2026-10-05\n---\n本文だけ。\n", encoding="utf-8")
        res = core.audit_diaries(home=self.home, days=5, repos=[self.repo])
        self.assertFalse(res["ok"])
        self.assertEqual(res["checked"], 2)
        self.assertEqual(res["failed"], ["2026-10-05.md"])

    def test_author_falls_back_from_root_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            root_home = Path(tmp) / ".hermes"
            res = core.write_skeleton(DAY, home=root_home)
            text = Path(res["path"]).read_text(encoding="utf-8")
            self.assertIn('author: "@agent"', text)
            self.assertNotIn(".hermes", text.split("---")[1])


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

    def _section_body(self) -> str:
        return (
            f"# {DAY}\n\n"
            + "\n\n".join(f"{s}\n本文。（{self.sha}）" for s in core.SECTIONS)
            + "\n"
        )

    def _good_body(self) -> str:
        return (
            f"---\ndate: {DAY}\nauthor: '@tester'\nsessions: 1\nmessages: 1\ncommit: none\n---\n\n"
            + self._section_body()
        )

    def test_valid_entry_passes(self):
        self._write(self._good_body())
        res = core.validate_diary(DAY, home=self.home, repos=[self.repo])
        self.assertTrue(res["ok"], res)

    def test_missing_frontmatter_fails(self):
        self._write(self._section_body())
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

    def test_short_prose_warns_even_when_file_is_long(self):
        # frontmatter＋見出し（実測 140〜160 字）を字数に含めると、要約1文の日記が通ってしまう
        self._write(self._good_body())
        res = core.validate_diary(DAY, home=self.home, repos=[self.repo])
        self.assertTrue(res["ok"], res)
        self.assertIn("too_short", [i["code"] for i in res["issues"]])
        self.assertLess(res["chars"], core.MIN_CHARS_SOFT)

    def test_prose_chars_exclude_frontmatter_and_headings(self):
        long = "\n\n".join(f"{s}\n" + "あ" * 150 for s in core.SECTIONS)
        body = (
            f"---\ndate: {DAY}\nauthor: '@tester'\nsessions: 1\nmessages: 1\ncommit: none\n---\n\n"
            f"# {DAY}\n\n{long}\n"
        )
        self._write(body)
        res = core.validate_diary(DAY, home=self.home, repos=[self.repo])
        self.assertTrue(res["ok"], res)
        self.assertNotIn("too_short", [i["code"] for i in res["issues"]])
        self.assertEqual(res["chars"], 150 * len(core.SECTIONS))

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


class RepoDiscoveryTests(unittest.TestCase):
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
                + "\n\n".join(f"{s}\n本文。" for s in core.SECTIONS)
                + "\n",
                encoding="utf-8",
            )
            res = core.validate_diary(
                path=str(diary), home=home, repos=core.discover_repos(base=str(root))
            )
            self.assertTrue(res["ok"], res)


class IndexAndRegistrationTests(unittest.TestCase):
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

    def test_index_counts_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = make_profile(Path(tmp))
            for day in (DAY, "2026-10-06"):
                core.write_skeleton(day, home=home)
            res = core.build_index(home=home)
            self.assertEqual(res["count"], 2)
            self.assertIn("2026-10-06", Path(res["path"]).read_text(encoding="utf-8"))

    def test_index_reads_frontmatter_not_body(self):
        # frontmatter に無い項目を本文から拾うと目次が嘘になる。frontmatter だけを読む
        with tempfile.TemporaryDirectory() as tmp:
            home = make_profile(Path(tmp))
            diary = home / "dreams" / f"{DAY}.md"
            diary.parent.mkdir(parents=True, exist_ok=True)
            diary.write_text(
                f"---\ndate: {DAY}\nauthor: \"@t\"\nsessions: 2\ncommit: none\n---\n\n"
                f"# {DAY}\n\n## 考えたこと\nmessages: 999\n",
                encoding="utf-8",
            )
            res = core.build_index(home=home)
            index = Path(res["path"]).read_text(encoding="utf-8")
            self.assertIn("セッション 2", index)
            self.assertNotIn("メッセージ 999", index)
            self.assertNotIn("999", index)

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
        out = ctx.calls[0][3]({"date": "not-a-date"})
        self.assertIn("error", out)


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


class CliTests(unittest.TestCase):
    def test_cli_warns_when_profile_is_unscoped(self):
        import cli as cli_mod  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".hermes"
            (root / "profiles" / "bloger").mkdir(parents=True)
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
            (root / "profiles" / "bloger").mkdir(parents=True)
            (root / "profiles" / "tester").mkdir(parents=True)
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
