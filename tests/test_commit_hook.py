"""Tests for the commit log hook and its installer (scripts/)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import install_commit_hook as installer  # noqa: E402

HOOK_SRC = ROOT / "scripts" / "git_commit_log_hook.sh"


def _env(extra: dict[str, str] | None = None, drop: tuple[str, ...] = ()) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in drop and not k.startswith("HERMES_")}
    env.update(
        {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        }
    )
    env.update(extra or {})
    return env


class CommitHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "my-repo"
        self.repo.mkdir()
        self.home = self.root / "profiles" / "research"
        self.home.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True, env=_env())
        code, msg = installer.install(self.repo)
        self.assertEqual(code, 0, msg)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _commit(self, name: str, env: dict[str, str]) -> str:
        (self.repo / name).write_text(name, encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", name], check=True, env=env)
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-qm", f"add {name}"],
            check=True,
            env=env,
        )
        return subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        ).stdout.strip()

    def _log(self) -> list[list[str]]:
        path = self.home / "logs" / "git-commits.tsv"
        if not path.exists():
            return []
        return [line.split("\t") for line in path.read_text(encoding="utf-8").splitlines()]

    def test_hermes_commit_is_logged_with_profile_and_source(self) -> None:
        env = _env({"HERMES_HOME": str(self.home), "HERMES_SESSION_PROFILE": "research"})
        sha = self._commit("a.txt", env)
        rows = self._log()
        self.assertEqual(len(rows), 1, rows)
        when, repo, logged_sha, profile, source = rows[0]
        self.assertEqual(repo, "my-repo")
        self.assertEqual(logged_sha, sha)
        self.assertEqual(profile, "research")
        self.assertEqual(source, "hermes")
        self.assertRegex(when, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+\d{2}:\d{2}$")

    def test_profile_falls_back_to_home_name_without_session_var(self) -> None:
        env = _env({"HERMES_HOME": str(self.home)}, drop=("HERMES_SESSION_PROFILE",))
        self._commit("b.txt", env)
        _, _, _, profile, source = self._log()[0]
        self.assertEqual((profile, source), ("research", "hermes"))

    def test_plain_commit_is_logged_as_external(self) -> None:
        env = _env(drop=("HERMES_HOME", "HERMES_SESSION_PROFILE"))
        env["HOME"] = str(self.root / "home")
        sha = self._commit("c.txt", env)
        rows = (self.root / "home" / ".hermes" / "logs" / "git-commits.tsv").read_text(
            encoding="utf-8"
        ).splitlines()
        cols = rows[0].split("\t")
        self.assertEqual(cols[2], sha)
        self.assertEqual(cols[3:], ["unset", "external"])

    def test_log_failure_never_blocks_commit(self) -> None:
        blocker = self.root / "not-a-dir"
        blocker.write_text("x", encoding="utf-8")
        env = _env({"HERMES_HOME": str(blocker)}, drop=("HERMES_SESSION_PROFILE",))
        sha = self._commit("d.txt", env)
        head = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        self.assertEqual(head, sha)

    def test_install_is_idempotent(self) -> None:
        code, msg = installer.install(self.repo)
        self.assertEqual(code, 0)
        self.assertIn("already installed", msg)

    def test_existing_foreign_hook_is_not_overwritten(self) -> None:
        target = self.repo / ".git" / "hooks" / "post-commit"
        target.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
        code, msg = installer.install(self.repo)
        self.assertEqual(code, 2)
        self.assertIn("not overwritten", msg)
        self.assertEqual(target.read_text(encoding="utf-8"), "#!/bin/sh\necho mine\n")

    def test_core_hookspath_is_refused(self) -> None:
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "core.hooksPath", "/tmp/elsewhere"],
            check=True,
        )
        code, msg = installer.install(self.repo)
        self.assertEqual(code, 2)
        self.assertIn("core.hooksPath", msg)

    def test_non_repo_is_refused(self) -> None:
        code, _ = installer.install(self.root)
        self.assertEqual(code, 2)

    def test_installed_hook_is_executable_and_matches_source(self) -> None:
        target = self.repo / ".git" / "hooks" / "post-commit"
        self.assertTrue(os.access(target, os.X_OK))
        self.assertEqual(target.read_text(encoding="utf-8"), HOOK_SRC.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
