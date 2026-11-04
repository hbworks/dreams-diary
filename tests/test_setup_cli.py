"""セットアップ・同期 CLI（docs/SPEC-setup-cli.md）のテスト。

- install: symlink 作成、スクリプト配置、plugins enable、cron create / 既存更新（冪等性）
- sync: スクリプトとプロンプトの同期（--check および適用）
- --all フラグによる全プロファイル一括処理
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import cli as cli_mod  # noqa: E402
import main as core  # noqa: E402


def _write_jobs(path: Path, prompt: str, script: str = "dreams_diary_material.py", job_id: str = "job1"):
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "jobs": [
            {
                "id": job_id,
                "name": "dreams-diary",
                "script": script,
                "prompt": prompt,
                "schedule": {"kind": "cron", "expr": "30 23 * * *"},
            }
        ]
    }
    path.write_text(json.dumps(body), encoding="utf-8")


class SetupCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.profiles_root = self.root / "profiles"
        self.profiles_root.mkdir()
        self.plugin_root = REPO_ROOT

    def tearDown(self):
        self.tmp.cleanup()

    def _make_profile(self, name: str) -> Path:
        p = self.profiles_root / name
        p.mkdir(parents=True, exist_ok=True)
        return p

    def test_check_scripts_detects_missing_and_same(self):
        self._make_profile("alpha")
        # まだ scripts/ が無い
        res = core.check_scripts("alpha", profiles_root=self.profiles_root, plugin_root=self.plugin_root)
        self.assertEqual(res["status"], "missing")
        self.assertEqual(len(res["missing"]), 3)

        # 1ファイルだけコピー
        alpha_scripts = self.profiles_root / "alpha" / "scripts"
        alpha_scripts.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.plugin_root / "scripts" / "dreams_diary_material.py", alpha_scripts)
        res = core.check_scripts("alpha", profiles_root=self.profiles_root, plugin_root=self.plugin_root)
        self.assertEqual(res["status"], "diff")
        self.assertEqual(len(res["missing"]), 2)

        # 全ファイルをコピー
        for name in core.DREAMS_BRIDGE_SCRIPTS:
            shutil.copy2(self.plugin_root / "scripts" / name, alpha_scripts)
        res = core.check_scripts("alpha", profiles_root=self.profiles_root, plugin_root=self.plugin_root)
        self.assertEqual(res["status"], "same")
        self.assertEqual(res["missing"], [])
        self.assertEqual(res["diff"], [])

    def test_sync_scripts_copies_all_bridge_scripts(self):
        self._make_profile("beta")
        res = core.sync_scripts("beta", profiles_root=self.profiles_root, plugin_root=self.plugin_root)
        self.assertTrue(res["ok"])
        self.assertEqual(len(res["copied"]), 3)

        beta_scripts = self.profiles_root / "beta" / "scripts"
        for name in core.DREAMS_BRIDGE_SCRIPTS:
            self.assertTrue((beta_scripts / name).is_file())

        # 再実行時はコピー対象が 0 件（冪等）
        res2 = core.sync_scripts("beta", profiles_root=self.profiles_root, plugin_root=self.plugin_root)
        self.assertTrue(res2["ok"])
        self.assertEqual(len(res2["copied"]), 0)

    def test_install_profile_new_job(self):
        self._make_profile("gamma")
        calls = []

        def mock_runner(cmd):
            calls.append(cmd)
            # cron create が呼ばれたら jobs.json に書き込む
            if cmd[3:5] == ["cron", "create"]:
                jobs_path = self.profiles_root / "gamma" / "cron" / "jobs.json"
                _write_jobs(jobs_path, cmd[-1], job_id="created_job")
            return 0

        res = core.install_profile(
            "gamma",
            profiles_root=self.profiles_root,
            plugin_root=self.plugin_root,
            runner=mock_runner,
        )
        self.assertTrue(res["ok"])
        self.assertEqual(res["action"], "created")

        # symlink の確認
        link = self.profiles_root / "gamma" / "plugins" / "dreams-diary"
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(), self.plugin_root.resolve())

        # scripts の確認
        for name in core.DREAMS_BRIDGE_SCRIPTS:
            self.assertTrue((self.profiles_root / "gamma" / "scripts" / name).is_file())

        # hermes コマンドの確認 (enable と cron create)
        enable_call = next(c for c in calls if c[3:6] == ["plugins", "enable", "dreams-diary"])
        self.assertEqual(enable_call[1:3], ["-p", "gamma"])
        create_call = next(c for c in calls if c[3:5] == ["cron", "create"])
        self.assertEqual(create_call[1:3], ["-p", "gamma"])
        self.assertIn("--script", create_call)
        self.assertIn("dreams_diary_material.py", create_call)

    def test_install_profile_idempotent_when_job_exists(self):
        self._make_profile("delta")
        jobs_path = self.profiles_root / "delta" / "cron" / "jobs.json"
        _write_jobs(jobs_path, "古いプロンプト", job_id="delta_job")

        calls = []

        def mock_runner(cmd):
            calls.append(cmd)
            if cmd[3:5] == ["cron", "edit"]:
                _write_jobs(jobs_path, cmd[-1], job_id="delta_job")
            return 0

        res = core.install_profile(
            "delta",
            profiles_root=self.profiles_root,
            plugin_root=self.plugin_root,
            runner=mock_runner,
        )
        self.assertTrue(res["ok"])
        self.assertEqual(res["action"], "updated")

        # cron create ではなく cron edit が呼ばれていること
        self.assertFalse(any(c[3:5] == ["cron", "create"] for c in calls))
        edit_call = next(c for c in calls if c[3:5] == ["cron", "edit"])
        self.assertEqual(edit_call[5], "delta_job")

    def test_sync_all_check_and_apply(self):
        self._make_profile("p1")
        self._make_profile("p2")
        rendered = core.render_cron_prompt()

        # p1 は scripts あり・プロンプト古い
        core.sync_scripts("p1", profiles_root=self.profiles_root, plugin_root=self.plugin_root)
        _write_jobs(self.profiles_root / "p1" / "cron" / "jobs.json", "old", job_id="j1")

        # p2 は scripts なし・プロンプト最新
        _write_jobs(self.profiles_root / "p2" / "cron" / "jobs.json", rendered, job_id="j2")

        # --check
        res_check = core.sync_all(
            check_only=True,
            profiles_root=self.profiles_root,
            plugin_root=self.plugin_root,
        )
        self.assertFalse(res_check["ok"])  # 差分があるので False
        p1_state = next(p for p in res_check["profiles"] if p["profile"] == "p1")
        p2_state = next(p for p in res_check["profiles"] if p["profile"] == "p2")
        self.assertEqual(p1_state["scripts"]["status"], "same")
        self.assertEqual(p1_state["prompt"]["state"], "diff")
        self.assertEqual(p2_state["scripts"]["status"], "missing")
        self.assertEqual(p2_state["prompt"]["state"], "same")

        # apply
        calls = []

        def mock_runner(cmd):
            calls.append(cmd)
            if cmd[3:5] == ["cron", "edit"]:
                prof = cmd[2]
                jpath = self.profiles_root / prof / "cron" / "jobs.json"
                _write_jobs(jpath, cmd[-1], job_id=cmd[5])
            return 0

        res_apply = core.sync_all(
            check_only=False,
            profiles_root=self.profiles_root,
            plugin_root=self.plugin_root,
            runner=mock_runner,
        )
        self.assertTrue(res_apply["ok"])

        # 再度 check すると全て same で ok: True
        res_check2 = core.sync_all(
            check_only=True,
            profiles_root=self.profiles_root,
            plugin_root=self.plugin_root,
        )
        self.assertTrue(res_check2["ok"])


class SetupCliIntegrationTests(unittest.TestCase):
    def test_cli_help_flags_print_module_usage_and_succeed(self):
        for flag in ("--help", "-h"):
            with self.subTest(flag=flag):
                out = io.StringIO()
                err = io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    code = cli_mod.main([flag])
                self.assertEqual(code, 0)
                self.assertEqual(out.getvalue().strip(), cli_mod.__doc__.strip())
                self.assertEqual(err.getvalue(), "")

    def test_cli_install_requires_profile(self):
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = cli_mod.main(["install"])
        self.assertEqual(code, 2)
        self.assertIn("--profile", err.getvalue())

    def test_cli_sync_requires_all_or_profile_when_applying(self):
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = cli_mod.main(["sync"])
        self.assertEqual(code, 2)
        self.assertIn("--all", err.getvalue())


if __name__ == "__main__":
    unittest.main()
