"""v1.7 の受け入れ基準（docs/SPEC-v1.7.md）を固定するテスト。

素材の `## コミット` が、日別のコミットログ（Hermes 経由のコミット）を
「自分の仕事」の根拠として使うことを確かめる。
"""

from __future__ import annotations

import datetime
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests"))

import main as core  # noqa: E402
from test_v15 import (  # noqa: E402
    DAY,
    _assistant_call,
    _git,
    _section,
    _tool_result,
    _ts,
    make_profile,
    make_repo_with_two_commits,
)


def _log_path(home: Path, day: str = DAY) -> Path:
    return home / "logs" / "git-commits" / f"{day}.tsv"


def _write_log(home: Path, rows: list[str], day: str = DAY) -> None:
    path = _log_path(home, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


class CommitLogMaterialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo, self.mine, self.other = make_repo_with_two_commits(self.root)
        self.full = {
            self.mine: _git(self.repo, "rev-parse", "HEAD~1"),
        }

    def tearDown(self):
        self.tmp.cleanup()

    def _material(self, home: Path) -> str:
        return core.collect_material(DAY, home=home, repos=[self.repo])["material"]

    def _edit_rows_for_mine(self):
        return [
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

    def test_1_log_only_commit_is_listed_as_log(self):
        # other.txt を触るコミット（編集では拾えない）を、ログだけで自分の仕事にする。
        home = make_profile(self.root)
        _write_log(home, [f"{DAY}T12:00:00+09:00\tdemorepo\t{self.other}\ttester"])
        commits = _section(self._material(home), "## コミット")
        self.assertIn(f"{self.other}", commits)
        self.assertIn("[log]", commits)
        # my edit（mine.txt）はログにも編集にも当たらないので「ほか」に残る（1件のみ）。
        self.assertIn("ほかのコミット: 1 件", self._material(home))

    def test_2_edit_only_commit_is_listed_as_edit(self):
        home = make_profile(self.root, self._edit_rows_for_mine())
        commits = _section(self._material(home), "## コミット")
        self.assertIn(self.mine, commits)
        self.assertIn("[edit]", commits)

    def test_3_both_sources_are_listed_once_as_log_plus_edit(self):
        home = make_profile(self.root, self._edit_rows_for_mine())
        _write_log(home, [f"{DAY}T12:00:00+09:00\tdemorepo\t{self.mine}\ttester"])
        commits = _section(self._material(home), "## コミット")
        self.assertEqual(commits.count(self.mine), 1)
        self.assertIn("[log+edit]", commits)

    def test_4_other_profile_log_row_is_not_mine(self):
        home = make_profile(self.root)
        _write_log(home, [f"{DAY}T12:00:00+09:00\tdemorepo\t{self.other}\tresearch"])
        material = self._material(home)
        self.assertNotIn(self.other, _section(material, "## コミット"))
        self.assertIn("ほかのコミット: 2 件", material)

    def test_5_missing_log_falls_back_to_edit_and_says_so(self):
        home = make_profile(self.root, self._edit_rows_for_mine())
        material = self._material(home)
        commits = _section(material, "## コミット")
        self.assertIn(self.mine, commits)
        self.assertIn("コミットログが無い", commits)

    def test_6_old_day_files_are_removed_and_current_kept(self):
        home = make_profile(self.root)
        today = datetime.date.fromisoformat(DAY)
        old = (today - datetime.timedelta(days=31)).isoformat()
        kept = (today - datetime.timedelta(days=29)).isoformat()
        _write_log(home, ["x"], day=old)
        _write_log(home, ["x"], day=kept)
        _write_log(home, ["x"], day=DAY)
        core.collect_material(DAY, home=home, repos=[self.repo])
        names = sorted(p.name for p in (home / "logs" / "git-commits").glob("*.tsv"))
        self.assertNotIn(f"{old}.tsv", names)
        self.assertIn(f"{kept}.tsv", names)
        self.assertIn(f"{DAY}.tsv", names)

    def test_7_rotation_does_not_drop_the_days_own_rows(self):
        home = make_profile(self.root)
        row = f"{DAY}T12:00:00+09:00\tdemorepo\t{self.other}\ttester"
        _write_log(home, [row])
        core.collect_material(DAY, home=home, repos=[self.repo])
        self.assertIn(row, _log_path(home).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
