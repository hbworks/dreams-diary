"""素材の `## 自分の発言`（v1.6）を固定するテスト。

守る契約:
- 応答本文と時刻が素材に出る（会話節は件数しか持たないため）。
- 途中の応答は短く、セッション最後の応答だけ長く残す。
- 空 content / tool 呼び出しだけの assistant 行は出さない。
- 切り詰めが走っても節ごと消えない（成功した操作と同じ事故を繰り返さない）。
"""

from __future__ import annotations

import datetime
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import main as core  # noqa: E402

DAY = "2026-10-05"


def _ts(hh: int, mm: int = 0) -> float:
    return datetime.datetime.strptime(f"{DAY} {hh}:{mm:02d}", "%Y-%m-%d %H:%M").timestamp()


def make_profile(root: Path, rows) -> Path:
    home = root / "profiles" / "tester"
    (home / "dreams").mkdir(parents=True)
    con = sqlite3.connect(home / "state.db")
    con.execute(
        """create table messages (
               session_id text, role text, content text, tool_call_id text,
               tool_calls text, tool_name text, timestamp real)"""
    )
    con.executemany("insert into messages values (?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()
    return home


def _section(material: str, heading: str) -> str:
    marks = [material.find(heading)]
    if marks[0] < 0:
        return ""
    start = marks[0] + len(heading)
    rest = material[start:]
    end = rest.find("\n## ")
    return rest if end < 0 else rest[:end]


class OwnStatementsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _material(self, rows, **kwargs) -> dict:
        home = make_profile(self.root, rows)
        return core.collect_material(day=DAY, home=home, repos=[], **kwargs)

    def test_response_text_and_time_appear(self):
        rows = [
            ("s1", "user", "レビューして", None, None, None, _ts(9)),
            ("s1", "assistant", "まず実装を読みます。", None, None, None, _ts(9, 2)),
        ]
        section = _section(self._material(rows)["material"], "## 自分の発言（応答の本文・抜粋）")
        self.assertIn("09:02 応答: まず実装を読みます。", section)

    def test_last_response_keeps_more_text_than_middle_ones(self):
        long_middle = "中" * 700
        long_last = "結" * 700
        rows = [
            ("s1", "user", "依頼", None, None, None, _ts(9)),
            ("s1", "assistant", long_middle, None, None, None, _ts(9, 1)),
            ("s1", "assistant", long_last, None, None, None, _ts(9, 2)),
        ]
        section = _section(self._material(rows)["material"], "## 自分の発言（応答の本文・抜粋）")
        middle_line = next(l for l in section.splitlines() if "09:01" in l)
        last_line = next(l for l in section.splitlines() if "09:02" in l)
        self.assertEqual(len(middle_line), len("- 09:01 応答: ") + core.ASSIST_CLIP)
        self.assertEqual(len(last_line), len("- 09:02 応答: ") + core.ASSIST_FINAL_CLIP)

    def test_empty_content_is_skipped(self):
        rows = [
            ("s1", "user", "依頼", None, None, None, _ts(9)),
            ("s1", "assistant", None, None, '[{"function": {"name": "terminal"}}]', None, _ts(9, 1)),
            ("s1", "assistant", "   ", None, None, None, _ts(9, 2)),
            ("s1", "assistant", "返答", None, None, None, _ts(9, 3)),
        ]
        section = _section(self._material(rows)["material"], "## 自分の発言（応答の本文・抜粋）")
        self.assertIn("09:03 応答: 返答", section)
        self.assertNotIn("09:01", section)
        self.assertNotIn("09:02", section)

    def test_sessions_are_separated_and_capped(self):
        rows = [("s1", "user", "依頼", None, None, None, _ts(9))]
        for i in range(core.MAX_ASSIST_ITEMS_PER_SESSION + 3):
            rows.append(("s1", "assistant", f"応答{i}", None, None, None, _ts(9, i + 1)))
        section = _section(self._material(rows)["material"], "## 自分の発言（応答の本文・抜粋）")
        self.assertIn("冒頭 3 回の応答は省略", section)
        self.assertIn(f"応答{core.MAX_ASSIST_ITEMS_PER_SESSION + 2}", section)
        self.assertNotIn("09:01 応答: 応答0", section)

    def test_user_utterances_carry_their_time(self):
        rows = [("s1", "user", "依頼", None, None, None, _ts(21, 5))]
        section = _section(self._material(rows)["material"], "## 会話")
        self.assertIn("- 21:05 発言: 依頼", section)

    def test_section_survives_truncation(self):
        rows = [("s1", "user", f"依頼{i}-" + "x" * 180, None, None, None, _ts(9) + i * 60)
                for i in range(15)]
        rows.append(("s1", "assistant", "最後の結論です。", None, None, None, _ts(10)))
        result = self._material(rows, max_chars=1200, include_material_path=False)
        self.assertTrue(result["truncated"])
        self.assertIn("## 自分の発言（応答の本文・抜粋）", result["material"])
        self.assertIn("最後の結論です。", result["material"])

    def test_missing_state_db_is_stated_not_silent(self):
        home = self.root / "profiles" / "nostate"
        (home / "dreams").mkdir(parents=True)
        result = core.collect_material(day=DAY, home=home, repos=[])
        section = _section(result["material"], "## 自分の発言（応答の本文・抜粋）")
        self.assertIn("state.db が見つからない", section)


if __name__ == "__main__":
    unittest.main()
