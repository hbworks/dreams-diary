"""Cron bridge tests: Hermes confines scripts to each profile's scripts/ directory.

The same bridge is the manual entry point, so these tests also pin:
- profile resolution when no --profile is given (HERMES_HOME wins; desktop marker and
  newest state.db are fallbacks),
- date forwarding (a YYYY-MM-DD argument reaches the CLI),
- the two-day window used by a bare `validate` (today, then yesterday, newest variant).
"""

from __future__ import annotations

import datetime
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BRIDGE = REPO_ROOT / "scripts" / "dreams_diary_material.py"


@contextmanager
def _env(**values):
    """一時的に環境変数を差し替える（None は削除）。"""
    saved = {key: os.environ.get(key) for key in values}
    for key, value in values.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _load_bridge():
    spec = importlib.util.spec_from_file_location("dreams_diary_bridge", BRIDGE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class CronBridgeTests(unittest.TestCase):
    def _make_env(self, tmp: Path, profile_name: str = "testprofile"):
        """隔離した HOME / HERMES_HOME と、argv を記録する偽 CLI を用意する。"""
        fake_home = tmp / "home"
        profile_home = fake_home / ".hermes" / "profiles" / profile_name
        plugin_dir = profile_home / "plugins" / "dreams-diary"
        plugin_dir.mkdir(parents=True)
        (profile_home / "dreams").mkdir()
        capture = tmp / "argv.json"
        (plugin_dir / "cli.py").write_text(
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "Path(os.environ['DREAMS_DIARY_CAPTURE']).write_text(\n"
            "    json.dumps(sys.argv[1:]), encoding='utf-8')\n",
            encoding="utf-8",
        )
        env = {
            **os.environ,
            "HOME": str(fake_home),
            "HERMES_HOME": str(profile_home),
            "DREAMS_DIARY_CAPTURE": str(capture),
        }
        for key in ("HERMES_SESSION_PROFILE", "HERMES_PROFILE"):
            env.pop(key, None)
        return capture, env

    def _run(self, script_name: str, env: dict, *args: str) -> list[str]:
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / script_name), *args],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(Path(env["DREAMS_DIARY_CAPTURE"]).read_text(encoding="utf-8"))

    def test_material_bridge_dispatches_material_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, env = self._make_env(Path(tmp))
            argv = self._run("dreams_diary_material.py", env)
            self.assertEqual(argv, ["material", "--profile", "testprofile"])

    def test_validator_bridge_dispatches_validate_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, env = self._make_env(Path(tmp))
            argv = self._run("dreams_diary_validate.py", env)
            self.assertEqual(argv[:2], ["validate", "--profile"])
            # 日記が無いので、今日の日付にフォールバックする
            self.assertEqual(argv[-1], datetime.date.today().isoformat())

    def test_date_argument_is_forwarded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, env = self._make_env(Path(tmp))
            argv = self._run("dreams_diary_validate.py", env, "2026-10-22")
            self.assertIn("2026-10-22", argv)
            self.assertNotIn("--path", argv)

    def test_bare_validate_prefers_newest_diary_in_two_day_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, env = self._make_env(Path(tmp))
            dreams = Path(env["HERMES_HOME"]) / "dreams"
            today = datetime.date.today()
            yesterday = today - datetime.timedelta(days=1)
            older = today - datetime.timedelta(days=2)
            for name in (
                f"{yesterday.isoformat()}.md",
                f"{yesterday.isoformat()}-2.md",
                f"{older.isoformat()}.md",
            ):
                (dreams / name).write_text("---\n---\n", encoding="utf-8")
            argv = self._run("dreams_diary_validate.py", env)
            self.assertIn("--path", argv)
            self.assertTrue(argv[-1].endswith(f"{yesterday.isoformat()}-2.md"))

    def test_today_wins_over_yesterday_variant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, env = self._make_env(Path(tmp))
            dreams = Path(env["HERMES_HOME"]) / "dreams"
            today = datetime.date.today()
            yesterday = today - datetime.timedelta(days=1)
            (dreams / f"{yesterday.isoformat()}-2.md").write_text("---\n---\n", encoding="utf-8")
            (dreams / f"{today.isoformat()}.md").write_text("---\n---\n", encoding="utf-8")
            argv = self._run("dreams_diary_validate.py", env)
            self.assertTrue(argv[-1].endswith(f"{today.isoformat()}.md"))

    def test_explicit_profile_is_not_overridden(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, env = self._make_env(Path(tmp))
            argv = self._run("dreams_diary_validate.py", env, "--profile", "other")
            self.assertEqual(argv[:3], ["validate", "--profile", "other"])
            self.assertNotIn("testprofile", argv)


class ProfileResolutionTests(unittest.TestCase):
    def _profiles(self, home: Path, *names: str) -> Path:
        root = home / ".hermes" / "profiles"
        for name in names:
            (root / name).mkdir(parents=True)
        return root

    def _desktop_marker(self, home: Path, name: str) -> None:
        marker = home / "Library" / "Application Support" / "Hermes" / "active-profile.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"profile": name}), encoding="utf-8")

    def test_hermes_home_wins_over_desktop_marker(self) -> None:
        bridge = _load_bridge()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            root = self._profiles(home, "fromhome", "fromapp")
            self._desktop_marker(home, "fromapp")
            with _env(
                HOME=str(home),
                HERMES_HOME=str(root / "fromhome"),
                HERMES_SESSION_PROFILE=None,
                HERMES_PROFILE=None,
            ):
                self.assertEqual(bridge.pick_profile([]), "fromhome")

    def test_desktop_marker_used_when_home_is_not_a_profile(self) -> None:
        bridge = _load_bridge()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self._profiles(home, "fromapp")
            self._desktop_marker(home, "fromapp")
            with _env(
                HOME=str(home),
                HERMES_HOME=str(home / "not-a-profile"),
                HERMES_SESSION_PROFILE=None,
                HERMES_PROFILE=None,
            ):
                self.assertEqual(bridge.pick_profile([]), "fromapp")

    def test_hermes_home_wins_over_session_profile(self) -> None:
        """cron / シェルの明示 HERMES_HOME が、内蔵ターミナルのセッション値を上書きする。"""
        bridge = _load_bridge()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            root = self._profiles(home, "explicit", "session")
            with _env(
                HOME=str(home),
                HERMES_HOME=str(root / "explicit"),
                HERMES_SESSION_PROFILE="session",
                HERMES_PROFILE=None,
            ):
                self.assertEqual(bridge.pick_profile([]), "explicit")

    def test_newest_state_db_is_the_last_resort(self) -> None:
        bridge = _load_bridge()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            root = self._profiles(home, "cold", "warm")
            for name in ("cold", "warm"):
                (root / name / "state.db").write_text("", encoding="utf-8")
            os.utime(root / "warm" / "state.db", (2_000_000_000, 2_000_000_000))
            os.utime(root / "cold" / "state.db", (1_000_000_000, 1_000_000_000))
            with _env(
                HOME=str(home),
                HERMES_HOME=str(home / "not-a-profile"),
                HERMES_SESSION_PROFILE=None,
                HERMES_PROFILE=None,
            ):
                self.assertEqual(bridge.pick_profile([]), "warm")


if __name__ == "__main__":
    unittest.main(verbosity=2)
