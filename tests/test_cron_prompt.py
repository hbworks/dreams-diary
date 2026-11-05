"""cron プロンプトの単一ソース化テスト（標準ライブラリのみ）。

守る契約:
- 見出しと字数の下限は main.py の定数から注入される（散文側に数値を書かない）。
- 描画結果に未展開トークンが残らない。トークンが欠けたテンプレートは黙って通さない。
- 実ジョブのプロンプトは script 名で同定する（ジョブ名はプロファイルごとに違う）。
- 適用は書き込み後に jobs.json を読み直して検証する。
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import cli as cli_mod  # noqa: E402
import main as core  # noqa: E402

TEMPLATE = REPO_ROOT / "templates" / "cron-prompt.md"
SCRIPT = "dreams_diary_material.py"


def _write_jobs(path: Path, prompt: str, name: str = "dreams-diary", job_id: str = "job1") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"jobs": [{"id": job_id, "name": name, "prompt": prompt, "script": SCRIPT}]}),
        encoding="utf-8",
    )


class RenderTests(unittest.TestCase):
    def test_sections_are_injected_in_order(self):
        text = core.render_cron_prompt()
        positions = [text.index(heading) for heading in core.SECTIONS]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("## 明日の自分へ", text)

    def test_no_token_remains(self):
        self.assertNotIn("{{", core.render_cron_prompt())

    def test_editor_notes_are_not_shipped(self):
        self.assertIn("<!--", TEMPLATE.read_text(encoding="utf-8"))
        self.assertNotIn("<!--", core.render_cron_prompt())

    def test_floor_comes_from_the_constant(self):
        with patch.object(core, "MIN_CHARS_SOFT", 1234):
            text = core.render_cron_prompt()
        self.assertIn("1234字以上", text)
        self.assertNotIn("800字以上", text)

    def test_template_does_not_restate_the_rules(self):
        raw = TEMPLATE.read_text(encoding="utf-8")
        for heading in core.SECTIONS:
            self.assertNotIn(heading, raw, f"見出しが散文へ再重複している: {heading}")
        self.assertNotIn("800", raw, "字数の下限が散文へ再重複している")

    def test_prompt_requires_session_times(self):
        text = core.render_cron_prompt()
        self.assertIn("HH:MM", text)
        self.assertIn("セッション", text)

    def test_prompt_names_the_success_section(self):
        """素材に `## 成功した操作` があることを書き手へ伝える（terminal の仕事の受け皿）。"""
        self.assertIn("成功した操作", core.render_cron_prompt())

    def test_prompt_scopes_the_commit_section_to_patch_and_write_file(self):
        """コミット節の範囲を patch / write_file と明示し、terminal 編集を数えないと書く。"""
        text = core.render_cron_prompt()
        self.assertIn("patch / write_file", text)
        self.assertIn("terminal 経由の編集", text)

    def test_prompt_scopes_the_emotion_ban_to_the_user(self):
        """心情の断定禁止はユーザーに係る。書き手自身の反応は書いてよい（抑止しない）。"""
        text = core.render_cron_prompt()
        self.assertIn("ユーザーの心情・意図は断定しない", text)
        self.assertIn("自分の感情は書いてよい", text)

    def test_prompt_allows_the_writers_own_judgment(self):
        """根拠が薄い判断も、自分が決めたことなら第2節に書ける（素材の範囲に閉じ込めない）。"""
        text = core.render_cron_prompt()
        self.assertIn("判断は自分のものとして書く", text)
        self.assertIn("薄さごと第2節に書いてよい", text)

    def test_readme_section_list_matches_the_two_sentence_rule(self):
        """README の節一覧が「一行」と言い、テンプレートが「2文以上」と言う食い違いを防ぐ。"""
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertNotIn("明日の自分へ — one line", readme)
        self.assertIn("two sentences or more", readme)

    def test_missing_token_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "prompt.md"
            broken.write_text("本文だけ。{{MIN_CHARS}} はあるが節は無い。", encoding="utf-8")
            with self.assertRaises(ValueError):
                core.render_cron_prompt(broken)

    def test_unknown_token_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "prompt.md"
            broken.write_text("{{SECTIONS}}\n{{MIN_CHARS}}\n{{NOPE}}\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                core.render_cron_prompt(broken)


class CheckTests(unittest.TestCase):
    def test_missing_only_profile_fails_check_and_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "profiles"
            path = root / "alpha" / "cron" / "jobs.json"
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps({"jobs": [{"id": "other", "script": "other.py"}]}),
                encoding="utf-8",
            )
            result = core.check_cron_prompt(profiles_root=root)
            out = io.StringIO()
            with patch.dict(os.environ, {"DREAMS_PROFILES_ROOT": str(root)}), \
                 redirect_stdout(out), redirect_stderr(io.StringIO()):
                code = cli_mod.main(["prompt", "--check"])
        self.assertEqual(result["missing_profiles"], ["alpha"])
        self.assertFalse(result["ok"])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(out.getvalue())["ok"])

    def test_reports_same_and_diff(self):
        rendered = core.render_cron_prompt()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "profiles"
            _write_jobs(root / "alpha" / "cron" / "jobs.json", rendered)
            _write_jobs(root / "beta" / "cron" / "jobs.json", "古いプロンプト")
            (root / "gamma" / "cron").mkdir(parents=True)
            (root / "gamma" / "cron" / "jobs.json").write_text(
                json.dumps({"jobs": [{"id": "x", "name": "other", "prompt": "p", "script": "other.py"}]}),
                encoding="utf-8",
            )
            result = core.check_cron_prompt(profiles_root=root)

        states = {row["profile"]: row["state"] for row in result["jobs"]}
        self.assertEqual(states, {"alpha": "same", "beta": "diff"})
        self.assertEqual(result["missing_profiles"], ["gamma"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["template_sha"], core._text_sha(rendered))

    def test_job_is_identified_by_script_not_name(self):
        rendered = core.render_cron_prompt()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "profiles"
            _write_jobs(root / "research" / "cron" / "jobs.json", rendered, name="muse-dreams-diary")
            result = core.check_cron_prompt(profiles_root=root)
        self.assertEqual(result["jobs"][0]["state"], "same")
        self.assertEqual(result["jobs"][0]["name"], "muse-dreams-diary")

    def test_cli_check_exits_nonzero_on_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "profiles"
            _write_jobs(root / "beta" / "cron" / "jobs.json", "古いプロンプト")
            out = io.StringIO()
            with patch.dict(os.environ, {"DREAMS_PROFILES_ROOT": str(root)}), \
                 redirect_stdout(out), redirect_stderr(io.StringIO()):
                code = cli_mod.main(["prompt", "--check"])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(out.getvalue())["ok"])

    def test_cli_prompt_prints_without_touching_a_profile(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli_mod.main(["prompt"])
        self.assertEqual(code, 0)
        self.assertEqual(err.getvalue(), "")
        self.assertIn(core.SECTIONS[0], out.getvalue())

    def test_mode_flags_are_rejected_outside_prompt(self):
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = cli_mod.main(["material", "--check"])
        self.assertEqual(code, 2)
        self.assertIn("prompt", err.getvalue())

    def test_broken_template_does_not_leak_a_traceback(self):
        err = io.StringIO()
        with patch.object(core, "render_cron_prompt", side_effect=ValueError("boom")), \
             redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = cli_mod.main(["prompt", "--apply", "--profile", "alpha"])
        self.assertEqual(code, 2)
        self.assertIn("boom", err.getvalue())

    def test_unreadable_jobs_file_is_not_reported_as_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "profiles"
            broken = root / "alpha" / "cron" / "jobs.json"
            broken.parent.mkdir(parents=True)
            broken.write_text("{not json", encoding="utf-8")
            result = core.check_cron_prompt(profiles_root=root)
        self.assertEqual(result["unreadable_profiles"], ["alpha"])
        self.assertEqual(result["missing_profiles"], [])
        self.assertFalse(result["ok"])


class ApplyTests(unittest.TestCase):
    def _runner_that_writes(self, jobs_path: Path, rendered: str):
        calls = []

        def runner(cmd):
            calls.append(cmd)
            _write_jobs(jobs_path, rendered, job_id=cmd[5])
            return 0

        return runner, calls

    def test_apply_verifies_the_stored_prompt(self):
        rendered = core.render_cron_prompt()
        with tempfile.TemporaryDirectory() as tmp:
            jobs_path = Path(tmp) / "profiles" / "alpha" / "cron" / "jobs.json"
            _write_jobs(jobs_path, "古いプロンプト")
            runner, calls = self._runner_that_writes(jobs_path, rendered)
            result = core.apply_cron_prompt("alpha", jobs_path=jobs_path, runner=runner)

        self.assertTrue(result["ok"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:5], ["hermes", "-p", "alpha", "cron", "edit"])
        self.assertTrue(result["applied"][0]["verified"])

    def test_apply_fails_when_the_write_did_not_land(self):
        with tempfile.TemporaryDirectory() as tmp:
            jobs_path = Path(tmp) / "profiles" / "alpha" / "cron" / "jobs.json"
            _write_jobs(jobs_path, "古いプロンプト")
            result = core.apply_cron_prompt("alpha", jobs_path=jobs_path, runner=lambda cmd: 0)

        self.assertFalse(result["ok"])
        self.assertFalse(result["applied"][0]["verified"])

    def test_apply_requires_an_explicit_profile(self):
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = cli_mod.main(["prompt", "--apply"])
        self.assertEqual(code, 2)
        self.assertIn("--profile", err.getvalue())

    def test_apply_surfaces_stderr_from_the_edit(self):
        with tempfile.TemporaryDirectory() as tmp:
            jobs_path = Path(tmp) / "profiles" / "alpha" / "cron" / "jobs.json"
            _write_jobs(jobs_path, "古いプロンプト")
            result = core.apply_cron_prompt(
                "alpha", jobs_path=jobs_path, runner=lambda cmd: (1, "job not found")
            )
        row = result["applied"][0]
        self.assertEqual(row["exit_code"], 1)
        self.assertEqual(row["stderr"], "job not found")
        self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main()
