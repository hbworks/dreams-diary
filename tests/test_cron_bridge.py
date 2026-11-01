"""Cron bridge tests: Hermes confines scripts to each profile's scripts/ directory."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


class CronBridgeTests(unittest.TestCase):
    def _exercise_bridge(self, script_name: str, expected_command: str) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile_home = Path(tmp) / "profile"
            plugin_dir = profile_home / "plugins" / "dreams-diary"
            plugin_dir.mkdir(parents=True)
            capture = Path(tmp) / "command.txt"
            (plugin_dir / "cli.py").write_text(
                "import os, sys\n"
                "from pathlib import Path\n"
                "Path(os.environ['DREAMS_DIARY_CAPTURE']).write_text(sys.argv[1], encoding='utf-8')\n",
                encoding="utf-8",
            )
            env = {
                **os.environ,
                "HERMES_HOME": str(profile_home),
                "DREAMS_DIARY_CAPTURE": str(capture),
            }
            result = subprocess.run(
                [sys.executable, str(REPO_ROOT / "scripts" / script_name)],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(capture.read_text(encoding="utf-8"), expected_command)

    def test_material_bridge_dispatches_material_command(self) -> None:
        self._exercise_bridge("dreams_diary_material.py", "material")

    def test_validator_bridge_dispatches_validate_command(self) -> None:
        self._exercise_bridge("dreams_diary_validate.py", "validate")


if __name__ == "__main__":
    unittest.main(verbosity=2)
